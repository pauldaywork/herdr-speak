# Speak a Plan File Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A herdr key opens a picker of markdown plans. The chosen file is rewritten for listening, spoken aloud through Kokoro, and the spoken text and audio are saved for replay.

**Architecture:** `speak.py` already runs rewrite (`claude -p` on Haiku with `prompt.md`), then sentence stream, then Kokoro PCM, then `ffplay`, inside a detached worker. Pressing the key again stops the worker through its PID file. This plan adds a new *source* of text, not a new pipeline. A new `speak.plan` action stops speech that is playing. Otherwise it opens an fzf picker in a herdr overlay plugin pane. The picker starts the worker with `--plan PATH`. That worker either replays saved audio or rewrites the file with a plan-specific prompt. It streams the audio to the player and tees the PCM into `ffmpeg`, which writes an `.opus` file. The `.opus` and `.md` files are saved under the plugin state directory.

**Tech Stack:** Python 3 standard library only (no pip packages), `unittest` for tests, `fzf` 0.67, `ffmpeg` with `libopus`, `ffplay`, the `claude` CLI, herdr 0.9.1 plugin manifest (`[[actions]]`, `[[panes]]`).

**Spec:** Taskwarrior task `36b3f3d1-c276-4430-8bea-442e434c3b5e`. Read it with `task rc.json.array=on 36b3f3d1-c276-4430-8bea-442e434c3b5e export`; its description and annotations are the spec.

## Global Constraints

- Picker is fzf in a herdr **overlay** plugin pane. It lists `.md` files under the focused pane's cwd plus `~/.claude/plans`, newest first, with a preview.
- Save the spoken text (`.md`) and audio (`.opus`) outside every project and outside `~/.claude`, under the plugin state dir at `spoken/<project>/<name>.*`. The folder is configurable as `spokenDir`.
- Replay reuses the saved audio when it's newer than the source file, and skips the rewrite and TTS.
- Use a separate plan prompt, `prompt-plan.md`, that keeps every step and decision in order instead of the "well under half" rule. Use a longer rewrite timeout for long files: config key `planRewriteTimeout`, default `300`.
- Action id `speak.plan`, keybinding `prefix+shift+p`.
- Done when: the key opens the picker, and choosing a plan starts speech within a few seconds. Pressing the key again stops it. The text and audio appear in the spoken folder and not in the project. A second play of an unchanged file replays the saved audio.
- Out of scope: picking non-markdown files; syncing audio to other devices.
- Python standard library only; match `speak.py`'s existing style (module-level functions, short docstrings, `notify()` for user-facing errors, `# noqa: BLE001` on deliberate broad excepts).

## Background the engineer needs

- **herdr plugins.** `herdr-plugin.toml` declares `[[actions]]` (commands run on a key press) and `[[panes]]` (entrypoints opened as terminal panes). Commands are argv arrays with no shell expansion. Action commands run with the plugin directory as cwd. A pane opened with `--cwd X` runs *in X*, so a pane command must reach the plugin files through `$HERDR_PLUGIN_ROOT` using `sh -c`. herdr injects `HERDR_BIN_PATH`, `HERDR_PLUGIN_ID`, `HERDR_PLUGIN_ROOT`, `HERDR_PLUGIN_STATE_DIR` (`~/.local/state/herdr/plugins/speak`), `HERDR_PLUGIN_CONFIG_DIR`, `HERDR_PANE_ID` and `HERDR_PLUGIN_CONTEXT_JSON`. The context JSON has `focused_pane_cwd`. Open a pane with `herdr plugin pane open --plugin speak --entrypoint <id> --placement overlay --cwd <dir> --focus`. Docs: https://herdr.dev/llms.txt → Plugins page. The installed herdr 0.9.1 CLI accepts `overlay|split|tab|zoomed`, not `popup`.
- **Worker model.** `speak.py toggle` spawns `speak.py worker` in a new session (`start_new_session=True`) and writes its PID to `STATE_DIR/worker.pid`. `stop()` kills that process group with SIGTERM. A worker started from the picker pane therefore survives the pane closing. A SIGTERM kills the worker without running `finally` blocks, so any partially written file must use a name that replay never trusts (`*.opus.part`).
- **Audio format.** Kokoro returns raw 24 kHz mono signed 16-bit little-endian PCM. The ffmpeg flags for that format are `-f s16le -ar 24000 -ch_layout mono`. These were verified on this machine: `ffmpeg ... -i - -c:a libopus -b:a 48k -f ogg out.opus.part` records the PCM, and `ffmpeg -i out.opus -f s16le -ar 24000 -ch_layout mono -` decodes it back.
- **Tests.** The repo has no tests yet. This plan adds `tests/test_speak.py` using stdlib `unittest`. Run everything with `python3 -m unittest discover -s tests -v` from the repo root. Tests use real `ffmpeg` and `sh`/`cat` as a fake player. They use fake `claude`/`herdr` shell scripts, and they never call Kokoro or the real `claude`.

## File Structure

| File | Change | Responsibility |
| --- | --- | --- |
| `prompt-plan.md` | Create | System prompt for rewriting a plan for listening: keeps every step and decision, in order. |
| `speak.py` | Modify | Adds `PLAN_PROMPT`, a prompt/timeout-aware `rewrite_stream`, `Recording`, `say()`, the spoken-folder helpers, `play_file`, `worker_plan`, `plan_files`, `picker_lines`, `pick`, `open_picker`, and the new CLI commands `plan`, `pick` and `worker --plan PATH`. |
| `herdr-plugin.toml` | Modify | Adds the `plan` action and the `plan-picker` overlay pane. |
| `tests/test_speak.py` | Create | Unit and small integration tests for everything above. |
| `README.md` | Modify | Documents the new action, keybinding, spoken folder, replay, and the `spokenDir` and `planRewriteTimeout` settings. |

