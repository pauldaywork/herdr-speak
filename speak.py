#!/usr/bin/env python3
"""Read the focused herdr pane's last agent answer aloud via a local Kokoro server.

`speak.py toggle [--verbatim]` starts speaking, or stops speech already in
progress. `speak.py stop` only stops. The work runs in a detached worker so the
herdr action returns at once.
"""

import glob
import json
import os
import queue
import re
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

DEFAULTS = {
    "baseUrl": "http://127.0.0.1:8880/v1",
    "model": "kokoro",
    "voice": "af_bella",
    "speed": 1.0,
    "rewrite": True,
    "rewriteModel": "haiku",
    "rewriteTimeout": 60,
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


def focused_pane():
    herdr = os.environ.get("HERDR_BIN_PATH") or "herdr"
    out = subprocess.run([herdr, "api", "snapshot"], capture_output=True, text=True, check=True).stdout
    snapshot = json.loads(out)["result"]["snapshot"]
    pane_id = os.environ.get("HERDR_PANE_ID") or snapshot.get("focused_pane_id")
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
    """messages yields (role, content) in order; return the last turn's assistant text.

    With finished_only, skip the turn in progress (the one after the last prompt).
    """
    current, finished = [], []
    for role, content in messages:
        if role == "user":
            if current:
                finished = current
            current = []
        elif role == "assistant":
            current += [t for t in text_blocks(content) if t.strip()]
    turn = finished if finished_only or not current else current
    return "\n\n".join(turn).strip()


def claude_messages(path):
    for entry in read_jsonl(path):
        if entry.get("isSidechain") or entry.get("isMeta"):
            continue
        message = entry.get("message") or {}
        content = message.get("content")
        if entry.get("type") == "assistant":
            yield "assistant", content
        elif entry.get("type") == "user":
            # Tool results come back as user entries; only a real prompt starts a turn.
            is_tool_result = isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            )
            if not is_tool_result:
                yield "user", content


def pi_messages(path):
    for entry in read_jsonl(path):
        if entry.get("type") != "message":
            continue
        message = entry.get("message") or {}
        role = message.get("role")
        if role in ("user", "assistant"):
            yield role, message.get("content")


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


def last_answer():
    pane = focused_pane()
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


def strip_markdown(text):
    text = re.sub(r"```.*?(```|$)", " ", text, flags=re.S)
    text = re.sub(r"^\s*\|.*\|\s*$", " ", text, flags=re.M)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*([-*+]|\d+\.)\s+", "", text, flags=re.M)
    text = re.sub(r"[`*_~>]", "", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def rewrite_stream(text, config):
    """Yield the spoken rewrite as it streams out of `claude -p`."""
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
            "--system-prompt", (ROOT / "prompt.md").read_text(),
        ],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    process.stdin.write(text)
    process.stdin.close()
    timer = threading.Timer(config["rewriteTimeout"], process.kill)
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
    return spoke


def worker(verbatim):
    config = load_config()
    try:
        text = last_answer()
    except Exception as error:  # noqa: BLE001 - every failure becomes a notification
        notify("Speak: nothing to read", str(error))
        return
    try:
        ensure_kokoro(config)
    except (OSError, RuntimeError) as error:
        notify("Speak: couldn't start Kokoro", str(error))
        return
    try:
        if config["rewrite"] and not verbatim:
            try:
                play(sentences(rewrite_stream(text, config)), config)
                return
            except (OSError, RuntimeError) as error:
                if isinstance(error, urllib.error.URLError):
                    raise
                print(f"rewrite failed, reading cleaned text instead: {error!r}", file=sys.stderr)
        play([strip_markdown(text)], config)
    except Exception as error:  # noqa: BLE001
        notify("Speak: TTS failed", f"{config['baseUrl']}: {error}")


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


def main(argv):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    command = argv[1] if len(argv) > 1 else "toggle"
    verbatim = "--verbatim" in argv

    if command == "stop":
        stop()
    elif command == "toggle":
        if stop():
            return
        with open(LOG_FILE, "a") as log:
            args = [sys.executable, str(Path(__file__).resolve()), "worker"] + (["--verbatim"] if verbatim else [])
            process = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
            )
        PID_FILE.write_text(str(process.pid))
    elif command == "worker":
        try:
            worker(verbatim)
        finally:
            if running_worker() == os.getpid():
                PID_FILE.unlink(missing_ok=True)
    else:
        print(f"usage: {argv[0]} toggle [--verbatim] | stop", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
