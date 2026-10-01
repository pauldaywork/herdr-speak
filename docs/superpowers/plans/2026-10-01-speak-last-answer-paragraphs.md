# Pick Last-Answer Paragraphs to Speak Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `prefix+shift+f` first asks **Files** or **Last answer**. Files is today's plan picker, unchanged. Last answer shows the agent's last answer as paragraphs, all ticked, in a focused pane to the right. You untick the ones you don't want and press Enter, and the rest is spoken the way a selection is.

**Architecture:** `pick()` (the overlay's entrypoint) gains a two-item fzf chooser in front of it. Today's body moves unchanged into `pick_plan()`. `pick_answer()` reads the last answer of the pane the key was pressed in (`SPEAK_TARGET_PANE`, through a new `pane_id` argument to `last_answer()`). It saves the answer to `STATE_DIR/answer.md` and opens a new `paragraphs` split pane, focused, to the right of the key pane. That pane runs `speak.py paragraphs`, which splits the answer into paragraphs (each fenced code block kept whole) and runs a multi-select fzf over them. It writes the ticked paragraphs, in their original order, to `STATE_DIR/chosen.md` and starts `speak.py worker --answer <file>`, then exits so its pane closes. The worker runs the selection flow, now shared as `prepare_selection()`: a spinner, a rewrite with `prompt-plan.md`, files saved as `spoken/<project>/selection-<stamp>.*`, then the text, captions and player panes.

**Tech Stack:** Python 3.14 standard library only, `unittest`, fzf 0.67.0, herdr 0.9.1 plugin manifest.

**Spec:** Taskwarrior task `1eacd7da-9320-4184-95eb-dcb483a6158e`. Read it with `task rc.json.array=on 1eacd7da-9320-4184-95eb-dcb483a6158e export`; its description and annotations are the spec.

## Global Constraints

- The original `prefix+shift+f` flow is not changed. The only addition is a two-item fzf chooser in front of it, with the items `Files` and `Last answer`. `Files` runs today's plan picker and everything after it (prepare, nvim, captions, player, saved audio) unchanged.
- `speak.selection` (`prefix+shift+a`, copy-mode selection) stays exactly as it is, and so do `speak.last` and `speak.last-verbatim` and what `last_turn_text` returns for them.
- The chooser runs in the plan-picker overlay, where `HERDR_PANE_ID` is the overlay itself. The answer must come from the agent pane in `SPEAK_TARGET_PANE`: `focused_pane()` and `last_answer()` take a pane id.
- While the agent is working, use the last finished answer, as `speak.last` does. `last_answer()` already does this.
- The paragraph picker opens as a focused split to the right of the key pane, where the audio panes go, replacing any open text, captions and player panes. Enter closes it and the preparing spinner pane takes its place. Esc closes it and does nothing.
- The paragraph picker is fzf with `--multi --read0 --no-input`, all items ticked at the start, `space:toggle`. Arrows move, Space ticks and unticks, Enter confirms, and Esc cancels. There is no search box, so no key conflicts, and no new dependency.
  - **Deviation, checked on fzf 0.67.0 in tmux:** the spec's `start:select-all` runs before `--read0` input has loaded, so nothing is ticked. Use `load:select-all`, which fired correctly.
  - fzf prints picked items in the order they were ticked, not list order. So each item is `<index>\t<paragraph>`, shown with `--with-nth 2..` and printed with `--accept-nth 1 --print0`, and the indices are sorted. This was checked on multi-line items, including a code block holding a tab.
  - With nothing ticked, fzf's Enter would accept the item under the cursor. `--bind 'enter:transform:[ "$FZF_SELECT_COUNT" -eq 0 ] || echo accept'` makes Enter do nothing then. This was checked too.
- Split on blank lines (a line of only whitespace counts as blank), but keep each fenced code block (```` ``` ```` or `~~~`) whole. Join the kept paragraphs in their original order with blank lines.
- The chosen text goes through the selection flow. It is rewritten with `prompt-plan.md` (`PLAN_PROMPT`) and `rewriteTimeout`, and saved as `spoken/<project>/selection-<YYYY-MM-DD-HHMMSS>` (`.txt`, `.md`, `.lrc`, `.opus`) with the project from the key pane's cwd. Then the text, captions and player panes open. Pressing the key again while it is preparing cancels it, which `open_picker()` already does through `stop()`.
- Out of scope: changing `speak.last`/`speak.last-verbatim`, changing copy-mode selection, changing the existing plan picker flow.
- Python standard library only. Match `speak.py`'s style: module-level functions, short docstrings, `notify()` for user-facing errors, `# noqa: BLE001` on deliberate broad excepts.

## Background the engineer needs

- **herdr plugins.** `herdr-plugin.toml` declares `[[actions]]` (run on a key press, with the plugin folder as cwd) and `[[panes]]` (entrypoints opened as terminal panes). Panes reach plugin files through `$HERDR_PLUGIN_ROOT` with `sh -c`. A pane closes when its process exits. `open_pane()` in `speak.py` wraps `herdr plugin pane open … --placement split --direction right|down --target-pane <id> --no-focus` and returns the new pane id. `herdr plugin pane open` also accepts `--focus`.
- **How `speak.plan` runs today.** The action calls `open_picker()`. If a worker is running it is stopped (that is "press again to cancel"). Otherwise an overlay pane `plan-picker` opens in the key pane's cwd, with `SPEAK_TARGET_PANE=<key pane id>` in its env. The overlay runs `speak.py pick`. Inside the overlay, `HERDR_PANE_ID` is the overlay, not the agent pane. Anything opened from there targets `SPEAK_TARGET_PANE`.
- **Worker model.** `start_worker(args)` spawns `speak.py worker *args` in a new session (so it survives the pane that started it closing) and writes its PID to `STATE_DIR/worker.pid`. `stop()` SIGTERMs that process group. The worker inherits its starter's environment and cwd. From the paragraphs pane, that means `SPEAK_TARGET_PANE` is the agent pane, `HERDR_PANE_ID` is the closed paragraphs pane, and the cwd is the key pane's cwd.
- **Panes the plugin tracks.** `panes.json` (`load_panes`/`save_panes`) records the roles `preparing`, `plan` (the text pane), `captions` and `player`. `close_pane(panes, role)` closes one if it is still open. The paragraphs pane is not recorded: it closes itself when fzf exits.
- **Tests.** Run `python3 -m unittest discover -s tests -v` from the repo root. 113 pass today. `tests/test_speak.py` sets a throwaway state dir and a failing fake `herdr` before importing `speak`. `fake_command(folder, name, body)` writes an executable script. `FAKE_HERDR_PANES` is a fake herdr that logs every call to `$FAKE_LOG` and tracks live panes, numbering new ones `p1`, `p2`, …. `PanesTest` sets `SPEAK_TARGET_PANE=w1` and `HERDR_PANE_ID=w2`.

## File Structure

| File | Change | Responsibility |
| --- | --- | --- |
| `speak.py` | Modify | `focused_pane(pane_id)`, `last_answer(pane_id)`; `paragraphs`, `paragraph_input`, `chosen_text`; `key_pane`; `prepare_selection`, `worker_answer`; `open_pane(focus=)`, `open_paragraphs`, `paragraph_command`, `run_paragraphs`; `choose_mode`, `pick` → `pick_plan` + `pick_answer`; CLI `paragraphs` and `worker --answer`; module docstring. |
| `herdr-plugin.toml` | Modify | The `paragraphs` pane; the `plan` action and `plan-picker` pane titles. |
| `tests/test_speak.py` | Modify | New tests; `PickTest` calls `pick_plan()`; one `PanesTest` selection test drops `SPEAK_TARGET_PANE`. |
| `README.md` | Modify | The chooser, the paragraph picker, its keys, and the action table row. |

`speak.py` stays one file, as the repo is a single-script plugin.

---

### Task 1: `last_answer()` reads a given pane

**Files:**
- Modify: `speak.py:91-99` (`focused_pane`), `speak.py:187-200` (`last_answer`)
- Test: `tests/test_speak.py` (new `LastAnswerPaneTest`, after `WorkerTest`)

**Interfaces:**
- Produces: `focused_pane(pane_id=None) -> dict` and `last_answer(pane_id=None) -> str`. With `pane_id`, that pane is used. Without it, `HERDR_PANE_ID`, then herdr's focused pane, as today.

- [ ] **Step 1: Write the failing tests**

Add after `class WorkerTest` in `tests/test_speak.py`:

```python
FAKE_HERDR_SNAPSHOT = """#!/bin/sh
cat "$FAKE_SNAPSHOT"
"""


class LastAnswerPaneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        transcript = self.tmp / "session.jsonl"
        transcript.write_text("".join(json.dumps(entry) + "\n" for entry in [
            {"type": "user", "message": {"content": "Question?"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "The agent's answer."}]}},
        ]))
        snapshot = {"result": {"snapshot": {"focused_pane_id": "overlay", "panes": [
            {"pane_id": "overlay"},
            {"pane_id": "agent", "agent": "claude", "agent_status": "idle",
             "agent_session": {"kind": "path", "value": str(transcript)}},
        ]}}}
        (self.tmp / "snapshot.json").write_text(json.dumps(snapshot))
        self.env = {
            "HERDR_BIN_PATH": str(fake_command(self.tmp, "herdr", FAKE_HERDR_SNAPSHOT)),
            "FAKE_SNAPSHOT": str(self.tmp / "snapshot.json"),
            "HERDR_PANE_ID": "overlay",
        }

    def test_a_pane_id_reads_that_panes_answer_not_the_overlays(self):
        with mock.patch.dict(os.environ, self.env):
            self.assertEqual(speak.last_answer("agent"), "The agent's answer.")

    def test_without_a_pane_id_it_reads_the_pane_the_key_was_pressed_in(self):
        with mock.patch.dict(os.environ, self.env), self.assertRaisesRegex(RuntimeError, "no supported agent"):
            speak.last_answer()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_speak.LastAnswerPaneTest -v`
Expected: `test_a_pane_id_reads_that_panes_answer_not_the_overlays` errors with `TypeError: last_answer() takes 0 positional arguments but 1 was given`. The other test passes already.

- [ ] **Step 3: Implement**

In `speak.py`, replace `focused_pane` and the first line of `last_answer`:

```python
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
```

```python
def last_answer(pane_id=None):
    """The last answer of the agent in pane_id, or in the pane the key was pressed in."""
    pane = focused_pane(pane_id)
```

Leave the rest of `last_answer` as it is.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest discover -s tests`
Expected: `OK`, 115 tests.

- [ ] **Step 5: Commit**

```bash
git add speak.py tests/test_speak.py
git commit -m "Let last_answer read a given pane, for pickers whose own pane isn't the agent's"
```

---

### Task 2: Split an answer into paragraphs and join the picked ones

**Files:**
- Modify: `speak.py` (new functions after `last_answer`, still under `# --- Finding the last answer`)
- Test: `tests/test_speak.py` (new `ParagraphsTest`, after `LastAnswerPaneTest`)

**Interfaces:**
- Produces:
  - `paragraphs(text: str) -> list[str]`: text split at blank lines, each fenced code block whole, with no surrounding blank lines.
  - `paragraph_input(paras: list[str]) -> str`: fzf `--read0` input, one `"<index>\t<paragraph>\0"` per paragraph.
  - `chosen_text(paras: list[str], output: str) -> str`: fzf's NUL-separated indices turned into the picked paragraphs in their original order, joined with `"\n\n"`. Returns `""` when none were picked.

- [ ] **Step 1: Write the failing tests**

```python
class ParagraphsTest(unittest.TestCase):
    def test_blank_lines_split_paragraphs(self):
        self.assertEqual(speak.paragraphs("One.\n\nTwo\nlines.\n\n\nThree.\n"), ["One.", "Two\nlines.", "Three."])

    def test_a_line_of_spaces_is_blank(self):
        self.assertEqual(speak.paragraphs("A\n   \nB"), ["A", "B"])

    def test_a_fenced_code_block_with_blank_lines_stays_whole(self):
        text = "Intro.\n\n```py\na = 1\n\nb = 2\n```\n\nAfter."
        self.assertEqual(speak.paragraphs(text), ["Intro.", "```py\na = 1\n\nb = 2\n```", "After."])

    def test_a_tilde_fence_is_closed_only_by_tildes(self):
        text = "~~~\n```\n\nstill code\n~~~\n\nAfter."
        self.assertEqual(speak.paragraphs(text), ["~~~\n```\n\nstill code\n~~~", "After."])

    def test_an_unclosed_fence_keeps_the_rest_whole(self):
        self.assertEqual(speak.paragraphs("Intro.\n\n```\nx\n\ny"), ["Intro.", "```\nx\n\ny"])

    def test_nothing_but_blank_lines_has_no_paragraphs(self):
        self.assertEqual(speak.paragraphs("\n \n\n"), [])

    def test_fzf_input_numbers_each_paragraph_and_ends_each_with_nul(self):
        self.assertEqual(speak.paragraph_input(["One.", "Two\nlines."]), "0\tOne.\x001\tTwo\nlines.\x00")

    def test_picked_paragraphs_keep_their_original_order(self):
        # fzf prints them in the order they were ticked.
        paras = ["One.", "Two.", "```\ncode\n```"]
        self.assertEqual(speak.chosen_text(paras, "2\x000\x00"), "One.\n\n```\ncode\n```")

    def test_nothing_picked_is_empty(self):
        self.assertEqual(speak.chosen_text(["One."], ""), "")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_speak.ParagraphsTest -v`
Expected: every test errors with `AttributeError: module 'speak' has no attribute 'paragraphs'` (or `paragraph_input`/`chosen_text`).

- [ ] **Step 3: Implement**

Add after `last_answer` in `speak.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest discover -s tests`
Expected: `OK`, 124 tests.

- [ ] **Step 5: Commit**

```bash
git add speak.py tests/test_speak.py
git commit -m "Split an answer into paragraphs, keeping code blocks whole, and join the picked ones in order"
```

---

### Task 3: A worker that speaks picked text the way a selection is spoken

**Files:**
- Modify: `speak.py:762-794` (`worker_selection` → `prepare_selection`, new `worker_answer`), `speak.py:1014-1029` (`show_preparing`), `speak.py:1068-1084` (`show_selection`), `speak.py:1191-1230` (`main`)
- Test: `tests/test_speak.py` (new `WorkerAnswerTest` after `WorkerSelectionTest`; `PanesTest` changes)

**Interfaces:**
- Consumes: `selection_base`, `render`, `preparing`, `show_selection` (existing).
- Produces:
  - `key_pane() -> str | None`: `SPEAK_TARGET_PANE` (set for anything opened from a picker), else `HERDR_PANE_ID`.
  - `prepare_selection(text: str, cwd, label: str) -> None`: the selection flow for any text. `label` fills the spinner and the notifications, such as `"the selection"` or `"the answer"`.
  - `worker_answer(path: str) -> None`: reads the picked text from `path` and calls `prepare_selection(text, Path.cwd(), "the answer")`.
  - CLI: `speak.py worker --answer <path>` calls `worker_answer(path)`.

- [ ] **Step 1: Write the failing tests**

Add after `class WorkerSelectionTest`:

```python
class WorkerAnswerTest(unittest.TestCase):
    CHOSEN = "First kept paragraph.\n\nSecond kept paragraph."

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "proj").mkdir()
        self.chosen = self.tmp / "chosen.md"
        self.chosen.write_text(self.CHOSEN)
        self.config = dict(speak.DEFAULTS, spokenDir=str(self.tmp / "spoken"))
        self.rewrites = []

    def fake_rewrite(self, text, config, prompt, timeout):
        self.rewrites.append((text, prompt, timeout))
        return iter(["Two paragraphs. Said aloud."])

    def run_worker(self, path=None, **patches):
        fakes = dict(load_config=lambda: self.config, ensure_kokoro=lambda config: None,
                     synthesize_captioned=fake_captioned, rewrite_stream=self.fake_rewrite)
        fakes.update(patches)
        mocks = {name: mock.DEFAULT for name in ("show_selection", "notify", "show_preparing", "close_preparing")
                 if name not in patches}
        # The paragraph picker starts the worker in the key pane's cwd.
        with mock.patch.object(speak.Path, "cwd", return_value=self.tmp / "proj"), \
                mock.patch.multiple(speak, **fakes), mock.patch.multiple(speak, **mocks) as used, \
                contextlib.redirect_stderr(io.StringIO()):
            speak.worker_answer(str(path or self.chosen))
        return used

    def test_saves_the_picked_text_as_a_selection_of_the_cwds_project(self):
        self.run_worker()
        folder = self.tmp / "spoken" / "proj"
        [txt] = folder.glob("selection-*.txt")
        self.assertRegex(txt.name, r"^selection-\d{4}-\d\d-\d\d-\d{6}\.txt$")
        self.assertEqual(txt.read_text(), self.CHOSEN)
        self.assertTrue(txt.with_suffix(".opus").exists())

    def test_rewrites_with_the_plan_prompt_and_the_answer_timeout(self):
        self.run_worker()
        self.assertEqual(self.rewrites, [(self.CHOSEN, speak.PLAN_PROMPT, 60)])

    def test_the_spinner_and_notification_name_the_answer(self):
        used = self.run_worker()
        used["show_preparing"].assert_called_once_with("the answer", replace=True)
        self.assertEqual([c.args[0] for c in used["notify"].call_args_list], ["Speak: preparing the answer"])
        used["show_selection"].assert_called_once()

    def test_a_missing_file_is_reported_and_prepares_nothing(self):
        used = self.run_worker(path=self.tmp / "gone.md")
        self.assertEqual(used["notify"].call_args.args[0], "Speak: nothing to read")
        used["show_preparing"].assert_not_called()
        self.assertEqual(self.rewrites, [])

    def test_the_cli_runs_it(self):
        with mock.patch.object(speak, "worker_answer") as worker_answer:
            self.assertEqual(speak.main(["speak.py", "worker", "--answer", "/s/chosen.md"]), 0)
        worker_answer.assert_called_once_with("/s/chosen.md")
```

In `PanesTest`, change `test_a_selection_opens_its_text_captions_and_player_in_a_column` so it runs without `SPEAK_TARGET_PANE`, as the selection action does. Replace its first two lines:

```python
    def test_a_selection_opens_its_text_captions_and_player_in_a_column(self):
        base = self.saved("selection-1")
        speak.show_selection(base)
```

with:

```python
    def test_a_selection_opens_its_text_captions_and_player_in_a_column(self):
        base = self.saved("selection-1")
        with mock.patch.dict(os.environ):
            del os.environ["SPEAK_TARGET_PANE"]  # the selection action runs in the key pane
            speak.show_selection(base)
```

Then add after that test:

```python
    def test_picked_paragraphs_open_beside_the_agent_pane_not_the_picker(self):
        # From the paragraph picker, HERDR_PANE_ID (w2) is the picker, which has closed.
        speak.show_selection(self.saved("selection-1"))
        self.assertTrue(self.calls("plugin")[0].endswith("--target-pane w1"))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_speak.WorkerAnswerTest tests.test_speak.PanesTest -v`
Expected: every `WorkerAnswerTest` test errors with `AttributeError: … 'worker_answer'`. `test_picked_paragraphs_open_beside_the_agent_pane_not_the_picker` fails because the target is `w2`. The edited column test still passes.

- [ ] **Step 3: Implement**

Add above `show_preparing` in `speak.py`:

```python
def key_pane():
    """The pane the key was pressed in: SPEAK_TARGET_PANE from inside a picker, else HERDR_PANE_ID."""
    return os.environ.get("SPEAK_TARGET_PANE") or os.environ.get("HERDR_PANE_ID")
```

In `show_preparing`, replace the line `target = os.environ.get("SPEAK_TARGET_PANE") or os.environ.get("HERDR_PANE_ID")` with:

```python
    target = key_pane()
```

Update that docstring's last sentence to say: "Workers started from a picker target the key's pane through SPEAK_TARGET_PANE; the others are still in that pane's environment."

In `show_selection`, change the `open_pane("plan-view", …)` line to target `key_pane()`:

```python
    view = open_pane("plan-view", "right", key_pane(), base.parent, {"SPEAK_PLAN": text})
```

Replace `worker_selection` with:

```python
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
```

In `main`, in the `worker` branch, add the `--answer` case after `--selection`:

```python
            elif "--selection" in argv:
                worker_selection()
            elif "--answer" in argv:
                worker_answer(argv[argv.index("--answer") + 1])
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest discover -s tests`
Expected: `OK`, 130 tests. Every `WorkerSelectionTest` still passes unchanged.

- [ ] **Step 5: Commit**

```bash
git add speak.py tests/test_speak.py
git commit -m "Add a worker that speaks picked text as a selection, beside the pane the key was pressed in"
```

---

### Task 4: The paragraph picker pane

**Files:**
- Modify: `speak.py` (`open_pane` at `speak.py:916-935`; new `open_paragraphs`, `paragraph_command`, `run_paragraphs` in a new section `# --- Picking paragraphs of the last answer` placed after `open_picker`; `main`)
- Modify: `herdr-plugin.toml` (new `[[panes]]` entry after `plan-picker`)
- Test: `tests/test_speak.py` (new `RunParagraphsTest` after `PickTest`; new `PanesTest` and `ManifestTest` tests)

**Interfaces:**
- Consumes: `paragraphs`, `paragraph_input`, `chosen_text` (Task 2); `key_pane` (Task 3); `speak.py worker --answer <path>` (Task 3).
- Produces:
  - `ANSWER_FILE = STATE_DIR / "answer.md"` (the whole answer, written by Task 5's `pick_answer`) and `CHOSEN_FILE = STATE_DIR / "chosen.md"` (the picked text).
  - `open_pane(entrypoint, direction, target, cwd, env, focus=False)`: `focus=True` passes `--focus` instead of `--no-focus`.
  - `open_paragraphs(cwd) -> None`: closes the `preparing`, `plan`, `captions` and `player` panes, then opens the `paragraphs` pane focused, to the right of `key_pane()`, in `cwd`, with `SPEAK_TARGET_PANE=<key pane>`.
  - `paragraph_command() -> list[str]` and `run_paragraphs() -> None` (CLI `speak.py paragraphs`).

- [ ] **Step 1: Write the failing tests**

Add to `PanesTest`:

```python
    def test_the_paragraph_picker_opens_focused_beside_the_key_pane_replacing_the_audio_panes(self):
        speak.show_plan("/work/a.md")
        speak.open_player("/work/a.md", Path("/s/a.opus"), paused=False)
        speak.open_paragraphs(Path("/work/proj"))
        self.assertEqual(sorted(self.calls("pane close")), ["pane close p1", "pane close p2"])
        self.assertEqual(self.calls("plugin")[-1],
            "plugin pane open --plugin speak --entrypoint paragraphs --placement split --direction right"
            " --cwd /work/proj --env SPEAK_TARGET_PANE=w1 --focus --target-pane w1")
        self.assertEqual(json.loads(speak.PANES_FILE.read_text()), {})
```

Add after `class PickTest`:

```python
class RunParagraphsTest(unittest.TestCase):
    ANSWER = "Interim note.\n\nThe real answer.\n\n```sh\nmake\n\nmake test\n```"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.answer = self.tmp / "answer.md"
        self.answer.write_text(self.ANSWER)
        self.chosen = self.tmp / "chosen.md"
        for patch in (mock.patch.object(speak, "ANSWER_FILE", self.answer),
                      mock.patch.object(speak, "CHOSEN_FILE", self.chosen)):
            patch.start()
            self.addCleanup(patch.stop)

    def run_picker(self, fzf=None, error=None):
        self.fzf_calls = []

        def run(command, *args, **kwargs):
            self.fzf_calls.append((command, kwargs))
            if error:
                raise error
            return fzf

        with mock.patch.object(speak.subprocess, "run", side_effect=run), \
                mock.patch.multiple(speak, stop=mock.DEFAULT, start_worker=mock.DEFAULT, notify=mock.DEFAULT) as used:
            speak.run_paragraphs()
        return used

    def test_every_paragraph_is_offered_numbered(self):
        self.run_picker(subprocess.CompletedProcess([], 130, stdout=""))
        [(command, kwargs)] = self.fzf_calls
        self.assertEqual(command, speak.paragraph_command())
        self.assertEqual(kwargs["input"], speak.paragraph_input(speak.paragraphs(self.ANSWER)))

    def test_enter_speaks_the_ticked_paragraphs_in_order(self):
        used = self.run_picker(subprocess.CompletedProcess([], 0, stdout="2\x001\x00"))
        self.assertEqual(self.chosen.read_text(), "The real answer.\n\n```sh\nmake\n\nmake test\n```")
        used["stop"].assert_called_once_with()
        used["start_worker"].assert_called_once_with(["--answer", str(self.chosen)])

    def test_escape_speaks_nothing(self):
        used = self.run_picker(subprocess.CompletedProcess([], 130, stdout=""))
        used["start_worker"].assert_not_called()
        self.assertFalse(self.chosen.exists())

    def test_a_missing_fzf_is_reported(self):
        used = self.run_picker(error=FileNotFoundError("fzf"))
        used["notify"].assert_called_once_with("Speak: fzf not found", "Install fzf to pick paragraphs.")
        used["start_worker"].assert_not_called()

    def test_a_missing_answer_is_reported(self):
        self.answer.unlink()
        used = self.run_picker(subprocess.CompletedProcess([], 0, stdout="0\x00"))
        self.assertEqual(used["notify"].call_args.args[0], "Speak: nothing to read")
        self.assertEqual(self.fzf_calls, [])

    def test_all_are_ticked_and_space_toggles_without_a_search_box(self):
        command = speak.paragraph_command()
        for flag in ("--multi", "--read0", "--print0", "--no-input"):
            self.assertIn(flag, command)
        binds = [command[i + 1] for i, arg in enumerate(command) if arg == "--bind"]
        self.assertIn("load:select-all", binds)
        self.assertIn("space:toggle", binds)
        self.assertEqual(command[command.index("--accept-nth") + 1], "1")

    def test_the_cli_runs_it(self):
        with mock.patch.object(speak, "run_paragraphs") as run:
            self.assertEqual(speak.main(["speak.py", "paragraphs"]), 0)
        run.assert_called_once_with()
```

Add to `ManifestTest`:

```python
    def test_the_paragraphs_pane_is_a_split_running_speak_paragraphs(self):
        [pane] = [p for p in self.manifest["panes"] if p["id"] == "paragraphs"]
        self.assertEqual(pane["placement"], "split")
        self.assertTrue(pane["command"][-1].endswith('speak.py" paragraphs'))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_speak.RunParagraphsTest tests.test_speak.PanesTest tests.test_speak.ManifestTest -v`
Expected: the `RunParagraphsTest` tests error with `AttributeError: … has no attribute 'ANSWER_FILE'`. The new `PanesTest` test errors on `open_paragraphs`. The manifest test fails with `ValueError: not enough values to unpack`.

- [ ] **Step 3: Implement**

In `speak.py`, add the two paths after `PANES_FILE`:

```python
ANSWER_FILE = STATE_DIR / "answer.md"  # the last answer, for the paragraph picker
CHOSEN_FILE = STATE_DIR / "chosen.md"  # the paragraphs picked from it, for the worker
```

Change `open_pane` to take `focus`. Its signature, docstring and the `--no-focus` line become:

```python
def open_pane(entrypoint, direction, target, cwd, env, focus=False):
    """Open one of this plugin's panes as a split, without taking focus unless focus, and return its id."""
```

```python
    args.append("--focus" if focus else "--no-focus")
```

Add a new section after `open_picker`:

```python
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
    """
    return [
        "fzf", "--multi", "--read0", "--print0", "--no-input", "--layout", "reverse",
        "--delimiter", "\t", "--with-nth", "2..", "--accept-nth", "1",
        "--wrap", "--gap", "--highlight-line",
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
```

In `main`, add a branch after `pick`:

```python
    elif command == "paragraphs":
        run_paragraphs()
```

Add `paragraphs` to the usage line: `"usage: {argv[0]} toggle [--verbatim] | stop | plan | selection | pick | paragraphs | player | captions | preparing"`.

In `herdr-plugin.toml`, after the `plan-picker` pane, add:

```toml
# Opened by the plan picker's "Last answer", focused, to the right of the pane the
# key was pressed in: the answer's paragraphs, all ticked. Enter speaks the ticked
# ones as a selection; Esc closes it.
[[panes]]
id = "paragraphs"
title = "Speak: pick paragraphs · space tick · enter speak · esc cancel"
placement = "split"
command = ["sh", "-c", "exec python3 \"$HERDR_PLUGIN_ROOT/speak.py\" paragraphs"]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest discover -s tests`
Expected: `OK`, 139 tests.

- [ ] **Step 5: Commit**

```bash
git add speak.py herdr-plugin.toml tests/test_speak.py
git commit -m "Add a paragraph picker pane that speaks the ticked paragraphs of the last answer"
```

---

### Task 5: Choose Files or Last answer on prefix+shift+f, and document it

**Files:**
- Modify: `speak.py:848-880` (`pick` → `choose_mode`, `pick`, `pick_plan`, `pick_answer`), module docstring `speak.py:1-11`
- Modify: `herdr-plugin.toml` (the `plan` action title and the `plan-picker` pane title and comment)
- Modify: `README.md` (intro, the `speak.plan` section, the action table, the key description)
- Test: `tests/test_speak.py` (`PickTest` calls `pick_plan`; new `PickModeTest`, `PickAnswerTest`)

**Interfaces:**
- Consumes: `last_answer(pane_id)` (Task 1), `ANSWER_FILE` and `open_paragraphs(cwd)` (Task 4).
- Produces:
  - `MODES = ["Files", "Last answer"]`.
  - `choose_mode() -> str | None`: the picked mode, or `None` on Esc. Raises `OSError` without fzf.
  - `pick()`: chooser, then `pick_plan()` or `pick_answer()`.
  - `pick_plan()`: today's `pick()` body, unchanged.
  - `pick_answer()`: saves `last_answer(SPEAK_TARGET_PANE)` to `ANSWER_FILE` and calls `open_paragraphs(Path.cwd())`.

- [ ] **Step 1: Write the failing tests**

In `PickTest`, change the two `speak.pick()` calls (in `pick` and in `test_a_missing_fzf_is_reported`) to `speak.pick_plan()`.

Add after `PickTest`:

```python
class PickModeTest(unittest.TestCase):
    def pick(self, stdout="", returncode=0, error=None):
        self.calls = []

        def run(command, *args, **kwargs):
            self.calls.append((command, kwargs))
            if error:
                raise error
            return subprocess.CompletedProcess(command, returncode, stdout=stdout)

        with mock.patch.object(speak.subprocess, "run", side_effect=run), \
                mock.patch.multiple(speak, pick_plan=mock.DEFAULT, pick_answer=mock.DEFAULT,
                                    notify=mock.DEFAULT) as used:
            speak.pick()
        return used

    def test_offers_files_then_the_last_answer_without_a_search_box(self):
        self.pick(returncode=130)
        [(command, kwargs)] = self.calls
        self.assertEqual(command[0], "fzf")
        self.assertIn("--no-input", command)
        self.assertEqual(kwargs["input"], "Files\nLast answer")

    def test_files_runs_the_plan_picker(self):
        used = self.pick("Files\n")
        used["pick_plan"].assert_called_once_with()
        used["pick_answer"].assert_not_called()

    def test_last_answer_runs_the_paragraph_picker(self):
        used = self.pick("Last answer\n")
        used["pick_answer"].assert_called_once_with()
        used["pick_plan"].assert_not_called()

    def test_escape_runs_neither(self):
        used = self.pick(returncode=130)
        used["pick_plan"].assert_not_called()
        used["pick_answer"].assert_not_called()

    def test_a_missing_fzf_is_reported(self):
        used = self.pick(error=FileNotFoundError("fzf"))
        used["notify"].assert_called_once_with("Speak: fzf not found", "Install fzf to pick a plan.")
        used["pick_plan"].assert_not_called()


class PickAnswerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.answer = self.tmp / "answer.md"
        patch = mock.patch.object(speak, "ANSWER_FILE", self.answer)
        patch.start()
        self.addCleanup(patch.stop)

    def pick(self, last_answer):
        with mock.patch.dict(os.environ, {"SPEAK_TARGET_PANE": "agent-1"}), \
                mock.patch.object(speak.Path, "cwd", return_value=Path("/work/proj")), \
                mock.patch.multiple(speak, last_answer=last_answer, open_paragraphs=mock.DEFAULT,
                                    notify=mock.DEFAULT) as used:
            speak.pick_answer()
        return used

    def test_the_key_panes_answer_is_saved_and_the_paragraph_picker_opens(self):
        last_answer = mock.Mock(return_value="Note.\n\nAnswer.")
        used = self.pick(last_answer)
        last_answer.assert_called_once_with("agent-1")
        self.assertEqual(self.answer.read_text(), "Note.\n\nAnswer.")
        used["open_paragraphs"].assert_called_once_with(Path("/work/proj"))

    def test_no_answer_is_reported_and_opens_nothing(self):
        used = self.pick(mock.Mock(side_effect=RuntimeError("The agent hasn't written an answer yet.")))
        used["notify"].assert_called_once_with("Speak: nothing to read", "The agent hasn't written an answer yet.")
        used["open_paragraphs"].assert_not_called()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_speak.PickTest tests.test_speak.PickModeTest tests.test_speak.PickAnswerTest -v`
Expected: `PickTest` errors on `pick_plan` (`AttributeError`). `PickModeTest` errors because `mock.patch.multiple` can't find `pick_plan`. `PickAnswerTest` errors on `pick_answer`.

- [ ] **Step 3: Implement**

In `speak.py`, rename today's `pick` to `pick_plan` and keep its body unchanged. Give it this docstring: `"""Choose a plan with fzf, then speak it."""`. Add above it:

```python
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
```

Replace the module docstring's second sentence group so it reads:

```python
"""Read the focused herdr pane's last agent answer, a chosen plan file, or selected text aloud via Kokoro.

`speak.py toggle [--verbatim]` starts speaking, or stops speech already in
progress. `speak.py stop` only stops. `speak.py plan` stops speech in progress,
or opens a picker pane (`speak.py pick`) that asks for files or the last
answer. A picked plan is spoken and saved for replay. The last answer opens a
paragraph picker (`speak.py paragraphs`) whose ticked paragraphs are spoken
like a selection. `speak.py selection` cancels a selection being prepared, or
rewrites the selected text and shows it beside the pane with live captions
(`speak.py captions`) and a player (`speak.py player`). The work runs in a
detached worker so the herdr action returns at once.
"""
```

In `herdr-plugin.toml`, change the `plan` action's comment and title:

```toml
# Press to pick a markdown plan, or paragraphs of the last answer, and hear it;
# press again while it is preparing to cancel.
[[actions]]
id = "plan"
title = "Speak: pick a plan file or last-answer paragraphs and read them aloud (again to cancel)"
```

Change the `plan-picker` pane's comment and title:

```toml
# Opened by the plan action in the focused pane's cwd, so it reaches speak.py
# through HERDR_PLUGIN_ROOT rather than a relative path. It asks for files or
# the last answer first.
[[panes]]
id = "plan-picker"
title = "Speak: files or last answer"
```

In `README.md`:

1. In the intro, replace `Press \`prefix+shift+f\` to pick a markdown plan and hear it.` with `Press \`prefix+shift+f\` to pick a markdown plan, or the paragraphs of the last answer you want, and hear it.`

2. Replace the start of the `speak.plan` paragraph, `\`speak.plan\` opens an fzf picker over the focused pane. It lists the markdown files …`, with:

```markdown
`speak.plan` opens an fzf picker over the focused pane that asks for **Files** or **Last answer**. Files lists the markdown files under the pane's working directory and in `~/.claude/plans`, newest first, with a preview. The picker skips hidden and dependency folders (such as `node_modules`) and looks at most six levels deep.
```

3. After the paragraph ending `…sptlrx runs with its own settings from the plugin state directory, so your own sptlrx config is left alone.`, add:

```markdown
Last answer is for when `speak.last` reads too much, such as the agent's interim notes between tool calls. A pane opens to the right of the agent's pane, where the audio panes go, replacing any that are open, and takes focus. It shows the answer split into paragraphs at blank lines, with each fenced code block kept whole, and every paragraph ticked. Move with the arrow keys, press Space to untick or tick a paragraph, and press Enter to hear the ticked ones in their original order. Esc closes the pane and does nothing. Enter does nothing while nothing is ticked. While the agent is working, the paragraphs come from its last finished answer, as with `speak.last`. After Enter, the spinner takes the pane's place, and the picked text is spoken exactly like a selection: it is rewritten with `prompt-plan.md`, saved as `spoken/<project>/selection-<date>-<time>`, and shown with live captions and a player. Pressing the key again while it prepares cancels it.
```

4. Replace the `speak.plan` table row with:

```markdown
| `speak.plan` | `prefix+shift+f` | Pick a markdown plan, or paragraphs of the last answer, prepare the audio, then show it beside the pane with live captions and a player. Invoke again to cancel preparing. |
```

5. In the Install block, change the `prefix+shift+f` description to `"speak: pick a plan file or last-answer paragraphs and read them aloud (again to cancel)"`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest discover -s tests`
Expected: `OK`, 146 tests.

- [ ] **Step 5: Commit**

```bash
git add speak.py herdr-plugin.toml README.md tests/test_speak.py
git commit -m "Ask for files or the last answer on prefix+shift+f, and speak picked paragraphs"
```

---

### Task 6: Check it in herdr (manual, commit only a fix)

The plugin is linked from the main checkout (`herdr plugin link ~/Projects/herdr-speak`), so this check runs from the worktree through a temporary link. Ask the user before changing the link. Run `herdr plugin link <this worktree>`, then `herdr server reload-config`, and afterwards restore the link with `herdr plugin link ~/Projects/herdr-speak`. If the user would rather check after landing the branch, hand them this list instead.

- [ ] **Step 1: Last answer.** In a Claude pane whose last turn had notes between tool calls, press `prefix+shift+f` and pick **Last answer**. Expected: the overlay closes, and a pane opens to the right, focused, listing the paragraphs, all ticked. Code blocks are whole, and any open text, captions and player panes are gone.
  - If focus stays in the agent pane, herdr returned focus there when the overlay closed. Then add `herdr("plugin", "pane", "focus", os.environ["HERDR_PANE_ID"])` as the first line of `run_paragraphs()` (the picker pane's own id), and recheck. Commit as `Focus the paragraph picker after the chooser overlay closes`.
- [ ] **Step 2: Picking.** Use the arrows and Space to untick the interim notes, then press Enter. Expected: the picker closes and the spinner shows "Preparing the answer…". Then the text pane shows only the ticked paragraphs, with captions under it and a playing player under that. `spoken/<project>/selection-*.txt` holds only those paragraphs.
- [ ] **Step 3: Cancel and Esc.** Repeat, and press `prefix+shift+f` while the spinner shows. Expected: it cancels, and no `selection-*` files appear. Repeat again and press Esc in the paragraph picker. Expected: it closes and nothing happens.
- [ ] **Step 4: Nothing ticked.** Untick every paragraph and press Enter. Expected: nothing happens, and the picker stays open.
- [ ] **Step 5: Files and selection are unchanged.** Press `prefix+shift+f` and pick **Files**. Expected: today's plan picker, then a plan with captions and a player. Select text in copy mode and press `prefix+shift+a`. Expected: as before.
- [ ] **Step 6: Run the tests one last time**

Run: `python3 -m unittest discover -s tests`
Expected: `OK`.