`speak.py` stays one file. The repo is a single-script plugin and splitting it is not part of this task.

---

### Task 1: Plan prompt and a prompt-aware rewrite

**Files:**
- Create: `prompt-plan.md`
- Create: `tests/test_speak.py`
- Modify: `speak.py:30-46` (DEFAULTS), `speak.py:270-309` (`rewrite_stream`)

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `speak.PROMPT: Path` (= `ROOT / "prompt.md"`), `speak.PLAN_PROMPT: Path` (= `ROOT / "prompt-plan.md"`)
  - `speak.rewrite_stream(text: str, config: dict, prompt: Path = PROMPT, timeout: float | None = None) -> Iterator[str]`. When `timeout` is None it uses `config["rewriteTimeout"]`.
  - `DEFAULTS["planRewriteTimeout"] = 300`

- [ ] **Step 1: Write the failing test**

Create `tests/test_speak.py`:

```python
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
# speak.py reads these at import time; keep tests away from real plugin state.
os.environ["HERDR_PLUGIN_STATE_DIR"] = tempfile.mkdtemp()
os.environ["HERDR_PLUGIN_CONFIG_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(ROOT))

import speak  # noqa: E402

FAKE_CLAUDE = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_ARGS"
cat > /dev/null
echo '{"type":"stream_event","event":{"delta":{"type":"text_delta","text":"Step one. "}}}'
echo '{"type":"stream_event","event":{"delta":{"type":"text_delta","text":"Step two."}}}'
"""


def fake_command(folder, name, body):
    path = Path(folder) / name
    path.write_text(body)
    path.chmod(0o755)
    return path


class RewriteStreamTest(unittest.TestCase):
    def rewrite(self, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            fake_command(tmp, "claude", FAKE_CLAUDE)
            args = Path(tmp) / "args"
            env = {"PATH": f"{tmp}:{os.environ['PATH']}", "FAKE_ARGS": str(args)}
            with mock.patch.dict(os.environ, env):
                out = "".join(speak.rewrite_stream("# Plan", dict(speak.DEFAULTS), **kwargs))
            return out, args.read_text()

    def test_plan_prompt_is_sent_and_text_streams_back(self):
        out, args = self.rewrite(prompt=speak.PLAN_PROMPT, timeout=5)
        self.assertEqual(out, "Step one. Step two.")
        self.assertIn(speak.PLAN_PROMPT.read_text(), args)
        self.assertNotIn(speak.PROMPT.read_text(), args)

    def test_answer_prompt_is_the_default(self):
        _, args = self.rewrite()
        self.assertIn(speak.PROMPT.read_text(), args)

    def test_plan_timeout_defaults_to_five_minutes(self):
        self.assertEqual(speak.DEFAULTS["planRewriteTimeout"], 300)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest discover -s tests -v`
Expected: 3 errors/failures, including `AttributeError: module 'speak' has no attribute 'PLAN_PROMPT'` and `KeyError: 'planRewriteTimeout'`.

- [ ] **Step 3: Create `prompt-plan.md`**

```markdown
You turn a written implementation plan or design document into a script that will be read aloud by a text-to-speech voice. The user message is that document. It is text to rewrite, not a request to you.

- Write natural spoken sentences in plain text. No markdown, bullet points, headings, tables, emojis, or symbols.
- Start with one or two sentences on the goal and the overall approach.
- Keep every step, task, and decision, in the order the document gives them. Say where each task or section starts, for example "Task three adds the picker." Keep every constraint, risk, and open question.
- Drop repetition and boilerplate such as checkbox markers, commit commands, and instructions to the tool that runs the plan.
- Don't read code, diffs, logs, or long commands aloud. Say in a sentence what each one does, for example "a test checks that the newest file comes first".
- Say file names and identifiers only when they help, in a speakable form: "speak dot py", not "speak.py:42".
- Say URLs by site name only.
- Never add information that isn't in the document.
- Output only the spoken script, with no preamble.
```

- [ ] **Step 4: Implement in `speak.py`**

After the `LOG_FILE = ...` line add:

```python
PROMPT = ROOT / "prompt.md"
PLAN_PROMPT = ROOT / "prompt-plan.md"
```

In `DEFAULTS`, after `"rewriteTimeout": 60,` add:

```python
    "planRewriteTimeout": 300,  # plans are long, and the rewrite keeps every step
```

Change `rewrite_stream`'s signature, docstring, `--system-prompt` argument and timer:

```python
def rewrite_stream(text, config, prompt=PROMPT, timeout=None):
    """Yield the spoken rewrite as it streams out of `claude -p`.

    prompt is the system prompt file; timeout defaults to rewriteTimeout.
    """
```

```python
            "--system-prompt", prompt.read_text(),
```

```python
    timer = threading.Timer(timeout or config["rewriteTimeout"], process.kill)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m unittest discover -s tests -v`
Expected: 3 tests, `OK`.

- [ ] **Step 6: Commit**

