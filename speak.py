#!/usr/bin/env python3
"""Read the focused herdr pane's last agent answer, a chosen plan file, or selected text aloud via Kokoro.

`speak.py stop` stops speech in progress. `speak.py close` stops too, and
closes every pane the plugin opened. `speak.py plan` stops speech in progress,
or opens a picker pane (`speak.py pick`) that asks for files or the last
answer. A picked plan is spoken and saved for replay. The last answer opens a
paragraph picker (`speak.py paragraphs`) whose ticked paragraphs are spoken
like a selection. `speak.py selection` cancels a selection being prepared, or
rewrites the selected text and shows it beside the pane with live captions
(`speak.py captions`) and a player (`speak.py player`). The work runs in a
detached worker so the herdr action returns at once.
"""

import base64
import contextlib
import glob
import itertools
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("HERDR_PLUGIN_STATE_DIR") or ROOT / ".state")
CONFIG_DIR = Path(os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or ROOT)
PID_FILE = STATE_DIR / "worker.pid"
LOG_FILE = STATE_DIR / "speak.log"
PANES_FILE = STATE_DIR / "panes.json"
ANSWER_FILE = STATE_DIR / "answer.md"  # the last answer, for the paragraph picker
CHOSEN_FILE = STATE_DIR / "chosen.md"  # the paragraphs picked from it, for the worker
PLAN_PROMPT = ROOT / "prompt-plan.md"

DEFAULTS = {
    "baseUrl": "http://127.0.0.1:8880/v1",
    "model": "kokoro",
    "voice": "af_bella",
    "speed": 1.0,
    "rewrite": True,
    "rewriteModel": "haiku",
    "rewriteTimeout": 60,
    "planRewriteTimeout": 300,  # plans are long, and the rewrite keeps every step
    "spokenDir": None,  # None saves spoken plans under the plugin state dir
    "autoStart": True,
    "container": "kokoro-tts",
    "image": None,  # None picks the GPU image to match the NVIDIA GPU
    "startTimeout": 120,
    "player": [
        "ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
        "-f", "s16le", "-ar", "24000", "-ch_layout", "mono", "-i", "-",
    ],
}


def load_config():
    """Defaults, then the shared pi voice settings, then this plugin's config.json."""
    config = dict(DEFAULTS)
    try:
        pi = json.loads((Path.home() / ".pi" / "speak.json").read_text())
        provider = pi.get("providers", {}).get("openai-compatible", {})
        for src, dst in (("baseUrl", "baseUrl"), ("model", "model"), ("voice", "voice"), ("rate", "speed")):
            if provider.get(src) is not None:
                config[dst] = provider[src]
    except (OSError, ValueError):
        pass
    try:
        config.update(json.loads((CONFIG_DIR / "config.json").read_text()))
    except (OSError, ValueError):
        pass
    return config


def notify(title, body=None):
    herdr = os.environ.get("HERDR_BIN_PATH") or "herdr"
    cmd = [herdr, "notification", "show", title, "--sound", "none"]
    if body:
        cmd += ["--body", body]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


# --- Finding the last answer -------------------------------------------------


def focused_pane(pane_id=None):
    """The pane pane_id, else the pane the key was pressed in, else herdr's focused pane."""
    herdr = os.environ.get("HERDR_BIN_PATH") or "herdr"
    out = subprocess.run([herdr, "api", "snapshot"], capture_output=True, text=True, check=True).stdout
    snapshot = json.loads(out)["result"]["snapshot"]
    pane_id = pane_id or os.environ.get("HERDR_PANE_ID") or snapshot.get("focused_pane_id")
    for pane in snapshot.get("panes", []):
        if pane.get("pane_id") == pane_id:
            return pane
    raise RuntimeError("No focused pane found.")


def read_jsonl(path):
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def text_blocks(content):
    if isinstance(content, str):
        return [content]
    return [b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]


def last_turn_text(messages, finished_only=False):
    """messages yields (role, content, ended) in order; return the last turn's assistant text.

    ended is true for the assistant message that ends a turn. With finished_only,
    skip the turn in progress (the one after the last prompt) unless it has ended.
    """
    current, finished, ended = [], [], False
    for role, content, last in messages:
        if role == "user":
            if current:
                finished = current
            current, ended = [], False
        elif role == "assistant":
            current += [t for t in text_blocks(content) if t.strip()]
            ended = last
    turn = finished if (finished_only and not ended) or not current else current
    return "\n\n".join(turn).strip()


# Stop reasons that end a turn rather than hand over to a tool.
CLAUDE_TURN_ENDS = {"end_turn", "max_tokens", "stop_sequence", "refusal"}
PI_TURN_ENDS = {"stop", "length", "error", "aborted"}


def claude_messages(path):
    for entry in read_jsonl(path):
        if entry.get("isSidechain") or entry.get("isMeta"):
            continue
        message = entry.get("message") or {}
        content = message.get("content")
        if entry.get("type") == "assistant":
            yield "assistant", content, message.get("stop_reason") in CLAUDE_TURN_ENDS
        elif entry.get("type") == "user":
            # Tool results come back as user entries; only a real prompt starts a turn.
            is_tool_result = isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            )
            if not is_tool_result:
                yield "user", content, False


def pi_messages(path):
    for entry in read_jsonl(path):
        if entry.get("type") != "message":
            continue
        message = entry.get("message") or {}
        role = message.get("role")
        if role in ("user", "assistant"):
            yield role, message.get("content"), message.get("stopReason") in PI_TURN_ENDS


def claude_transcript(session):
    value = session.get("value") or ""
    if session.get("kind") == "path" or value.endswith(".jsonl"):
        return value
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    matches = glob.glob(os.path.join(config_dir, "projects", "*", f"{value}.jsonl"))
    if not matches:
        raise RuntimeError(f"No Claude transcript for session {value}.")
    return max(matches, key=os.path.getmtime)