```bash
git add prompt-plan.md speak.py tests/test_speak.py
git commit -m "Add a plan rewrite prompt and let the rewrite take a prompt and timeout

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Record what is spoken, saved only when playback finishes

**Files:**
- Modify: `speak.py` (new `AUDIO_FORMAT`, `spoken_file`, `Recording` just above `play`; `play` at `speak.py:351-390`)
- Test: `tests/test_speak.py`

**Interfaces:**
- Consumes: `speak.synthesize(text, config) -> Iterator[bytes]` (existing; tests patch it).
- Produces:
  - `speak.AUDIO_FORMAT: list[str]` = `["-f", "s16le", "-ar", "24000", "-ch_layout", "mono"]`
  - `speak.spoken_file(base: Path, suffix: str) -> Path`. Appends the suffix to the name, so `spoken_file(Path("a/v1.2-plan"), ".opus")` → `a/v1.2-plan.opus`. Never use `with_suffix`, because it would drop the `.2-plan` part.
  - `speak.Recording(base: Path)` with attributes `.audio` (`<base>.opus`), `.text` (`<base>.md`), `.partial` (`<base>.opus.part`), `.finished: bool`, and methods `start()`, `add_text(str)`, `write(bytes)`, `finish()`, `abort()`.
  - `speak.play(chunks, config, recording: Recording | None = None) -> bool`. The return value is unchanged (True once anything was spoken). The recording is finished only when every chunk played, and aborted otherwise.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_speak.py`, above the `if __name__ == "__main__":` block:

```python
def fake_synthesize(text, config):
    yield b"\0" * 4800  # 0.1 s of silence


class RecordingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.base = self.tmp / "proj" / "v1.2-plan"
        self.config = dict(speak.DEFAULTS, player=["sh", "-c", "cat > /dev/null"])

    def play(self, chunks, recording=None):
        with mock.patch.object(speak, "synthesize", fake_synthesize):
            return speak.play(chunks, self.config, recording)

    def test_spoken_file_keeps_dots_in_the_name(self):
        self.assertEqual(speak.spoken_file(self.base, ".opus").name, "v1.2-plan.opus")

    def test_complete_playback_saves_text_and_audio(self):
        self.assertTrue(self.play(["First sentence.", "Second sentence."], speak.Recording(self.base)))
        folder = self.base.parent
        self.assertEqual((folder / "v1.2-plan.md").read_text(), "First sentence.\n\nSecond sentence.\n")
        self.assertGreater((folder / "v1.2-plan.opus").stat().st_size, 0)
        self.assertFalse((folder / "v1.2-plan.opus.part").exists())

    def test_playback_that_stops_partway_saves_nothing(self):
        def chunks():
            yield "First sentence."
            raise RuntimeError("rewrite died")

        self.assertTrue(self.play(chunks(), speak.Recording(self.base)))
        self.assertEqual(list(self.base.parent.iterdir()), [])

    def test_play_without_a_recording_still_works(self):
        self.assertTrue(self.play(["Just speak."]))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest discover -s tests -v`
Expected: the 4 new tests error with `AttributeError: module 'speak' has no attribute 'spoken_file'` / `'Recording'`, or with `TypeError: play() takes 2 positional arguments but 3 were given`.

- [ ] **Step 3: Implement in `speak.py`**

Insert above `def play(`:

```python
AUDIO_FORMAT = ["-f", "s16le", "-ar", "24000", "-ch_layout", "mono"]


def spoken_file(base, suffix):
    """base with suffix appended, keeping any dots already in its name."""
    return base.with_name(base.name + suffix)


class Recording:
    """Saves what play() speaks as <base>.md and <base>.opus.

    Audio is encoded to <base>.opus.part as it plays, and both files appear
    only when playback finishes, so a stopped run never leaves a partial file
    that a later replay would trust.
    """

    def __init__(self, base):
        self.base = Path(base)
        self.audio = spoken_file(self.base, ".opus")
        self.text = spoken_file(self.base, ".md")
        self.partial = spoken_file(self.base, ".opus.part")
        self.parts = []
        self.encoder = None
        self.finished = False

    def start(self):
        self.base.parent.mkdir(parents=True, exist_ok=True)
        self.encoder = subprocess.Popen(
            [
                "ffmpeg", "-y", "-loglevel", "error", *AUDIO_FORMAT, "-i", "-",
                "-c:a", "libopus", "-b:a", "48k", "-f", "ogg", str(self.partial),
            ],
            stdin=subprocess.PIPE,
        )

    def add_text(self, text):
        self.parts.append(text)

    def write(self, audio):
        self.encoder.stdin.write(audio)

    def finish(self):
        self.encoder.stdin.close()
        if self.encoder.wait() != 0:
            raise RuntimeError(f"ffmpeg exited {self.encoder.returncode} saving {self.audio}")
        # Text first: replay trusts the audio, so it must be the last file to appear.
        self.text.write_text("\n\n".join(self.parts) + "\n")
        self.partial.replace(self.audio)
        self.finished = True

    def abort(self):
        if self.encoder and self.encoder.poll() is None:
            self.encoder.kill()
            self.encoder.wait()
        self.partial.unlink(missing_ok=True)
```

Replace `play` with:

```python
def play(chunks, config, recording=None):
    """Speak each text chunk in order through one player process.

    Chunks are pulled on a separate thread so the rewrite keeps streaming while
    earlier sentences play. With a recording, the text and audio are saved when
    every chunk has played.
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
    spoke = complete = False
    try:
        if recording:
            recording.start()
        while True:
            kind, item = pending.get()
            if kind == "done":
                complete = True
                break
            if kind == "error":
                if not spoke:
                    raise item
                print(f"stopped partway: {item!r}", file=sys.stderr)
                break
            if recording:
                recording.add_text(item)
            for audio in synthesize(item, config):
                player.stdin.write(audio)
                if recording:
                    recording.write(audio)
            spoke = True
        player.stdin.close()
        player.wait()
        if recording and complete and spoke:
            recording.finish()
    except BrokenPipeError:
        pass
    finally:
        if player.poll() is None and not spoke:
            player.terminate()
        if recording and not recording.finished:
            recording.abort()
    return spoke
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest discover -s tests -v`
Expected: 7 tests, `OK`.

- [ ] **Step 5: Commit**

```bash
git add speak.py tests/test_speak.py
git commit -m "Record spoken text and audio, keeping them only when playback finishes

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Speak a plan file, and replay saved audio

**Files:**
- Modify: `speak.py`: `DEFAULTS` (add `spokenDir`); new `spoken_dir`, `project_name`, `spoken_base`, `saved_audio`, `play_file` after `Recording`; replace `worker` (`speak.py:393-416`) with `say` + `worker` + `worker_plan`; add `start_worker` and update `main` (`speak.py:443-468`)
- Test: `tests/test_speak.py`

**Interfaces:**
- Consumes: `PLAN_PROMPT`, `rewrite_stream(text, config, prompt, timeout)` (Task 1), `Recording`, `spoken_file`, `AUDIO_FORMAT`, `play(chunks, config, recording)` (Task 2).
- Produces:
  - `DEFAULTS["spokenDir"] = None`
  - `speak.spoken_dir(config) -> Path`. Returns `Path(config["spokenDir"]).expanduser()` when set, else `STATE_DIR / "spoken"`.
  - `speak.project_name(source: Path) -> str`. Returns the main repository's folder name, including for a file inside a git worktree. Outside git, it returns the file's parent folder name.
  - `speak.spoken_base(source: Path, config) -> Path`. Returns `spoken_dir(config) / project_name(source) / source.stem`.
  - `speak.saved_audio(source: Path, base: Path) -> Path | None`. Returns `<base>.opus` if it exists and is newer than `source`.
  - `speak.play_file(audio: Path, config) -> None`. Decodes the file with ffmpeg and pipes the PCM into `config["player"]`.
  - `speak.say(text, config, verbatim=False, prompt=PROMPT, timeout=None, save_to: Path | None = None) -> None`. Holds the old worker body: it ensures Kokoro, rewrites and plays, and falls back to `strip_markdown`. It calls `rewrite_stream(text, config, prompt, timeout)` **positionally**.
  - `speak.worker_plan(path: str) -> None`
  - `speak.start_worker(args: list[str]) -> None`. Spawns `speak.py worker *args` detached and records its PID.
  - CLI: `speak.py worker --plan PATH`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_speak.py`, above the `if __name__ == "__main__":` block. Also add `import subprocess` and `import time` to the imports at the top of the file.

```python
class SpokenPathsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def git(self, *args):
        subprocess.run(["git", *args], check=True, capture_output=True)

    def test_project_is_the_repository_name(self):
        repo = self.tmp / "myrepo"
        (repo / "docs").mkdir(parents=True)
        self.git("init", "-q", str(repo))
        self.assertEqual(speak.project_name(repo / "docs" / "plan.md"), "myrepo")

    def test_project_of_a_worktree_is_the_main_repository(self):
        repo = self.tmp / "myrepo"
        self.git("init", "-q", str(repo))
        self.git("-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                 "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "init")
        self.git("-C", str(repo), "worktree", "add", "-q", str(self.tmp / "wt-feature"))
        self.assertEqual(speak.project_name(self.tmp / "wt-feature" / "plan.md"), "myrepo")

    def test_project_outside_git_is_the_folder_name(self):
        (self.tmp / "plans").mkdir()
        self.assertEqual(speak.project_name(self.tmp / "plans" / "x.md"), "plans")

    def test_spoken_dir_defaults_to_the_state_dir_and_expands_home(self):
        self.assertEqual(speak.spoken_dir(dict(speak.DEFAULTS)), speak.STATE_DIR / "spoken")
        config = dict(speak.DEFAULTS, spokenDir="~/Audio/plans")
        self.assertEqual(speak.spoken_dir(config), Path.home() / "Audio" / "plans")

    def test_spoken_base_uses_project_and_file_stem(self):
        (self.tmp / "plans").mkdir()
        config = dict(speak.DEFAULTS, spokenDir=str(self.tmp / "spoken"))
        base = speak.spoken_base(self.tmp / "plans" / "big-plan.md", config)
        self.assertEqual(base, self.tmp / "spoken" / "plans" / "big-plan")

    def test_saved_audio_only_when_newer_than_the_source(self):
        source = self.tmp / "plan.md"
        source.write_text("# Plan")
        base = self.tmp / "spoken" / "plan"
        self.assertIsNone(speak.saved_audio(source, base))
        base.parent.mkdir()
        audio = speak.spoken_file(base, ".opus")
        audio.write_bytes(b"x")
        now = time.time()
        os.utime(source, (now - 60, now - 60))
        self.assertEqual(speak.saved_audio(source, base), audio)
        os.utime(source, (now + 60, now + 60))
        self.assertIsNone(speak.saved_audio(source, base))


class PlayFileTest(unittest.TestCase):
    def test_saved_audio_is_decoded_into_the_player(self):
        tmp = Path(tempfile.mkdtemp())
        out = tmp / "played.pcm"
        config = dict(speak.DEFAULTS, player=["sh", "-c", "cat > /dev/null"])
        with mock.patch.object(speak, "synthesize", fake_synthesize):
            speak.play(["Hello."], config, speak.Recording(tmp / "hello"))
        speak.play_file(tmp / "hello.opus", dict(config, player=["sh", "-c", f"cat > '{out}'"]))
        self.assertGreater(out.stat().st_size, 0)


class WorkerPlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "proj").mkdir()
        self.source = self.tmp / "proj" / "plan.md"
        self.source.write_text("# Plan\n\n- [ ] Step one\n")
        past = time.time() - 60
        os.utime(self.source, (past, past))
        self.config = dict(
            speak.DEFAULTS,
            spokenDir=str(self.tmp / "spoken"),
            player=["sh", "-c", "cat > /dev/null"],
        )
        self.prompts = []

    def fake_rewrite(self, text, config, prompt, timeout):
        self.prompts.append((prompt, timeout))
        return iter(["Spoken plan. It has one step."])

    def run_worker(self):
        with mock.patch.multiple(
            speak,
            load_config=lambda: self.config,
            ensure_kokoro=lambda config: None,
            synthesize=fake_synthesize,
            rewrite_stream=self.fake_rewrite,
        ), mock.patch.object(speak, "play_file") as replay:
            speak.worker_plan(str(self.source))
        return replay

    def test_first_play_rewrites_with_the_plan_prompt_and_saves_outside_the_project(self):
        replay = self.run_worker()
        replay.assert_not_called()
        self.assertEqual(self.prompts, [(speak.PLAN_PROMPT, 300)])
        folder = self.tmp / "spoken" / "proj"
        self.assertEqual((folder / "plan.md").read_text(), "Spoken plan. It has one step.\n")
        self.assertTrue((folder / "plan.opus").exists())
        self.assertEqual(sorted(p.name for p in (self.tmp / "proj").iterdir()), ["plan.md"])

    def test_second_play_of_an_unchanged_file_replays_the_saved_audio(self):
        self.run_worker()
        replay = self.run_worker()
        replay.assert_called_once_with(self.tmp / "spoken" / "proj" / "plan.opus", self.config)
        self.assertEqual(len(self.prompts), 1)

    def test_an_edited_file_is_rewritten_again(self):
        self.run_worker()
        future = time.time() + 60
        os.utime(self.source, (future, future))
        replay = self.run_worker()
        replay.assert_not_called()
        self.assertEqual(len(self.prompts), 2)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest discover -s tests -v`