def pi_transcript(session, cwd):
    value = (session or {}).get("value") or ""
    if value.endswith(".jsonl") and os.path.exists(value):
        return value
    base = Path.home() / ".pi" / "agent" / "sessions"
    files = glob.glob(str(base / "*" / f"*_{value}.jsonl")) if value else []
    if not files and cwd:
        # Same directory naming as pi's session manager.
        safe = "--" + re.sub(r"[/\\:]", "-", re.sub(r"^[/\\]", "", cwd)) + "--"
        files = glob.glob(str(base / safe / "*.jsonl"))
    if not files:
        raise RuntimeError("No pi session found for this pane.")
    return max(files, key=os.path.getmtime)


def last_answer(pane_id=None):
    """The last answer of the agent in pane_id, or in the pane the key was pressed in."""
    pane = focused_pane(pane_id)
    agent = pane.get("agent")
    session = pane.get("agent_session") or {}
    working = pane.get("agent_status") == "working"
    if agent == "claude" and session:
        text = last_turn_text(claude_messages(claude_transcript(session)), working)
    elif agent == "pi":
        text = last_turn_text(pi_messages(pi_transcript(session, pane.get("cwd"))), working)
    else:
        raise RuntimeError(f"The focused pane has no supported agent (found {agent or 'none'}).")
    if not text:
        raise RuntimeError("The agent hasn't written an answer yet.")
    return text


FENCE = re.compile(r"^\s*(```|~~~)")


def paragraphs(text):
    """text split at blank lines into paragraphs, each fenced code block kept whole."""
    found, current, fence = [], [], None
    for line in text.splitlines():
        marker = FENCE.match(line)
        if fence:
            current.append(line)
            if marker and marker.group(1) == fence:
                fence = None
        elif marker:
            current.append(line)
            fence = marker.group(1)
        elif line.strip():
            current.append(line)
        elif current:
            found.append("\n".join(current))
            current = []
    if current:
        found.append("\n".join(current))
    return found


def paragraph_input(paras):
    """fzf --read0 input: each paragraph after its index and a tab, ended by a NUL."""
    return "".join(f"{index}\t{para}\0" for index, para in enumerate(paras))


def chosen_text(paras, output):
    """The paragraphs whose indices fzf printed (NUL-separated), in their original order, joined by blank lines.

    fzf prints them in the order they were ticked, so they are sorted back.
    """
    picked = sorted({int(index) for index in output.split("\0") if index.strip().isdigit()})
    return "\n\n".join(paras[index] for index in picked if index < len(paras))


# --- Starting Kokoro --------------------------------------------------------


def kokoro_healthy(base_url):
    parts = urllib.parse.urlsplit(base_url)
    try:
        with urllib.request.urlopen(f"{parts.scheme}://{parts.netloc}/health", timeout=3) as response:
            return json.load(response).get("status") == "healthy"
    except (OSError, ValueError):
        return False


def gpu_image():
    """Kokoro's GPU image for this machine's NVIDIA GPU.

    Blackwell GPUs (RTX 50 series and data-center B-series) have compute
    capability 10 or higher and need the CUDA 12.8 build; RTX 30 and 40 series
    use the default build.
    """
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"],
            capture_output=True, text=True, check=True,
        ).stdout
        capability = max(float(line) for line in out.split())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise RuntimeError(f"No NVIDIA GPU found to pick a Kokoro image ({error}).") from error
    tag = "latest-cu128" if capability >= 10 else "latest"
    return f"ghcr.io/remsky/kokoro-fastapi-gpu:{tag}"


def ensure_kokoro(config):
    """Start the local Kokoro container when the server isn't answering.

    Starts the existing container if there is one, otherwise runs a new one
    the way the README does, then waits for it to report healthy.
    """
    if kokoro_healthy(config["baseUrl"]):
        return
    parts = urllib.parse.urlsplit(config["baseUrl"])
    if not config["autoStart"] or parts.hostname not in ("127.0.0.1", "localhost"):
        return
    name = config["container"]
    exists = subprocess.run(
        ["docker", "container", "inspect", name], capture_output=True, check=False,
    ).returncode == 0
    if exists:
        command = ["docker", "start", name]
    else:
        port = parts.port or 8880
        command = [
            "docker", "run", "-d", "--name", name, "--restart", "unless-stopped",
            "--gpus", "all", "-p", f"127.0.0.1:{port}:8880", config["image"] or gpu_image(),
        ]
    notify("Speak: starting Kokoro", f"{name} isn't running; this can take a minute.")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(command[:2])} failed: {result.stderr.strip()[-300:]}")
    deadline = time.monotonic() + config["startTimeout"]
    while time.monotonic() < deadline:
        if kokoro_healthy(config["baseUrl"]):
            return
        time.sleep(1)
    raise RuntimeError(f"Kokoro didn't become healthy within {config['startTimeout']} seconds.")


# --- Turning it into speech --------------------------------------------------