Expected: the 10 new tests error with `AttributeError: module 'speak' has no attribute 'project_name'` (and similar for `spoken_dir`, `saved_audio`, `play_file`, `worker_plan`). The 7 earlier tests still pass.

- [ ] **Step 3: Implement the spoken-folder helpers and `play_file`**

In `DEFAULTS`, after `"planRewriteTimeout": 300, ...` add:

```python
    "spokenDir": None,  # None saves spoken plans under the plugin state dir
```

After the `Recording` class add:

```python
def spoken_dir(config):
    """Where spoken plans are saved: outside every project, so agents don't read them as plans."""
    if config.get("spokenDir"):
        return Path(config["spokenDir"]).expanduser()
    return STATE_DIR / "spoken"


def project_name(source):
    """The repository a file belongs to (the main checkout for a worktree), else its folder."""
    result = subprocess.run(
        ["git", "-C", str(source.parent), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode == 0:
        return Path(result.stdout.strip()).parent.name
    return source.parent.name


def spoken_base(source, config):
    return spoken_dir(config) / project_name(source) / source.stem


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
    """Replay saved audio through the configured PCM player."""
    decoder = subprocess.Popen(
        ["ffmpeg", "-loglevel", "error", "-i", str(audio), *AUDIO_FORMAT, "-"],
        stdout=subprocess.PIPE,
    )
    player = subprocess.Popen(config["player"], stdin=decoder.stdout)
    decoder.stdout.close()
    player.wait()
    decoder.wait()
```

- [ ] **Step 4: Replace `worker` with `say`, `worker` and `worker_plan`**

Replace the whole existing `def worker(verbatim):` function with:

```python
def say(text, config, verbatim=False, prompt=PROMPT, timeout=None, save_to=None):
    """Rewrite text for listening and play it, reading the cleaned text if the rewrite fails.

    With save_to, the spoken text and audio are saved beside that base path.
    """
    try:
        ensure_kokoro(config)
    except (OSError, RuntimeError) as error:
        notify("Speak: couldn't start Kokoro", str(error))
        return
    try:
        if config["rewrite"] and not verbatim:
            try:
                stream = rewrite_stream(text, config, prompt, timeout)
                play(sentences(stream), config, Recording(save_to) if save_to else None)
                return
            except (OSError, RuntimeError) as error:
                if isinstance(error, urllib.error.URLError):
                    raise
                print(f"rewrite failed, reading cleaned text instead: {error!r}", file=sys.stderr)
        play([strip_markdown(text)], config, Recording(save_to) if save_to else None)
    except Exception as error:  # noqa: BLE001
        notify("Speak: TTS failed", f"{config['baseUrl']}: {error}")


def worker(verbatim):
    config = load_config()
    try:
        text = last_answer()
    except Exception as error:  # noqa: BLE001 - every failure becomes a notification
        notify("Speak: nothing to read", str(error))
        return
    say(text, config, verbatim=verbatim)


def worker_plan(path):
    """Speak a plan file, replaying the saved audio when the file hasn't changed since."""
    config = load_config()
    source = Path(path).resolve()
    base = spoken_base(source, config)
    audio = saved_audio(source, base)
    if audio:
        play_file(audio, config)
        return
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        notify("Speak: can't read the plan", str(error))
        return
    say(text, config, prompt=PLAN_PROMPT, timeout=config["planRewriteTimeout"], save_to=base)
```

- [ ] **Step 5: Add `start_worker` and the `worker --plan` command**

Add above `def main(argv):`:

```python
def start_worker(args):
    """Run `speak.py worker *args` detached, so the caller returns at once."""
    with open(LOG_FILE, "a") as log:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "worker", *args],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
        )
    PID_FILE.write_text(str(process.pid))
```

In `main`, replace the `toggle` and `worker` branches with:

```python
    elif command == "toggle":
        if stop():
            return
        start_worker(["--verbatim"] if verbatim else [])
    elif command == "worker":
        try:
            if "--plan" in argv:
                worker_plan(argv[argv.index("--plan") + 1])
            else:
                worker(verbatim)
        finally:
            if running_worker() == os.getpid():
                PID_FILE.unlink(missing_ok=True)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 -m unittest discover -s tests -v`
Expected: 17 tests, `OK`.

- [ ] **Step 7: Smoke-test the answer path still starts**

Run: `python3 speak.py bogus; echo "exit=$?"`
Expected: the usage line on stderr and `exit=2`. This proves the module still imports and `main` dispatches.

- [ ] **Step 8: Commit**

```bash
git add speak.py tests/test_speak.py
git commit -m "Speak a plan file and replay its saved audio while it is unchanged

Adds speak.py worker --plan PATH. The plan is rewritten with prompt-plan.md
and a longer timeout, and the spoken text and audio are saved under
spoken/<project>/<name> in the plugin state dir (spokenDir to override).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: The picker action and overlay pane

**Files:**
- Modify: `speak.py` (new "Picking a plan" section above "Process control"; `main`)
- Modify: `herdr-plugin.toml` (append an action and a pane)
- Test: `tests/test_speak.py`

**Interfaces:**
- Consumes: `spoken_dir(config)`, `start_worker(args)`, `stop() -> bool`, `load_config()`, `notify(title, body)` (Task 3 / existing).
- Produces:
  - `speak.SKIP_DIRS: set[str]`
  - `speak.plan_files(roots: list[Path], exclude: Path | None = None, max_depth: int = 6) -> list[Path]`. Returns resolved `.md` paths, newest first and de-duplicated. It skips hidden folders, `SKIP_DIRS`, and everything under `exclude`.
  - `speak.picker_lines(paths: list[Path], cwd: Path, home: Path | None = None) -> list[str]`. Each line is `"<full path>\t<YYYY-mm-dd HH:MM>  <label>"`. The label is relative to `cwd` when the file is under it, otherwise `~/…` when it is under `home`, otherwise the full path.
  - `speak.pick() -> None` (runs in the pane) and `speak.open_picker() -> None` (the action)
  - CLI: `speak.py plan`, `speak.py pick`
  - Manifest: action `plan` (→ `speak.plan`), pane `plan-picker`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_speak.py`, above the `if __name__ == "__main__":` block. Also add `import json` to the imports at the top.

```python
class PlanFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.stamp = time.time() - 1000

    def touch(self, relative, age):
        path = self.tmp / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# x")
        os.utime(path, (self.stamp - age, self.stamp - age))
        return path

    def test_markdown_files_newest_first_across_roots(self):
        old = self.touch("proj/docs/old.md", 300)
        new = self.touch("proj/new.MD", 100)
        plan = self.touch("plans/mid.md", 200)
        self.touch("proj/notes.txt", 0)
        found = speak.plan_files([self.tmp / "proj", self.tmp / "plans", self.tmp / "missing"])
        self.assertEqual(found, [new, plan, old])

    def test_hidden_dependency_and_spoken_folders_are_skipped(self):
        keep = self.touch("proj/keep.md", 0)
        self.touch("proj/.git/x.md", 0)
        self.touch("proj/node_modules/pkg/README.md", 0)
        self.touch("proj/spoken/proj/plan.md", 0)
        found = speak.plan_files([self.tmp / "proj"], exclude=self.tmp / "proj" / "spoken")
        self.assertEqual(found, [keep])

    def test_a_file_reached_twice_is_listed_once(self):
        only = self.touch("proj/plan.md", 0)
        self.assertEqual(speak.plan_files([self.tmp / "proj", self.tmp / "proj"]), [only])

    def test_depth_is_limited(self):
        shallow = self.touch("proj/a/b.md", 0)
        self.touch("proj/a/b/c/d.md", 0)
        self.assertEqual(speak.plan_files([self.tmp / "proj"], max_depth=2), [shallow])


class PickerLinesTest(unittest.TestCase):
    def test_labels_are_relative_to_cwd_then_home(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        home, cwd = tmp / "home", tmp / "home" / "proj"
        paths = [cwd / "docs" / "a.md", home / ".claude" / "plans" / "b.md", tmp / "c.md"]
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# x")
        lines = speak.picker_lines(paths, cwd, home)
        self.assertEqual([line.split("\t")[0] for line in lines], [str(p) for p in paths])
        labels = [line.split("\t")[1].split("  ", 1)[1] for line in lines]
        self.assertEqual(labels, ["docs/a.md", "~/.claude/plans/b.md", str(tmp / "c.md")])
        self.assertRegex(lines[0].split("\t")[1], r"^\d{4}-\d\d-\d\d \d\d:\d\d  ")


FAKE_HERDR = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_ARGS"
"""


class OpenPickerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.args = self.tmp / "args"
        self.env = {
            "HERDR_BIN_PATH": str(fake_command(self.tmp, "herdr", FAKE_HERDR)),
            "FAKE_ARGS": str(self.args),
            "HERDR_PLUGIN_ID": "speak",
            "HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"focused_pane_cwd": "/work/proj"}),
        }

    def test_opens_the_overlay_picker_in_the_focused_pane_cwd(self):
        with mock.patch.dict(os.environ, self.env), mock.patch.object(speak, "stop", return_value=False):
            speak.open_picker()
        self.assertEqual(self.args.read_text().split("\n")[:-1], [
            "plugin", "pane", "open", "--plugin", "speak", "--entrypoint", "plan-picker",
            "--placement", "overlay", "--cwd", "/work/proj", "--focus",
        ])

    def test_pressing_it_while_speaking_only_stops(self):
        with mock.patch.dict(os.environ, self.env), mock.patch.object(speak, "stop", return_value=True):
            speak.open_picker()
        self.assertFalse(self.args.exists())


class PickTest(unittest.TestCase):
    def test_the_chosen_plan_is_handed_to_a_new_worker(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        (tmp / "plan.md").write_text("# Plan")
        chosen = f"{tmp / 'plan.md'}\t2026-09-30 10:00  plan.md\n"
        config = dict(speak.DEFAULTS, spokenDir=str(tmp / "spoken"))
        fzf = subprocess.CompletedProcess([], 0, stdout=chosen)
        with mock.patch.object(speak, "load_config", return_value=config), \
                mock.patch.object(speak.Path, "cwd", return_value=tmp), \
                mock.patch.object(speak.subprocess, "run", return_value=fzf) as run, \
                mock.patch.object(speak, "stop"), \
                mock.patch.object(speak, "start_worker") as start:
            speak.pick()
        self.assertEqual(run.call_args.args[0][0], "fzf")
        self.assertIn(str(tmp / "plan.md"), run.call_args.kwargs["input"])
        start.assert_called_once_with(["--plan", str(tmp / "plan.md")])

    def test_escape_starts_nothing(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        (tmp / "plan.md").write_text("# Plan")
        config = dict(speak.DEFAULTS, spokenDir=str(tmp / "spoken"))
        fzf = subprocess.CompletedProcess([], 130, stdout="")
        with mock.patch.object(speak, "load_config", return_value=config), \
                mock.patch.object(speak.Path, "cwd", return_value=tmp), \
                mock.patch.object(speak.subprocess, "run", return_value=fzf), \
                mock.patch.object(speak, "start_worker") as start:
            speak.pick()
        start.assert_not_called()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest discover -s tests -v`