# Adapted from privateer-speak's stripMarkdown (src/distill.ts, MIT, (c) 2026 Patrick (zahnno)).
def strip_markdown(text):
    text = re.sub(r"```.*?(```|$)", " ", text, flags=re.S)
    text = re.sub(r"^\s*\|.*\|\s*$", " ", text, flags=re.M)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*([-*+]|\d+\.)\s+", "", text, flags=re.M)
    text = re.sub(r"[`*_~>]", "", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def rewrite_stream(text, config, prompt, timeout=None):
    """Yield the spoken rewrite as it streams out of `claude -p`.

    prompt is the system prompt file; timeout defaults to rewriteTimeout.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
    # Thinking delays the first words by up to a minute on long answers.
    env["MAX_THINKING_TOKENS"] = "0"
    process = subprocess.Popen(
        [
            "claude", "-p",
            "--model", config["rewriteModel"],
            "--no-session-persistence",
            "--tools", "",
            "--disable-slash-commands",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--system-prompt", prompt.read_text(),
        ],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    process.stdin.write(text)
    process.stdin.close()
    timer = threading.Timer(timeout or config["rewriteTimeout"], process.kill)
    timer.start()
    try:
        for line in process.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            delta = (event.get("event") or {}).get("delta") or {}
            if event.get("type") == "stream_event" and delta.get("type") == "text_delta":
                yield delta["text"]
        if process.wait() != 0:
            raise RuntimeError(f"claude exited {process.returncode}: {process.stderr.read()[-500:]}")
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
        process.stdout.close()
        process.stderr.close()
        process.wait()


SENTENCE_END = re.compile(r"[.!?][\"')\]]*\s+")


def sentences(deltas, first=20, rest=120):
    """Group streamed text into sentence chunks; the first is short so speech starts early."""
    buffer, minimum = "", first
    for delta in deltas:
        buffer += delta
        cut = 0
        for match in SENTENCE_END.finditer(buffer):
            if match.end() >= minimum:
                cut = match.end()
                break
        if cut:
            yield buffer[:cut].strip()
            buffer, minimum = buffer[cut:], rest
    if buffer.strip():
        yield buffer.strip()


def synthesize(text, config):
    """Yield raw 24 kHz mono s16le PCM for text from the Kokoro server."""
    body = json.dumps({
        "model": config["model"],
        "voice": config["voice"],
        "input": text,
        "speed": config["speed"],
        "response_format": "pcm",
        "stream": True,
    }).encode()
    request = urllib.request.Request(
        config["baseUrl"].rstrip("/") + "/audio/speech",
        data=body, headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        while chunk := response.read(16384):
            yield chunk


def synthesize_captioned(text, config):
    """(PCM, word timings) for text from Kokoro's captioned speech endpoint.

    The PCM is 24 kHz mono s16le like synthesize(); each timing is a dict with
    word, start_time and end_time in seconds into that PCM.
    """
    root = config["baseUrl"].rstrip("/")
    root = root.removesuffix("/v1")
    body = json.dumps({
        "model": config["model"],
        "voice": config["voice"],
        "input": text,
        "speed": config["speed"],
        "response_format": "pcm",
        "stream": False,
        "return_timestamps": True,
    }).encode()
    request = urllib.request.Request(
        root + "/dev/captioned_speech",
        data=body, headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        try:
            reply = json.load(response)
            return base64.b64decode(reply["audio"]), list(reply.get("timestamps") or [])
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError(f"Kokoro's captioned speech reply had no audio ({error!r})") from error


AUDIO_FORMAT = ["-f", "s16le", "-ar", "24000", "-ch_layout", "mono"]
BYTES_PER_SECOND = 48000  # AUDIO_FORMAT: 24000 samples a second, 2 bytes each
# A sentence ends where the next one starts with a capital, so "e.g. about" stays whole.
SENTENCE_BREAK = re.compile(r"[.!?][\"')\]]*\s+(?=[\"'(\[]?[A-Z])")
SENTENCE_TOKEN = re.compile(r"[.!?]+[\"')\]]*")


def caption_lines(start, text, words):
    """(seconds, sentence) captions for a chunk whose audio starts at start seconds.

    words are Kokoro's timings within the chunk's audio; the word after each
    sentence-ending token marks where the next sentence starts. Kokoro spells
    out numbers and abbreviations, so when its sentence ends don't line up with
    the text's, the whole chunk is one caption at start.
    """
    flat = " ".join(text.split())
    cuts = [match.end() for match in SENTENCE_BREAK.finditer(flat)]
    pieces = [flat[a:b].strip() for a, b in zip([0] + cuts, cuts + [len(flat)])]
    starts = [
        after.get("start_time", 0.0) for before, after in zip(words, words[1:])
        if SENTENCE_TOKEN.fullmatch(before.get("word", ""))
    ]
    if len(starts) != len(pieces) - 1:
        return [(start, flat)]
    return [(start, pieces[0])] + [(start + max(0.0, t), piece) for t, piece in zip(starts, pieces[1:])]


def lrc_stamp(seconds):
    """An .lrc time tag, [mm:ss.cc]."""
    centis = round(seconds * 100)
    return f"[{centis // 6000:02d}:{centis // 100 % 60:02d}.{centis % 100:02d}]"


def lrc_text(captions):
    """An .lrc file: one time-tagged line per (seconds, text) caption."""
    return "".join(f"{lrc_stamp(seconds)}{text}\n" for seconds, text in captions)


def spoken_file(base, suffix):
    """base with suffix appended, keeping any dots already in its name."""
    return base.with_name(base.name + suffix)


class Recording:
    """Saves spoken text, its captions and its audio as <base>.md, .lrc and .opus.

    With source, the text that was rewritten is saved too, as <base>.txt.
    Audio is encoded to <base>.opus.part as it is written, and every file
    appears only on finish(), so a stopped run never leaves a partial file that
    a later replay would trust.
    """

    def __init__(self, base, source=None):
        self.base = Path(base)
        self.source = source
        self.audio = spoken_file(self.base, ".opus")
        self.text = spoken_file(self.base, ".md")
        self.lyrics = spoken_file(self.base, ".lrc")
        self.raw = spoken_file(self.base, ".txt")
        self.partial = spoken_file(self.base, ".opus.part")
        self.parts = []
        self.captions = []  # (seconds, sentence) for the .lrc
        self.written = 0  # PCM bytes so far
        self.encoder = None
        self.finished = False
        self.failed = False

    def start(self):
        self.base.parent.mkdir(parents=True, exist_ok=True)
        self.encoder = subprocess.Popen(
            [
                "ffmpeg", "-y", "-loglevel", "error", *AUDIO_FORMAT, "-i", "-",
                "-c:a", "libopus", "-b:a", "48k", "-f", "ogg", str(self.partial),
            ],
            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )

    def add_text(self, text, words=()):
        """Note the chunk whose audio is written next; words are Kokoro's timings within it."""
        self.parts.append(text)
        self.captions += caption_lines(self.written / BYTES_PER_SECOND, text, list(words))

    def write(self, audio):
        """Feed the encoder; if it has died, note it once and keep playback going."""
        self.written += len(audio)
        if self.failed:
            return
        try:
            self.encoder.stdin.write(audio)
        except OSError as error:
            self.failed = True
            print(f"recording stopped, encoder failed: {error!r}", file=sys.stderr)

    def finish(self):
        if self.failed:
            return  # abort() cleans up
        self.encoder.stdin.close()
        if self.encoder.wait() != 0:
            raise RuntimeError(f"ffmpeg exited {self.encoder.returncode} saving {self.audio}")
        # Replay trusts the audio, so it must be the last file to appear.
        if self.source is not None:
            self.raw.write_text(self.source)
        self.text.write_text("\n\n".join(self.parts) + "\n")
        self.lyrics.write_text(lrc_text(self.captions))
        # A SIGTERM between the rename and the flag would make abort() keep only the audio.
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
        try:
            self.partial.replace(self.audio)
            self.finished = True
        finally:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})

    def abort(self):
        if self.encoder:
            if self.encoder.stdin and not self.encoder.stdin.closed:
                self.encoder.stdin.close()
            if self.encoder.poll() is None:
                self.encoder.kill()
                self.encoder.wait()
        for path in (self.partial, self.raw, self.text, self.lyrics):
            path.unlink(missing_ok=True)  # finish() may have failed after writing some


def spoken_dir(config):
    """Where spoken plans are saved: outside every project, so agents don't read them as plans."""
    if config.get("spokenDir"):
        return Path(config["spokenDir"]).expanduser()
    return STATE_DIR / "spoken"


def spoken_location(source):
    """(project, name) that a source's saved files go under.

    Inside git the project is the main repository's folder name (the same for
    every worktree) and the name is the file's path from that worktree's top
    level, without `.md` and with `--` between folders, so docs/README.md and
    README.md don't collide. Outside git, or if git can't run, the project is the
    file's folder and the name is its stem.
    """
    try:
        result = subprocess.run(
            [
                "git", "-C", str(source.parent), "rev-parse", "--path-format=absolute",
                "--git-common-dir", "--show-toplevel",
            ],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return source.parent.name, source.stem
    lines = result.stdout.splitlines()
    if result.returncode == 0 and len(lines) == 2:
        try:
            relative = source.relative_to(lines[1]).with_suffix("")
        except ValueError:
            return source.parent.name, source.stem
        return Path(lines[0]).parent.name, "--".join(relative.parts)
    return source.parent.name, source.stem


def spoken_base(source, config):
    """Base path (no suffix) for source's saved text and audio."""
    project, name = spoken_location(source)
    return spoken_dir(config) / project / name


def saved_audio(source, base):
    """The saved audio for source, when it is newer than the file."""
    audio = spoken_file(base, ".opus")
    try:
        if audio.stat().st_mtime > source.stat().st_mtime:
            return audio
    except OSError:
        pass
    return None


def play_file(audio, config):
    """Replay saved audio through the configured PCM player; raise RuntimeError if either exits badly."""
    decoder = subprocess.Popen(
        ["ffmpeg", "-loglevel", "error", "-i", str(audio), *AUDIO_FORMAT, "-"],
        stdout=subprocess.PIPE,
    )
    player = subprocess.Popen(config["player"], stdin=decoder.stdout)
    decoder.stdout.close()
    player.wait()
    decoder.wait()
    if decoder.returncode != 0 or player.returncode != 0:
        raise RuntimeError(f"ffmpeg exited {decoder.returncode}, player exited {player.returncode}")


def play(chunks, config):
    """Speak each text chunk in order through one player process.

    Chunks are pulled on a separate thread so the rewrite keeps streaming while
    earlier sentences play.
    """
    pending = queue.Queue()

    def produce():
        try:
            for chunk in chunks:
                pending.put(("text", chunk))
        except Exception as error:  # noqa: BLE001 - handed to the consumer
            pending.put(("error", error))
        pending.put(("done", None))

    threading.Thread(target=produce, daemon=True).start()
    player = subprocess.Popen(config["player"], stdin=subprocess.PIPE)
    spoke = False
    try:
        while True:
            kind, item = pending.get()
            if kind == "done":
                break
            if kind == "error":
                if not spoke:
                    raise item
                print(f"stopped partway: {item!r}", file=sys.stderr)
                break
            for audio in synthesize(item, config):
                player.stdin.write(audio)
            spoke = True
        player.stdin.close()
        player.wait()
    except BrokenPipeError:
        pass
    finally:
        if player.poll() is None and not spoke:
            player.terminate()
        if player.stdin and not player.stdin.closed:
            player.stdin.close()
        player.wait()
    return spoke


def say_cleaned(text, config):
    """Read text aloud with its markdown and code removed, unsaved: the fallback when a plan's rewrite fails."""
    try:
        play([strip_markdown(text)], config)
    except Exception as error:  # noqa: BLE001
        notify("Speak: TTS failed", f"{config['baseUrl']}: {error}")


def render(text, config, base, prompt=PLAN_PROMPT, timeout=None, keep_source=False):
    """Rewrite text for listening and save the speech as <base>.md, .lrc and .opus, unplayed.

    With keep_source, text itself is saved as <base>.txt. Raises when the
    rewrite or Kokoro fails, and then saves nothing.
    """
    recording = Recording(base, source=text if keep_source else None)
    recording.start()
    try:
        for sentence in sentences(rewrite_stream(text, config, prompt, timeout)):
            try:
                audio, words = synthesize_captioned(sentence, config)
            except urllib.error.HTTPError as error:
                if error.code not in (404, 405):
                    raise
                # No captioned endpoint on this server: one caption line per chunk.
                audio, words = b"".join(synthesize(sentence, config)), ()
            recording.add_text(sentence, words)
            recording.write(audio)
        if not recording.parts:
            raise RuntimeError("the rewrite was empty")
        recording.finish()
        if not recording.finished:
            raise RuntimeError(f"couldn't save {recording.audio}")
    finally:
        if not recording.finished:
            recording.abort()


def worker_plan(path):
    """Prepare a plan's audio, then show the plan with a player that plays it.

    A plan whose saved audio is newer than the file is replayed as it is.
    """
    config = load_config()
    source = Path(path).resolve()
    base = spoken_base(source, config)
    audio = saved_audio(source, base)
    if audio:
        try:
            play_file(audio, config)
            return
        except (OSError, RuntimeError) as error:
            # Prepare it afresh below, which replaces a corrupt saved file.
            notify("Speak: couldn't replay the saved audio", str(error))
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        notify("Speak: can't read the plan", str(error))
        return
    with preparing("the plan"):
        try:
            ensure_kokoro(config)
        except (OSError, RuntimeError) as error:
            notify("Speak: couldn't start Kokoro", str(error))
            return
        if config["rewrite"]:
            notify("Speak: preparing the plan", "It plays once the audio is ready. Press the key again to cancel.")
            try:
                render(text, config, base, timeout=config["planRewriteTimeout"])
            except (OSError, RuntimeError) as error:
                if isinstance(error, urllib.error.URLError):
                    notify("Speak: TTS failed", f"{config['baseUrl']}: {error}")
                    return
                print(f"rewrite failed, reading cleaned text instead: {error!r}", file=sys.stderr)
    show_plan(str(source))
    audio = saved_audio(source, base)
    if audio:
        open_player(str(source), audio, paused=False)
    else:
        # The cleaned text is a degraded fallback, so it is streamed and never saved.
        say_cleaned(text, config)


# --- Speaking a selection ------------------------------------------------------


def selection_base(cwd, config, now=None):
    """Base path for a selection's saved files: <spoken dir>/<project>/selection-<date>-<time>.

    The project follows spoken_location's rule for the pane's folder, so every
    worktree of a repository shares one.
    """
    project, _ = spoken_location(Path(cwd) / "selection")
    stamp = time.strftime("%Y-%m-%d-%H%M%S", time.localtime(now))
    return spoken_dir(config) / project / f"selection-{stamp}"


def speak_selection():
    """The speak.selection action: cancel a selection being prepared, otherwise prepare this one."""
    if stop():
        return
    if not (plugin_context().get("selected_text") or "").strip():
        notify("Speak: nothing selected", "Press prefix+[ for copy mode, select the text, then press the key.")
        return
    # The worker inherits this environment, so it reads the selection from it too.
    start_worker(["--selection"])


def worker_selection():
    """Rewrite the selected text and save its speech, then show it with captions and a player."""
    context = plugin_context()
    cwd = context.get("focused_pane_cwd") or Path.home()
    prepare_selection(context.get("selected_text") or "", cwd, "the selection")


def worker_answer(path):
    """Speak the paragraphs picked from the last answer, saved at path, the way a selection is spoken.

    The paragraph picker starts this worker in the key pane's cwd, which names the project.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        notify("Speak: nothing to read", str(error))
        return
    prepare_selection(text, Path.cwd(), "the answer")


def prepare_selection(text, cwd, label):
    """Rewrite text and save its speech as a selection of cwd's project, then show it with captions and a player.

    label says what is preparing, such as "the selection".
    """
    config = load_config()
    base = selection_base(cwd, config)
    with preparing(label):
        try:
            ensure_kokoro(config)
        except (OSError, RuntimeError) as error:
            notify("Speak: couldn't start Kokoro", str(error))
            return
        notify(f"Speak: preparing {label}", "It plays once the audio is ready. Press the key again to cancel.")
        try:
            render(text, config, base, prompt=PLAN_PROMPT, timeout=config["rewriteTimeout"], keep_source=True)
        except (OSError, RuntimeError) as error:
            if isinstance(error, urllib.error.URLError):
                notify("Speak: TTS failed", f"{config['baseUrl']}: {error}")
            else:
                notify(f"Speak: couldn't prepare {label}", str(error))
            return
    show_selection(base)


# --- Picking a plan ------------------------------------------------------------

SKIP_DIRS = {"node_modules", "vendor", "target", "dist", "build", "venv", "__pycache__"}


def plan_files(roots, exclude=None, max_depth=6):
    """Markdown files under roots, newest first, skipping hidden and dependency folders."""
    exclude = Path(exclude).resolve() if exclude else None
    found = {}
    for root in roots:
        root = Path(root).expanduser().resolve()
        if not root.is_dir():
            continue
        for folder, dirs, files in os.walk(root):
            here = Path(folder)
            if exclude and (here == exclude or exclude in here.parents):
                dirs[:] = []
                continue
            if len(here.parts) - len(root.parts) >= max_depth - 1:
                dirs[:] = []
            else:
                dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS]
            for name in files:
                if name.lower().endswith(".md"):
                    path = here / name
                    try:
                        found[path] = path.stat().st_mtime
                    except OSError:
                        continue
    return sorted(found, key=found.get, reverse=True)


def picker_lines(paths, cwd, home=None):
    """fzf lines: the full path, a tab, then the date and a short name to show."""
    cwd, home = Path(cwd).resolve(), Path(home or Path.home()).resolve()
    lines = []
    for path in paths:
        if path.is_relative_to(cwd):
            label = str(path.relative_to(cwd))
        elif path.is_relative_to(home):
            label = "~/" + str(path.relative_to(home))
        else:
            label = str(path)
        try:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))
        except OSError:
            continue  # deleted since the walk
        lines.append(f"{path}\t{when}  {label}")
    return lines


MODES = ["Files", "Last answer"]


def choose_mode():
    """Ask in fzf whether to speak a file or the last answer: one of MODES, or None on Esc.

    Raises OSError when fzf is missing.
    """
    result = subprocess.run(
        [
            "fzf", "--no-input", "--layout", "reverse", "--no-sort",
            "--header", "Enter picks, Esc cancels",
        ],
        input="\n".join(MODES), stdout=subprocess.PIPE, text=True, check=False,
    )
    choice = result.stdout.strip()
    return choice if result.returncode == 0 and choice in MODES else None


def pick():
    """Run in the picker pane: choose files or the last answer, then what of it to speak."""
    try:
        mode = choose_mode()
    except OSError:
        notify("Speak: fzf not found", "Install fzf to pick a plan.")
        return
    if mode == "Files":
        pick_plan()
    elif mode == "Last answer":
        pick_answer()


def pick_answer():
    """Save the key pane's last answer and open the paragraph picker on it.

    This runs in the overlay, whose own pane isn't the agent's, so the answer
    comes from SPEAK_TARGET_PANE.
    """
    try:
        text = last_answer(os.environ.get("SPEAK_TARGET_PANE"))
    except Exception as error:  # noqa: BLE001 - every failure becomes a notification
        notify("Speak: nothing to read", str(error))
        return
    ANSWER_FILE.write_text(text, encoding="utf-8")
    open_paragraphs(Path.cwd())


def pick_plan():
    """Choose a plan with fzf, then speak it."""
    config = load_config()
    cwd = Path.cwd()
    paths = plan_files([cwd, Path.home() / ".claude" / "plans"], exclude=spoken_dir(config))
    if not paths:
        notify("Speak: no plans found", f"No markdown files under {cwd} or ~/.claude/plans.")
        return
    try:
        result = subprocess.run(
            [
                "fzf", "--delimiter", "\t", "--with-nth", "2..", "--no-sort",
                "--prompt", "plan> ", "--header", "Enter reads the plan aloud, Esc cancels",
                "--preview", "head -n 200 {1}", "--preview-window", "right,60%,wrap",
            ],
            input="\n".join(picker_lines(paths, cwd)), stdout=subprocess.PIPE, text=True, check=False,
        )
    except OSError:
        notify("Speak: fzf not found", "Install fzf to pick a plan.")
        return
    choice = result.stdout.strip()
    if result.returncode != 0 or not choice:
        return
    path = choice.split("\t", 1)[0]
    stop()
    source = Path(path).resolve()
    audio = saved_audio(source, spoken_base(source, config))
    if audio:
        show_plan(path)
        open_player(path, audio, paused=False)
    else:
        # The worker shows the plan and its player once the audio is ready.
        start_worker(["--plan", path])


def herdr(*args):
    """Run a herdr CLI command, capturing its output."""
    binary = os.environ.get("HERDR_BIN_PATH") or "herdr"
    return subprocess.run([binary, *args], capture_output=True, text=True, check=False)


def load_panes():
    """The panes this plugin opened: {role: {"pane": id, "plan": path}} for roles plan, captions and player."""
    try:
        return json.loads(PANES_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_panes(panes):
    PANES_FILE.write_text(json.dumps(panes))


def live_pane(panes, role):
    """The recorded pane for role ("plan", "captions" or "player"), if it is still open."""
    entry = panes.get(role)
    if entry and herdr("pane", "get", entry["pane"]).returncode == 0:
        return entry
    return None


def close_pane(panes, role):
    entry = live_pane(panes, role)
    panes.pop(role, None)
    if entry:
        herdr("pane", "close", entry["pane"])


def open_pane(entrypoint, direction, target, cwd, env, focus=False):
    """Open one of this plugin's panes as a split, without taking focus unless focus, and return its id."""
    args = [
        "plugin", "pane", "open", "--plugin", os.environ.get("HERDR_PLUGIN_ID") or "speak",
        "--entrypoint", entrypoint, "--placement", "split", "--direction", direction,
        "--cwd", str(cwd),
    ]
    for key, value in env.items():
        args += ["--env", f"{key}={value}"]
    args.append("--focus" if focus else "--no-focus")
    if target:
        args += ["--target-pane", target]
    result = herdr(*args)
    if result.returncode != 0:
        notify("Speak: couldn't open a pane", result.stderr.strip()[-300:])
        return None
    try:
        return json.loads(result.stdout)["result"]["plugin_pane"]["pane"]["pane_id"]
    except (ValueError, KeyError, TypeError):
        return None


def show_plan(path):
    """Show the plan read-only beside the pane the key was pressed in.

    A pane already showing this plan is kept; one showing another plan or a selection is
    replaced, along with its captions and player.
    """
    panes = load_panes()
    close_pane(panes, "preparing")
    shown = live_pane(panes, "plan")
    if shown and shown["plan"] == path:
        save_panes(panes)
        return
    close_pane(panes, "plan")
    close_pane(panes, "captions")
    close_pane(panes, "player")
    pane = open_pane("plan-view", "right", os.environ.get("SPEAK_TARGET_PANE"), Path(path).parent, {"SPEAK_PLAN": path})
    if pane:
        panes["plan"] = {"pane": pane, "plan": path}
    save_panes(panes)


def open_player(path, audio, paused):
    """Open mpv on saved audio under the text pane, with live captions between them.

    Captions open when the audio has its .lrc beside it (recordings made before
    captions don't). Any captions and player already open are replaced.
    """
    panes = load_panes()
    close_pane(panes, "captions")
    close_pane(panes, "player")
    shown = live_pane(panes, "plan")
    if shown:
        target, direction = shown["pane"], "down"
    else:
        target, direction = os.environ.get("SPEAK_TARGET_PANE"), "right"
    cwd = Path(path).parent
    lyrics = Path(audio).with_suffix(".lrc")
    if lyrics.exists():
        captions = open_pane("captions", direction, target, cwd, {"SPEAK_LYRICS": str(lyrics)})
        if captions:
            panes["captions"] = {"pane": captions, "plan": path}
            # Without captions the player goes under the text: the audio is the point.
            target, direction = captions, "down"
    env = {"SPEAK_AUDIO": str(audio)}
    if paused:
        env["SPEAK_PAUSE"] = "1"
    pane = open_pane("plan-player", direction, target, cwd, env)
    if pane:
        panes["player"] = {"pane": pane, "plan": path}
    save_panes(panes)


def player_command(audio, paused):
    """mpv on saved audio, kept open at the end, with a time line and progress bar."""
    command = [
        "mpv", "--no-video", "--audio-display=no", "--keep-open=yes", "--term-osd-bar",
        "--msg-level=all=warn,statusline=status",
        "--term-status-msg=${?pause==yes:Paused }${time-pos} / ${duration}",
    ]
    if paused:
        command.append("--pause")
    return command + [str(audio)]


def run_player():
    """Run in the player pane: become mpv on the audio the pane was opened with."""
    command = player_command(os.environ["SPEAK_AUDIO"], bool(os.environ.get("SPEAK_PAUSE")))
    try:
        os.execvp(command[0], command)
    except OSError:
        notify("Speak: mpv not found", "Install mpv to replay and scrub saved plans.")


SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def key_pane():
    """The pane the key was pressed in: SPEAK_TARGET_PANE from inside a picker, else HERDR_PANE_ID."""
    return os.environ.get("SPEAK_TARGET_PANE") or os.environ.get("HERDR_PANE_ID")


def show_preparing(label):
    """Open a spinner pane beside the pane the key was pressed in.

    label says what is preparing, such as "the plan". It takes the place of any
    open text, captions and player panes, which the finished ones will replace.
    Workers started from a picker target the key's pane through
    SPEAK_TARGET_PANE; the others are still in that pane's environment.
    """
    panes = load_panes()
    for role in ("preparing", "plan", "captions", "player"):
        close_pane(panes, role)
    target = key_pane()
    pane = open_pane("preparing", "right", target, Path.home(), {"SPEAK_LABEL": label})
    if pane:
        panes["preparing"] = {"pane": pane, "plan": label}
    save_panes(panes)


def close_preparing():
    panes = load_panes()
    close_pane(panes, "preparing")
    save_panes(panes)


def close_panes():
    """The speak.close action: stop any speech or preparing, and close every pane this plugin opened.

    Panes already closed by hand are skipped, and no other pane is touched.
    """
    stop()
    panes = load_panes()
    for role in ("preparing", "plan", "captions", "player"):
        close_pane(panes, role)
    save_panes(panes)


@contextlib.contextmanager
def preparing(label):
    """Show the spinner pane while the block runs, and close it however the block ends."""
    show_preparing(label)
    try:
        yield
    finally:
        close_preparing()


def preparing_line(label, seconds, frame):
    return f" {frame} Preparing {label}… {seconds}s"


def run_preparing():
    """Run in the spinner pane: animate until the worker closes the pane."""
    label = os.environ.get("SPEAK_LABEL") or "the speech"
    start = time.monotonic()
    # Clear the pane, hide the cursor and write the hint once, under the spinner line.
    sys.stdout.write("\x1b[2J\x1b[?25l\x1b[3;1H Press the key again to cancel.")
    try:
        for frame in itertools.cycle(SPINNER):
            line = preparing_line(label, int(time.monotonic() - start), frame)
            sys.stdout.write(f"\x1b[1;1H\x1b[2K{line}")
            sys.stdout.flush()
            time.sleep(0.1)
    except (KeyboardInterrupt, BrokenPipeError):
        pass


def show_selection(base):
    """Show a selection's original text, live captions and player in a column beside the key pane.

    They replace any plan or selection panes already open, taking the plan,
    captions and player roles, and a plan picked later replaces them.
    """
    panes = load_panes()
    for role in ("preparing", "plan", "captions", "player"):
        close_pane(panes, role)
    base = Path(base)
    text = str(spoken_file(base, ".txt"))
    view = open_pane("plan-view", "right", key_pane(), base.parent, {"SPEAK_PLAN": text})
    if view:
        panes["plan"] = {"pane": view, "plan": text}
    save_panes(panes)
    if view:
        open_player(text, spoken_file(base, ".opus"), paused=False)


def sptlrx_config(folder):
    """sptlrx settings: follow mpv over MPRIS every 250 ms, with lyrics only from folder."""
    return "\n".join([
        "player: mpris",
        "updateInterval: 250",
        "mpris:",
        "  players: [mpv]",
        "local:",
        f"  folder: {json.dumps(str(folder))}",
        "style:",
        "  hAlignment: left",
        "  before: {faint: true}",
        '  current: {bold: true, foreground: "3"}',
        "  after: {}",
    ]) + "\n"


def run_captions():
    """Run in the captions pane: become sptlrx, showing the .lrc in step with the mpv player.

    sptlrx picks the .lrc whose name best matches what mpv is playing, so its
    folder holds only this one, and its config is ours rather than the user's.
    """
    lyrics = Path(os.environ["SPEAK_LYRICS"])
    folder = STATE_DIR / "captions"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    (folder / lyrics.name).symlink_to(lyrics)
    config = STATE_DIR / "sptlrx.yaml"
    config.write_text(sptlrx_config(folder))
    command = ["sptlrx", "--config", str(config)]
    try:
        os.execvp(command[0], command)
    except OSError:
        notify("Speak: sptlrx not found", "Install sptlrx (sudo apt install sptlrx) to show live captions.")


def plugin_context():
    """herdr's context for this invocation, or {} when it is missing or unreadable."""
    try:
        return json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    except ValueError:
        return {}


def open_picker():
    """The speak.plan action: stop speech in progress, otherwise open the plan picker."""
    if stop():
        return
    cwd = plugin_context().get("focused_pane_cwd") or str(Path.home())
    args = [
        "plugin", "pane", "open", "--plugin", os.environ.get("HERDR_PLUGIN_ID") or "speak",
        "--entrypoint", "plan-picker", "--placement", "overlay", "--cwd", cwd, "--focus",
    ]
    # The picker runs in its own pane, so tell it which pane to open the plan beside.
    if os.environ.get("HERDR_PANE_ID"):
        args += ["--env", f"SPEAK_TARGET_PANE={os.environ['HERDR_PANE_ID']}"]
    result = herdr(*args)
    if result.returncode != 0:
        notify("Speak: couldn't open the plan picker", result.stderr.strip()[-300:])


# --- Picking paragraphs of the last answer --------------------------------------


def open_paragraphs(cwd):
    """Open the paragraph picker, focused, to the right of the key pane where the audio panes go.

    It replaces any open text, captions and player panes. It isn't recorded in
    panes.json, because it closes itself when fzf exits.
    """
    panes = load_panes()
    for role in ("preparing", "plan", "captions", "player"):
        close_pane(panes, role)
    save_panes(panes)
    target = key_pane()
    env = {"SPEAK_TARGET_PANE": target} if target else {}
    open_pane("paragraphs", "right", target, cwd, env, focus=True)


def paragraph_command():
    """fzf ticking every paragraph: arrows move, Space ticks and unticks, Enter speaks the ticked, Esc cancels.

    Items are "<index>\\t<paragraph>"; fzf shows the paragraph and prints the
    indices. load:select-all, since start fires before --read0 input arrives.
    Enter does nothing while nothing is ticked, rather than taking the cursor's item.
    The Enter binding's transform runs under sh, regardless of the user's shell.
    """
    return [
        "fzf", "--multi", "--read0", "--print0", "--no-input", "--layout", "reverse",
        "--delimiter", "\t", "--with-nth", "2..", "--accept-nth", "1",
        "--wrap", "--gap", "--highlight-line",
        "--with-shell", "sh -c",
        "--bind", "load:select-all",
        "--bind", "space:toggle",
        "--bind", 'enter:transform:[ "$FZF_SELECT_COUNT" -eq 0 ] || echo accept',
        "--header", "Space ticks or unticks · Enter speaks the ticked · Esc cancels",
    ]


def run_paragraphs():
    """Run in the paragraph picker pane: tick the last answer's paragraphs, then hand them to a worker."""
    try:
        paras = paragraphs(ANSWER_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as error:
        notify("Speak: nothing to read", str(error))
        return
    try:
        result = subprocess.run(
            paragraph_command(), input=paragraph_input(paras), stdout=subprocess.PIPE, text=True, check=False,
        )
    except OSError:
        notify("Speak: fzf not found", "Install fzf to pick paragraphs.")
        return
    text = chosen_text(paras, result.stdout)
    if result.returncode != 0 or not text.strip():
        return
    CHOSEN_FILE.write_text(text, encoding="utf-8")
    stop()
    # The worker opens the spinner where this pane was, once this pane has closed.
    start_worker(["--answer", str(CHOSEN_FILE)])


# --- Process control -----------------------------------------------------------


def running_worker():
    try:
        pid = int(PID_FILE.read_text())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def stop():
    pid = running_worker()
    if pid is None:
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        pass
    PID_FILE.unlink(missing_ok=True)
    return True


def start_worker(args):
    """Run `speak.py worker *args` detached, so the caller returns at once."""
    with open(LOG_FILE, "a") as log:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "worker", *args],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
        )
    PID_FILE.write_text(str(process.pid))


def exit_on_sigterm():
    """Turn SIGTERM into a normal exit, so finally blocks run and a killed render leaves no partial files."""
    def handler(signum, frame):
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, handler)


def main(argv):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    command = argv[1] if len(argv) > 1 else ""

    if command == "stop":
        stop()
    elif command == "close":
        close_panes()
    elif command == "worker":
        exit_on_sigterm()
        try:
            if "--plan" in argv:
                worker_plan(argv[argv.index("--plan") + 1])
            elif "--selection" in argv:
                worker_selection()
            elif "--answer" in argv:
                worker_answer(argv[argv.index("--answer") + 1])
        finally:
            if running_worker() == os.getpid():
                PID_FILE.unlink(missing_ok=True)
    elif command == "plan":
        open_picker()
    elif command == "selection":
        speak_selection()
    elif command == "pick":
        pick()
    elif command == "paragraphs":
        run_paragraphs()
    elif command == "player":
        run_player()
    elif command == "preparing":
        run_preparing()
    elif command == "captions":
        run_captions()
    else:
        print(f"usage: {argv[0]} stop | close | plan | selection | pick | paragraphs | player | captions | preparing",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