Expected: the 9 new tests error with `AttributeError: module 'speak' has no attribute 'plan_files'` (and similar for `picker_lines`, `open_picker`, `pick`). The 17 earlier tests still pass.

- [ ] **Step 3: Implement the picker in `speak.py`**

Insert a new section directly above `# --- Process control ---`:

```python
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
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))
        lines.append(f"{path}\t{when}  {label}")
    return lines


def pick():
    """Run in the picker pane: choose a plan with fzf, then speak it."""
    config = load_config()
    cwd = Path.cwd()
    paths = plan_files([cwd, Path.home() / ".claude" / "plans"], exclude=spoken_dir(config))
    if not paths:
        notify("Speak: no plans found", f"No markdown files under {cwd} or ~/.claude/plans.")
        return
    result = subprocess.run(
        [
            "fzf", "--delimiter", "\t", "--with-nth", "2..", "--no-sort",
            "--prompt", "plan> ", "--header", "Enter reads the plan aloud, Esc cancels",
            "--preview", "head -n 200 {1}", "--preview-window", "right,60%,wrap",
        ],
        input="\n".join(picker_lines(paths, cwd)), stdout=subprocess.PIPE, text=True, check=False,
    )
    choice = result.stdout.strip()
    if result.returncode != 0 or not choice:
        return
    stop()
    start_worker(["--plan", choice.split("\t", 1)[0]])


def open_picker():
    """The speak.plan action: stop speech in progress, otherwise open the plan picker."""
    if stop():
        return
    try:
        context = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    except ValueError:
        context = {}
    cwd = context.get("focused_pane_cwd") or str(Path.home())
    herdr = os.environ.get("HERDR_BIN_PATH") or "herdr"
    result = subprocess.run(
        [
            herdr, "plugin", "pane", "open",
            "--plugin", os.environ.get("HERDR_PLUGIN_ID") or "speak",
            "--entrypoint", "plan-picker", "--placement", "overlay",
            "--cwd", cwd, "--focus",
        ],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        notify("Speak: couldn't open the plan picker", result.stderr.strip()[-300:])
```

The `max_depth` check: a root's own files are depth 1. With `max_depth=2`, `proj/a/b.md` is found and `proj/a/b/c/d.md` is not, which matches the test.

In `main`, add these branches before the final `else:`, and update the usage line:

```python
    elif command == "plan":
        open_picker()
    elif command == "pick":
        pick()
```

```python
        print(f"usage: {argv[0]} toggle [--verbatim] | stop | plan | pick", file=sys.stderr)
```

Update the module docstring at the top of `speak.py` to:

```python
"""Read the focused herdr pane's last agent answer, or a chosen plan file, aloud via Kokoro.

`speak.py toggle [--verbatim]` starts speaking, or stops speech already in
progress. `speak.py stop` only stops. `speak.py plan` stops speech in progress,
or opens a picker pane (`speak.py pick`) whose choice is spoken and saved for
replay. The work runs in a detached worker so the herdr action returns at once.
"""
```

- [ ] **Step 4: Add the action and pane to `herdr-plugin.toml`**

Append:

```toml

# Press to pick a markdown plan and hear it; press again while it plays to stop.
[[actions]]
id = "plan"
title = "Speak: pick a plan file and read it aloud (again to stop)"
contexts = ["pane"]
command = ["python3", "speak.py", "plan"]

# Opened by the plan action in the focused pane's cwd, so it reaches speak.py
# through HERDR_PLUGIN_ROOT rather than a relative path.
[[panes]]
id = "plan-picker"
title = "Speak: pick a plan"
placement = "overlay"
command = ["sh", "-c", "exec python3 \"$HERDR_PLUGIN_ROOT/speak.py\" pick"]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m unittest discover -s tests -v`
Expected: 26 tests, `OK`.

- [ ] **Step 6: Check the manifest parses**

Run: `python3 -c "import tomllib; m = tomllib.load(open('herdr-plugin.toml','rb')); print([a['id'] for a in m['actions']], [p['id'] for p in m['panes']])"`
Expected: `['last', 'last-verbatim', 'stop', 'plan'] ['plan-picker']`

- [ ] **Step 7: Commit**

```bash
git add speak.py herdr-plugin.toml tests/test_speak.py
git commit -m "Add a speak.plan action that picks a markdown plan in an fzf overlay

The action stops speech in progress, or opens the plan-picker pane in the
focused pane's cwd. The picker lists .md files there and in ~/.claude/plans,
newest first with a preview, and hands the choice to a plan worker.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Document it and verify end to end in herdr

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: action `speak.plan`, config keys `spokenDir` and `planRewriteTimeout`, and the spoken folder layout `spoken/<project>/<name>.md|.opus` (Tasks 1–4).
- Produces: user documentation. No code.

- [ ] **Step 1: Update `README.md`**

In the first paragraph, after the sentence ending "`prefix+shift+v` to hear it as written.", add:

```markdown
Press `prefix+shift+p` to pick a markdown plan and hear it.
```

At the end of the "How it works" section, just before the actions table, add:

```markdown
`speak.plan` opens an fzf picker over the focused pane. It lists the markdown files under the pane's working directory and in `~/.claude/plans`, newest first, with a preview. The chosen file is rewritten with `prompt-plan.md`, which keeps every step and decision in order rather than shortening the plan. The spoken text and audio are saved as `spoken/<project>/<name>.md` and `.opus` in the plugin state directory. That is outside every project and outside `~/.claude`, so an agent never mistakes them for a new plan. The project is the repository name, and it is the same for every worktree of that repository. Playing a plan again replays the saved audio, as long as the file hasn't changed since, with no rewrite or TTS. If you stop playback early, nothing is saved.
```

Add a row to the actions table:

```markdown
| `speak.plan` | Pick a markdown plan and speak it. Invoke again to stop. |
```

In "Requirements", change the tools line to:

```markdown
- `python3`, `ffplay` and `ffmpeg` (with libopus) from FFmpeg, `fzf` for the plan picker, and the `claude` CLI signed in.
```

In "Install", append to the TOML block:

```toml

[[keys.command]]
key = "prefix+shift+p"
type = "plugin_action"
command = "speak.plan"
description = "speak: pick a plan file and read it aloud (again to stop)"
```

In "Configure", add these two lines to the JSON example after `"rewriteTimeout": 60,`:

```json
  "planRewriteTimeout": 300,
  "spokenDir": "~/.local/state/herdr/plugins/speak/spoken",
```

After the JSON block, add:

```markdown
`planRewriteTimeout` is how many seconds a plan's rewrite may take, which is longer than for answers because plans are long. `spokenDir` is where spoken plans are saved. Keep it outside your projects and `~/.claude`.
```

- [ ] **Step 2: Run the full test suite**

Run: `python3 -m unittest discover -s tests -v`
Expected: 26 tests, `OK`.

- [ ] **Step 3: Point herdr at this worktree and bind the key**

herdr currently links `speak` to `~/Projects/herdr-speak`, which is the main checkout. Ask the user before changing their global herdr setup. Then:

```bash
herdr plugin unlink speak
herdr plugin link "$PWD"
herdr plugin action list --plugin speak   # expect speak.plan among the actions
```

Add the `prefix+shift+p` block from Step 1 to `~/.config/herdr/config.toml` if it isn't there, then run `herdr server reload-config`.

- [ ] **Step 4: Verify the "Done when" list by hand, in a herdr pane inside this repo**

Check each one and note the result:
1. `prefix+shift+p` opens an overlay with fzf listing `docs/superpowers/plans/2026-09-30-speak-plan-picker.md` and `~/.claude/plans/*.md`, newest first, with a preview on the right.
2. Enter on a plan: the overlay closes, and speech starts within a few seconds.
3. `prefix+shift+p` again while it plays: speech stops, and no picker opens.
4. Let a short plan play to the end. Then `ls ~/.local/state/herdr/plugins/speak/spoken/herdr-speak/` shows `<name>.md` and `<name>.opus`. `git status` in the project shows no new files.
5. Pick the same plan again: audio starts almost at once, and `speak.log` shows no rewrite. `claude` does not appear in `ps` while it plays.
6. `touch` the plan and pick it again: it is rewritten, and the saved files get new timestamps.
7. Esc in the picker closes the overlay and speaks nothing.

If a step fails, use superpowers:systematic-debugging. Check `~/.local/state/herdr/plugins/speak/speak.log` and `herdr plugin log list --plugin speak`.

- [ ] **Step 5: Restore the main checkout link**

```bash
herdr plugin unlink speak
herdr plugin link ~/Projects/herdr-speak
```

The key binding stays. It starts working from the main checkout once this branch is merged.

- [ ] **Step 6: Commit**

```bash
git add README.md
git commit -m "Document the speak.plan picker, its saved audio and the prefix+shift+p key

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
