# Speak a Selection with Live Captions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Select part of an agent's answer in a herdr pane, press a key, and hear it rewritten. Three panes then open beside it: the whole rewrite, live captions that highlight the sentence being spoken, and a player for pausing and seeking.

**Architecture:** A new `speak.selection` action reads `selected_text` from `HERDR_PLUGIN_CONTEXT_JSON` and starts the detached worker with `--selection`. The worker runs the existing `render()` with `prompt-plan.md`. `render()` now gets each chunk's audio and word timings from Kokoro-FastAPI's `/dev/captioned_speech`. It saves `.txt`, `.md`, `.lrc` (one line per sentence, at its real start time) and `.opus` under `spoken/<project>/selection-<stamp>`. The worker then opens a column of three panes to the right of the key pane:
- the `.md` rendered by glow (`glow -t`, a new `text-view` pane);
- sptlrx showing the `.lrc` as captions (a new `captions` pane);
- mpv playing the `.opus` (the existing `plan-player` pane).

sptlrx follows mpv's position over MPRIS through the `mpv-mpris` plugin. It uses a config the plugin writes, whose lyrics folder holds only this selection's `.lrc`.

**Tech Stack:** Python 3.14 standard library only (no pip packages), `unittest`, `ffmpeg` with libopus, herdr 0.9.1 plugin manifest, Kokoro-FastAPI 0.9.0 (`/dev/captioned_speech`), sptlrx 1.2.3, mpv-mpris 1.2 and glow 2.1.1 from apt, mpv 0.41.

**Spec:** Taskwarrior task `c5e5991e-3818-470c-8459-a7877d6b1805`. Read it with `task rc.json.array=on c5e5991e-3818-470c-8459-a7877d6b1805 export`; its description and annotations are the spec. The three annotations starting "Revised" replace the earlier kew, `.lrc`-format and sentence-timing decisions. Where they conflict, the revised ones win.

## Global Constraints

- `speak.selection` runs `render()` on the selected text with `prompt-plan.md` (`PLAN_PROMPT`) and `rewriteTimeout` (default 60). Pressing the key again while it is preparing cancels it (`stop()`).
- Selections are saved under `spoken_dir(config)` as `spoken/<project>/selection-<YYYY-MM-DD-HHMMSS>.*`. `<project>` comes from the focused pane's cwd by `spoken_location`'s rule: the main repository folder name (the same for every worktree), or the folder name outside git. Each run keeps its own files.
- Each selection saves `.txt` (the raw selection), `.md` (the spoken rewrite), `.lrc` (timings) and `.opus`. The audio appears last, and a cancelled or failed run leaves no partial files.
- Every `Recording` (plans included) writes `<base>.lrc` before the `.opus` appears. Timings come from Kokoro-FastAPI `/dev/captioned_speech` word timestamps: one line per sentence, stamped `[mm:ss.cc]` with its start. A chunk's first sentence starts where the chunk's audio starts (PCM bytes / 48000 s). Later sentences start at the chunk start plus the `start_time` of the first word after a sentence-ending token. If the sentence ends Kokoro reports don't line up with the text's, the whole chunk is one line at its start. This happens because Kokoro spells out numbers and abbreviations, for example `e.g.` becomes the token `e-g-`.
- A selection opens three panes in a column to the right of the key pane: the `.md` rendered by glow (`text-view`, running `glow -t "$SPEAK_TEXT"`), sptlrx captions under it (`captions`), and mpv under that (`plan-player`), playing. Plans keep their nvim `plan-view` pane. `panes.json` roles are `plan` (the text pane, whether a plan's nvim or a selection's glow), `captions` and `player`. A selection closes any plan or selection panes, and a plan closes any selection panes.
- The glow pane shows what you hear: the spoken rewrite (`<base>.md`), exactly the sentences sent to Kokoro, one paragraph per chunk. It never shows the raw selection (`<base>.txt`), which is saved only as a record.
- sptlrx runs with a plugin-owned config (`--config STATE_DIR/sptlrx.yaml`): `player: mpris`, `mpris.players: [mpv]`, `updateInterval: 250`, and `local.folder` set to `STATE_DIR/captions`, which holds only a symlink to this selection's `.lrc`. The user's own sptlrx config is never touched.
- If no selection text arrives, show a `Speak: nothing selected` notification. A clipboard fallback is out of scope unless Task 1 shows herdr drops `selected_text` (then stop and report).
- Out of scope: moving `speak.plan` onto captions, listing saved selections in the plan picker, word-level highlighting, selecting text outside herdr panes.
- Python standard library only. Match `speak.py`'s style: module-level functions, short docstrings, `notify()` for user-facing errors, `# noqa: BLE001` on deliberate broad excepts.
- Suggested key: `prefix+shift+r` (free in `~/.config/herdr/config.toml`).

## Background the engineer needs

- **herdr plugins.** `herdr-plugin.toml` declares `[[actions]]` (run on a key press, with the plugin folder as cwd) and `[[panes]]` (entrypoints opened as terminal panes, which reach plugin files through `$HERDR_PLUGIN_ROOT` with `sh -c`). herdr 0.9.1 action contexts are `global`, `workspace`, `tab`, `pane` and `selection`. `HERDR_PLUGIN_CONTEXT_JSON` includes `focused_pane_cwd` and `selected_text` (a string or null) when they're available. `HERDR_PANE_ID` is the pane the key was pressed in. `open_pane()` in `speak.py` wraps `herdr plugin pane open … --placement split --direction right|down --target-pane <id> --no-focus` and returns the new pane id. A split halves its target pane. Docs: https://raw.githubusercontent.com/herdrdev/herdr/v0.9.3/docs/next/website/src/content/docs/plugins.mdx
- **Worker model.** `start_worker(args)` spawns `speak.py worker *args` in a new session and writes its PID to `STATE_DIR/worker.pid`. `stop()` SIGTERMs that process group. The worker inherits the action's environment (`HERDR_PLUGIN_CONTEXT_JSON`, `HERDR_PANE_ID`), so it reads the selection and the key pane from its own env. SIGTERM skips `finally` blocks, which is why `Recording` writes audio to `*.opus.part` and renames it last.
- **Kokoro `/dev/captioned_speech`.** This endpoint sits at the server root, not under `/v1`: `http://127.0.0.1:8880/dev/captioned_speech` for the default `baseUrl` `http://127.0.0.1:8880/v1`. The request is the `/v1/audio/speech` body plus `"stream": false, "return_timestamps": true`. It was checked against the running server (API 0.9.0). The reply is one JSON object: `{"audio": "<base64 of 24 kHz mono s16le PCM when response_format is pcm>", "audio_format": "pcm", "timestamps": [{"word": "Then", "start_time": 4.0, "end_time": 4.15}, …]}`. Times are seconds into that reply's audio. Punctuation comes as its own tokens (`"."`, `"!"`, `"?"`, `","`). Words are normalized: `$5` becomes `five`, `dollars`, and `e.g.` becomes `e-g-`. The first `start_time` can be slightly negative (-0.02). Docs: README "Timestamps (word level)" at https://github.com/remsky/Kokoro-FastAPI
- **sptlrx 1.2.3** (`sudo apt install sptlrx`, source https://github.com/raitonoberu/sptlrx):
  - It draws the current line in the middle of the pane, with earlier lines above and later lines below. Every line is wrapped to the pane width, so nothing is cut off.
  - It parses lines matching `[mm:ss.cc]text` with exactly two minute digits, and has no length limit.
  - With `player: mpris`, it polls the player's position every `updateInterval` ms and interpolates between polls, so pauses and seeks are followed.
  - With `local.folder`, it picks the `.lrc` whose name best matches the MPRIS artist and title. mpv's title is the file name, and sptlrx strips the extension.
  - `q` quits. Keys are otherwise irrelevant.
  - All of this was checked on this machine with the extracted debs: the right file was chosen next to a decoy, a seek showed up within about 0.1 s, pausing held the line, and a 230-character line wrapped in a 40-column pane.
- **mpv-mpris 1.2** (`sudo apt install mpv-mpris`) installs `/etc/mpv/scripts/mpris.so`, so every mpv loads it automatically and `player_command()` needs no new flag.
- **Tests.** `python3 -m unittest discover -s tests -v` from the repo root (52 pass today). The tests use real `ffmpeg`, `sh`/`cat` as a fake player, and fake `claude`/`herdr` scripts. `tests/test_speak.py` sets a throwaway state dir and a failing fake `herdr` before importing `speak`. `FAKE_HERDR_PANES` is a fake herdr that logs every call to `$FAKE_LOG`, tracks live panes, and numbers new panes `p<live + closed + 1>`, so fresh panes come out `p1`, `p2`, …

## File Structure

| File | Change | Responsibility |
| --- | --- | --- |
| `speak.py` | Modify | `synthesize_captioned`, `caption_lines`, `lrc_stamp`, `lrc_text`; `Recording` gains `.lrc` and an optional `.txt`; `render` uses captioned speech and gains `keep_source`; `show_selection`, `sptlrx_config`, `run_captions`; `show_plan` closes captions too; `plugin_context`, `selection_base`, `speak_selection`, `worker_selection`; CLI `selection`, `captions` and `worker --selection`. |
| `herdr-plugin.toml` | Modify | The `selection` action, the `text-view` and `captions` panes, and `min_herdr_version = "0.9.1"`. |
| `tests/test_speak.py` | Modify | New tests, plus updating existing render tests to fake `synthesize_captioned`. |
| `README.md` | Modify | The new action, key, panes, saved files, `.lrc` for plans, and the sptlrx and mpv-mpris requirements. |

`speak.py` stays one file, as the repo is a single-script plugin.

---

### Task 1: Probe herdr's selection context (manual, nothing committed)

This task needs the user at the keyboard: they must select text, press keys, and install packages. Ask them to do each step and report back. Record the result with `task c5e5991e-3818-470c-8459-a7877d6b1805 annotate "Probe: ..."`. Later tasks assume the **expected** results. If a result differs, stop and report back instead of improvising.

**Files:** temporary edit to `herdr-plugin.toml` in the checkout herdr has linked (see `herdr plugin list`; normally `~/Projects/herdr-speak`), reverted at the end.

- [ ] **Step 1: Install the packages**

Ask the user to run `! sudo apt install sptlrx mpv-mpris glow`. Then check: `sptlrx --version` (1.2.3), `glow --version` (2.1.1) and `ls -l /etc/mpv/scripts/mpris.so`.

- [ ] **Step 2: Add a throwaway action that logs the context**

Append to the linked checkout's `herdr-plugin.toml`:

```toml
[[actions]]
id = "probe"
title = "Speak: probe selection context"
contexts = ["pane", "selection"]
command = ["sh", "-c", "{ date; env | grep ^HERDR_; echo; } >> \"$HERDR_PLUGIN_STATE_DIR/probe.log\""]
```

Ask the user to add this key to `~/.config/herdr/config.toml` and run `herdr server reload-config`:

```toml
[[keys.command]]
key = "prefix+shift+r"
type = "plugin_action"
command = "speak.probe"
description = "speak: probe"
```

- [ ] **Step 3: Have the user trigger it four ways, then read the log**

1. Select text in an agent pane with the mouse, then press `prefix+shift+r`.
2. Select text, then choose "Speak: probe selection context" from herdr's selection menu (ask the user how herdr 0.9.1 shows it).
3. Repeat 1 with `copy_on_select = false` in `~/.config/herdr/config.toml` (then reload-config), and restore it afterwards.
4. Press `prefix+shift+r` with nothing selected.

Run: `cat ~/.local/state/herdr/plugins/speak/probe.log`

Expected:
- In 1–3, `HERDR_PLUGIN_CONTEXT_JSON` contains `"selected_text":"<the text>"` and `focused_pane_cwd`, and `HERDR_PANE_ID` is the pane with the selection.
- In 4, `selected_text` is null or absent.
- If 1 lacks `selected_text` only when `copy_on_select` is on, stop: that is the spec's clipboard-fallback case.
- Note which contexts expose it. Task 4 uses `contexts = ["pane", "selection"]` unless one of them doesn't.

- [ ] **Step 4: Revert the probe**

Remove the `probe` action (`git diff` in that checkout must be empty) and have the user remove the temporary key. The real key is added in Task 4.

---

### Task 2: Captioned speech and timed `.lrc` in every recording

**Files:**
- Modify: `speak.py` (imports; new functions after `synthesize` at line 361; constants and functions after `AUDIO_FORMAT` at line 363; `Recording` at lines 371-430; `render` at lines 576-595)
- Modify: `tests/test_speak.py` (new tests; existing `PlayFileTest`, `WorkerPlanTest`, `RenderTest` fakes)
- Modify: `README.md`

**Interfaces:**
- Consumes: `sentences()`, `rewrite_stream()`, `spoken_file(base, suffix)` (existing).
- Produces:
  - `speak.synthesize_captioned(text: str, config: dict) -> tuple[bytes, list[dict]]`: 24 kHz mono s16le PCM and Kokoro's word timings. Raises `urllib.error.URLError` (an `OSError`) when the server fails, and `RuntimeError` on a malformed reply.
  - `speak.BYTES_PER_SECOND = 48000`
  - `speak.caption_lines(start: float, text: str, words: list[dict]) -> list[tuple[float, str]]`
  - `speak.lrc_stamp(seconds: float) -> str`, for example `"[01:23.46]"`
  - `speak.lrc_text(captions: list[tuple[float, str]]) -> str`
  - `speak.Recording(base, source: str | None = None)`, with `.lyrics` (`<base>.lrc`) and `.raw` (`<base>.txt`), and `add_text(text, words=())`
  - `speak.render(text, config, base, prompt=PLAN_PROMPT, timeout=None, keep_source=False)`. With `keep_source`, it also saves `text` verbatim as `<base>.txt`.

- [ ] **Step 1: Write the failing tests**

Near the top of `tests/test_speak.py`, add `import base64`, `import http.server` and `import threading` to the imports (alphabetical). After `fake_synthesize`, add:

```python
def fake_captioned(text, config):
    return b"\0" * 4800, []  # 0.1 s of silence, no word timings


# Word timings Kokoro returned for "It costs $5, e.g. about 3.5 times more. Then we stop! Why?"
KOKORO_WORDS = [
    {"word": w, "start_time": t} for w, t in [
        ("It", -0.02), ("costs", 0.11), ("five", 0.48), ("dollars", 0.81), (",", 1.38),
        ("e-g-", 1.48), ("about", 1.8), ("three", 2.05), ("point", 2.28), ("five", 2.56),
        ("times", 2.88), ("more", 3.26), (".", 3.83), ("Then", 4.0), ("we", 4.15),
        ("stop", 4.27), ("!", 4.78), ("Why", 4.92), ("?", 5.58),
    ]
]
```

Update the existing tests that render so they fake the new call:

- In `PlayFileTest.test_saved_audio_is_decoded_into_the_player`, change `synthesize=fake_synthesize` to `synthesize_captioned=fake_captioned`.
- In `WorkerPlanTest.run_worker`, add `synthesize_captioned=fake_captioned,` to `fakes`, and keep `synthesize=fake_synthesize` for the fallback reading.
- Change `WorkerPlanTest.test_a_kokoro_failure_is_reported_and_nothing_plays` to:

```python
    def test_a_kokoro_failure_is_reported_and_nothing_plays(self):
        def down(text, config):
            raise speak.urllib.error.URLError("connection refused")

        used = self.run_worker(synthesize_captioned=down)
        self.assertEqual(self.titles(used["notify"])[-1], "Speak: TTS failed")
        used["play"].assert_not_called()
        used["open_player"].assert_not_called()
        self.assertFalse(self.audio.exists())
```

- In `RenderTest`, replace the `render` helper, and in `test_an_encoder_that_dies_midway_raises_and_saves_nothing` replace `synthesize=big` (and delete the `big` function) with `synthesize_captioned=lambda text, config: (b"\0" * 1_000_000, [])`:

```python
    def render(self, rewrite, captioned=fake_captioned, **kwargs):
        with mock.patch.multiple(speak, rewrite_stream=rewrite, synthesize_captioned=captioned):
            speak.render("# Plan", self.config, self.base, timeout=5, **kwargs)
```

Add at the end of `test_saves_the_text_and_audio`:

```python
        self.assertFalse(speak.spoken_file(self.base, ".txt").exists())
```

Add these tests to `RenderTest`:

```python
    def test_each_chunk_without_timings_is_one_lrc_line_at_its_audio_start(self):
        # sentences() cuts after "here. " (20 chars); each chunk is 0.1 s of fake audio.
        self.render(lambda text, config, prompt, timeout: iter(["First step is here. ", "Second step."]))
        self.assertEqual(speak.spoken_file(self.base, ".lrc").read_text(),
                         "[00:00.00]First step is here.\n[00:00.10]Second step.\n")

    def test_kokoro_timings_split_a_chunk_into_sentences(self):
        def captioned(text, config):
            words = [{"word": "One", "start_time": 0.0}, {"word": ".", "start_time": 0.3},
                     {"word": "Two", "start_time": 0.5}, {"word": ".", "start_time": 0.8}]
            return b"\0" * 48000, words

        self.render(lambda text, config, prompt, timeout: iter(["One. Two."]), captioned=captioned)
        self.assertEqual(speak.spoken_file(self.base, ".lrc").read_text(), "[00:00.00]One.\n[00:00.50]Two.\n")

    def test_keep_source_saves_the_raw_text(self):
        self.render(lambda text, config, prompt, timeout: iter(["Spoken."]), keep_source=True)
        self.assertEqual(speak.spoken_file(self.base, ".txt").read_text(), "# Plan")

    def test_the_audio_appears_after_every_other_file(self):
        seen, real_replace = set(), Path.replace

        def replace(path, target):
            seen.update(p.name for p in self.base.parent.iterdir())
            return real_replace(path, target)

        with mock.patch.object(speak.Path, "replace", replace):
            self.render(lambda text, config, prompt, timeout: iter(["Spoken."]), keep_source=True)
        self.assertEqual(seen, {"plan.txt", "plan.md", "plan.lrc", "plan.opus.part"})

    def test_a_failed_rewrite_keeping_the_source_saves_nothing(self):
        def broken(text, config, prompt, timeout):
            yield "First step. "
            raise RuntimeError("claude exited 1")

        with self.assertRaises(RuntimeError):
            self.render(broken, keep_source=True)
        self.assertEqual(list(self.base.parent.iterdir()), [])
```

Add new classes after `RenderTest`:

```python
class CaptionLinesTest(unittest.TestCase):
    TEXT = "It costs $5, e.g. about 3.5 times more.\nThen we stop! Why?"

    def test_sentences_start_at_kokoros_word_after_each_sentence_end(self):
        self.assertEqual(speak.caption_lines(10.0, self.TEXT, KOKORO_WORDS), [
            (10.0, "It costs $5, e.g. about 3.5 times more."),
            (14.0, "Then we stop!"),
            (14.92, "Why?"),
        ])

    def test_sentence_ends_that_dont_line_up_make_one_line(self):
        text = "Mr. Smith left. Then we stop! Why?"  # "Mr. Smith" splits here but not in Kokoro's tokens
        self.assertEqual(speak.caption_lines(10.0, text, KOKORO_WORDS), [(10.0, text)])

    def test_no_timings_make_one_flattened_line(self):
        self.assertEqual(speak.caption_lines(2.5, "One.\n\nTwo  three.", []), [(2.5, "One. Two three.")])

    def test_a_single_sentence_is_one_line_whatever_the_timings(self):
        self.assertEqual(speak.caption_lines(0.0, "Just one.", KOKORO_WORDS), [(0.0, "Just one.")])


class LrcTest(unittest.TestCase):
    def test_stamps_are_minutes_seconds_and_hundredths(self):
        self.assertEqual(speak.lrc_stamp(0), "[00:00.00]")
        self.assertEqual(speak.lrc_stamp(83.456), "[01:23.46]")
        self.assertEqual(speak.lrc_stamp(59.999), "[01:00.00]")

    def test_one_line_per_caption(self):
        self.assertEqual(speak.lrc_text([(0.0, "One."), (1.5, "Two.")]), "[00:00.00]One.\n[00:01.50]Two.\n")


class SynthesizeCaptionedTest(unittest.TestCase):
    def setUp(self):
        self.requests = []
        test = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                test.requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                body = test.reply
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        port = self.server.server_address[1]
        self.config = dict(speak.DEFAULTS, baseUrl=f"http://127.0.0.1:{port}/v1", voice="af_bella")

    def test_asks_the_dev_endpoint_for_pcm_with_timestamps(self):
        words = [{"word": "Hi", "start_time": 0.0, "end_time": 0.2}]
        self.reply = json.dumps({"audio": base64.b64encode(b"\1\2\3\4").decode(), "timestamps": words}).encode()
        audio, got = speak.synthesize_captioned("Hi.", self.config)
        self.assertEqual((audio, got), (b"\1\2\3\4", words))
        [(path, body)] = self.requests
        self.assertEqual(path, "/dev/captioned_speech")
        self.assertEqual(body, {"model": "kokoro", "voice": "af_bella", "input": "Hi.", "speed": 1.0,
                                "response_format": "pcm", "stream": False, "return_timestamps": True})

    def test_a_reply_without_audio_raises(self):
        self.reply = b'{"detail": "nope"}'
        with self.assertRaises(RuntimeError):
            speak.synthesize_captioned("Hi.", self.config)

    def test_a_reply_that_isnt_json_raises(self):
        self.reply = b"<html>"
        with self.assertRaises(RuntimeError):
            speak.synthesize_captioned("Hi.", self.config)
```

`speak.DEFAULTS["model"]` is `"kokoro"` and `speed` is `1.0`; the test builds its config from `DEFAULTS`, so those values hold.

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest discover -s tests -v 2>&1 | tail -30`
Expected: FAIL/ERROR:
- `AttributeError: module 'speak' has no attribute 'synthesize_captioned'`, which also breaks the patched existing tests;
- the same for `caption_lines` and `lrc_stamp`;
- `TypeError … 'keep_source'`.

- [ ] **Step 3: Implement**

Add `import base64` (first, before `glob`) to the imports in `speak.py`.

After `synthesize()`:

```python
def synthesize_captioned(text, config):
    """(PCM, word timings) for text from Kokoro's captioned speech endpoint.

    The PCM is 24 kHz mono s16le like synthesize(); each timing is a dict with
    word, start_time and end_time in seconds into that PCM.
    """
    parts = urllib.parse.urlsplit(config["baseUrl"])
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
        f"{parts.scheme}://{parts.netloc}/dev/captioned_speech",
        data=body, headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        try:
            reply = json.load(response)
            return base64.b64decode(reply["audio"]), list(reply.get("timestamps") or [])
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError(f"Kokoro's captioned speech reply had no audio ({error!r})") from error
```

After `AUDIO_FORMAT = [...]`:

```python
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
```

Replace the `Recording` docstring, `__init__`, `add_text`, `write` and `finish` (leave `start` and `abort` as they are):

```python
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
```

```python
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
        self.partial.replace(self.audio)
        self.finished = True
```

Replace `render`:

```python
def render(text, config, base, prompt=PLAN_PROMPT, timeout=None, keep_source=False):
    """Rewrite text for listening and save the speech as <base>.md, .lrc and .opus, unplayed.

    With keep_source, text itself is saved as <base>.txt. Raises when the
    rewrite or Kokoro fails, and then saves nothing.
    """
    recording = Recording(base, source=text if keep_source else None)
    recording.start()
    try:
        for sentence in sentences(rewrite_stream(text, config, prompt, timeout)):
            audio, words = synthesize_captioned(sentence, config)
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
```

- [ ] **Step 4: Run the whole suite**

Run: `python3 -m unittest discover -s tests -v`
Expected: all PASS.

- [ ] **Step 5: Check it against the real Kokoro server**

Kokoro is running locally (`curl -s http://127.0.0.1:8880/health`). Run:

```bash
python3 -c "
import speak
audio, words = speak.synthesize_captioned('First we add it. Then we test it!', speak.load_config())
print(len(audio) / speak.BYTES_PER_SECOND, 's')
for t, s in speak.caption_lines(0.0, 'First we add it. Then we test it!', words): print(speak.lrc_stamp(t) + s)
"
```

Expected: about 2–3 s of audio and two lines, `[00:00.00]First we add it.` and `[00:01.xx]Then we test it!`. If Kokoro isn't running, note that and move on.

- [ ] **Step 6: Update the README**

In `README.md`, change the sentence beginning "The spoken text and audio are saved as `spoken/<project>/<name>.md` and `.opus`" so it begins:

```markdown
The spoken text, its captions and the audio are saved as `spoken/<project>/<name>.md`, `.lrc` and `.opus` in the plugin state directory.
```

Keep the rest of that sentence and paragraph. Directly after that sentence, add:

```markdown
The `.lrc` has one line per spoken sentence, stamped with the time Kokoro started saying it (from Kokoro-FastAPI's `/dev/captioned_speech` word timings), so players such as sptlrx and mpv can show the text in step with the audio.
```

- [ ] **Step 7: Commit**

```bash
git add speak.py tests/test_speak.py README.md
git commit -m "Save sentence captions from Kokoro's word timings beside every recording"
```

---

### Task 3: The three-pane view: text, captions and player

**Files:**
- Modify: `speak.py` (`load_panes` docstring at line 734; `show_plan` at lines 783-798; new functions after `run_player` at line 838; `main`)
- Modify: `herdr-plugin.toml` (new `text-view` and `captions` panes)
- Test: `tests/test_speak.py` (`PanesTest`; new `RunCaptionsTest`)

**Interfaces:**
- Consumes: `load_panes()`, `save_panes()`, `close_pane(panes, role)`, `open_pane(entrypoint, direction, target, cwd, env) -> str | None`, `spoken_file`, `notify` (existing); the `.md`, `.lrc` and `.opus` that `render()` saves (Task 2).
- Produces:
  - `speak.show_selection(base: Path) -> None` opens `text-view` (`SPEAK_TEXT=<base>.md`) right of `$HERDR_PANE_ID`, then `captions` (`SPEAK_LYRICS=<base>.lrc`) below it, then `plan-player` (`SPEAK_AUDIO=<base>.opus`) below that. It records them as roles `plan`, `captions` and `player`, each `{"pane": id, "plan": "<base>.md"}`, after closing all three roles.
  - `speak.sptlrx_config(folder: Path) -> str`
  - `speak.run_captions() -> None` execs `sptlrx --config STATE_DIR/sptlrx.yaml` after making `STATE_DIR/captions/` hold only a symlink to `$SPEAK_LYRICS`.
  - `show_plan` also closes the `captions` role.
  - CLI: `speak.py captions`.

- [ ] **Step 1: Write the failing tests**

In `PanesTest.setUp`, add `"HERDR_PANE_ID": "w2",` to `self.env`. Then add to `PanesTest`:

```python
    def test_a_selection_opens_its_text_captions_and_player_in_a_column(self):
        speak.show_selection(Path("/s/proj/selection-1"))
        self.assertEqual(self.calls("plugin"), [
            "plugin pane open --plugin speak --entrypoint text-view --placement split --direction right"
            " --cwd /s/proj --env SPEAK_TEXT=/s/proj/selection-1.md --no-focus --target-pane w2",
            "plugin pane open --plugin speak --entrypoint captions --placement split --direction down"
            " --cwd /s/proj --env SPEAK_LYRICS=/s/proj/selection-1.lrc --no-focus --target-pane p1",
            "plugin pane open --plugin speak --entrypoint plan-player --placement split --direction down"
            " --cwd /s/proj --env SPEAK_AUDIO=/s/proj/selection-1.opus --no-focus --target-pane p2",
        ])
        entry = {"plan": "/s/proj/selection-1.md"}
        self.assertEqual(json.loads(speak.PANES_FILE.read_text()), {
            "plan": dict(entry, pane="p1"), "captions": dict(entry, pane="p2"), "player": dict(entry, pane="p3"),
        })

    def test_a_selection_replaces_an_open_plan_and_its_player(self):
        speak.show_plan("/work/a.md")
        speak.open_player("/work/a.md", Path("/s/a.opus"), paused=False)
        speak.show_selection(Path("/s/proj/selection-1"))
        self.assertEqual(sorted(self.calls("pane close")), ["pane close p1", "pane close p2"])

    def test_a_new_selection_replaces_the_last_one(self):
        speak.show_selection(Path("/s/proj/selection-1"))
        speak.show_selection(Path("/s/proj/selection-2"))
        self.assertEqual(sorted(self.calls("pane close")), ["pane close p1", "pane close p2", "pane close p3"])

    def test_a_plan_replaces_an_open_selection(self):
        speak.show_selection(Path("/s/proj/selection-1"))
        speak.show_plan("/work/a.md")
        self.assertEqual(sorted(self.calls("pane close")), ["pane close p1", "pane close p2", "pane close p3"])
        self.assertIn("SPEAK_PLAN=/work/a.md", self.calls("plugin")[-1])

    def test_a_selection_whose_text_pane_fails_opens_nothing_else(self):
        failing = fake_command(self.tmp, "herdr-fails", "#!/bin/sh\necho 'no such pane' >&2\nexit 1\n")
        with mock.patch.dict(os.environ, {"HERDR_BIN_PATH": str(failing)}), \
                mock.patch.object(speak, "notify") as notify:
            speak.show_selection(Path("/s/proj/selection-1"))
        notify.assert_called_once_with("Speak: couldn't open a pane", "no such pane")
```

Add after `PlayerCommandTest`:

```python
class RunCaptionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.folder = speak.STATE_DIR / "captions"
        self.config = speak.STATE_DIR / "sptlrx.yaml"

    def lrc(self, name, text="[00:00.00]Hi.\n"):
        path = self.tmp / name
        path.write_text(text)
        return path

    def run_captions(self, lyrics, **patches):
        with mock.patch.dict(os.environ, {"SPEAK_LYRICS": str(lyrics)}), \
                mock.patch.object(speak.os, "execvp", **patches) as execvp:
            speak.run_captions()
        return execvp

    def test_becomes_sptlrx_on_a_folder_holding_only_this_lrc(self):
        self.lrc("other.lrc", "[00:00.00]Wrong file.\n")
        execvp = self.run_captions(self.lrc("selection-1.lrc"))
        execvp.assert_called_once_with("sptlrx", ["sptlrx", "--config", str(self.config)])
        self.assertEqual([p.name for p in self.folder.iterdir()], ["selection-1.lrc"])
        self.assertEqual((self.folder / "selection-1.lrc").read_text(), "[00:00.00]Hi.\n")
        self.assertEqual(self.config.read_text(), speak.sptlrx_config(self.folder))

    def test_a_new_selection_replaces_the_last_in_the_folder(self):
        self.run_captions(self.lrc("selection-1.lrc"))
        self.run_captions(self.lrc("selection-2.lrc"))
        self.assertEqual([p.name for p in self.folder.iterdir()], ["selection-2.lrc"])

    def test_a_missing_sptlrx_is_reported(self):
        with mock.patch.object(speak, "notify") as notify:
            self.run_captions(self.lrc("selection-1.lrc"), side_effect=FileNotFoundError("sptlrx"))
        notify.assert_called_once_with(
            "Speak: sptlrx not found", "Install sptlrx (sudo apt install sptlrx) to show live captions.")

    def test_the_config_follows_mpv_quickly_and_reads_only_local_lyrics(self):
        text = speak.sptlrx_config(Path("/state/cap tions"))
        for line in ("player: mpris", "updateInterval: 250", "mpris:", "  players: [mpv]",
                     "local:", '  folder: "/state/cap tions"', "  hAlignment: left"):
            self.assertIn(line + "\n", text)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest tests.test_speak.PanesTest tests.test_speak.RunCaptionsTest -v`
Expected: ERROR with `AttributeError: module 'speak' has no attribute 'show_selection'` (and `run_captions`, `sptlrx_config`). `test_a_plan_replaces_an_open_selection` errors the same way.

- [ ] **Step 3: Implement**

Add `import shutil` to the imports in `speak.py`, after `import signal`.

Change the `load_panes` docstring:

```python
def load_panes():
    """The panes this plugin opened: {role: {"pane": id, "plan": path}} for roles plan, captions and player."""
```

In `show_plan`, close the captions pane with the others:

```python
    close_pane(panes, "plan")
    close_pane(panes, "captions")
    close_pane(panes, "player")
```

Also update its docstring's last sentence to: "one showing another plan or a selection is replaced, along with its captions and player."

Add after `run_player()`:

```python
def show_selection(base):
    """Show a selection's rewrite, live captions and player in a column beside the key pane.

    They replace any plan or selection panes already open, taking the plan,
    captions and player roles, and a plan picked later replaces them.
    """
    panes = load_panes()
    for role in ("plan", "captions", "player"):
        close_pane(panes, role)
    base = Path(base)
    text = str(spoken_file(base, ".md"))
    view = open_pane("text-view", "right", os.environ.get("HERDR_PANE_ID"), base.parent, {"SPEAK_TEXT": text})
    if view:
        panes["plan"] = {"pane": view, "plan": text}
        captions = open_pane("captions", "down", view, base.parent,
                             {"SPEAK_LYRICS": str(spoken_file(base, ".lrc"))})
        if captions:
            panes["captions"] = {"pane": captions, "plan": text}
            player = open_pane("plan-player", "down", captions, base.parent,
                               {"SPEAK_AUDIO": str(spoken_file(base, ".opus"))})
            if player:
                panes["player"] = {"pane": player, "plan": text}
    save_panes(panes)


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
```

In `main`, add before the final `else:`:

```python
    elif command == "captions":
        run_captions()
```

and change the usage line to:

```python
        print(f"usage: {argv[0]} toggle [--verbatim] | stop | plan | pick | player | captions", file=sys.stderr)
```

In `herdr-plugin.toml`, leave `plan-view` as it is (plans keep nvim) and append:

```toml
# Opened by speak.selection to the right of the pane the key was pressed in:
# glow renders the selection's spoken rewrite, scrollable. q closes it.
[[panes]]
id = "text-view"
title = "Speak: rewrite · j/k scroll · q close"
placement = "split"
command = ["sh", "-c", "exec glow -t \"$SPEAK_TEXT\""]

# Opened by speak.selection under the rewrite's pane: sptlrx shows the rewrite as
# captions in step with the mpv player under it, the spoken sentence in bold. q closes it.
[[panes]]
id = "captions"
title = "Speak: captions · q close"
placement = "split"
command = ["sh", "-c", "exec python3 \"$HERDR_PLUGIN_ROOT/speak.py\" captions"]
```

- [ ] **Step 4: Run the whole suite**

Run: `python3 -m unittest discover -s tests -v`
Expected: all PASS.

- [ ] **Step 5: Smoke-test sptlrx with the generated config (needs Task 1's packages)**

This checks the generated YAML against the real sptlrx and mpv, with no herdr involved. Run it from the repo root, with `S` set to a scratch folder (the session scratchpad):

```bash
S=<scratchpad>/captions-smoke; mkdir -p "$S"
ffmpeg -loglevel error -y -f lavfi -i "sine=duration=8" -c:a libopus "$S/selection-smoke.opus"
printf '[00:00.00]First line.\n[00:03.00]Second line.\n' > "$S/selection-smoke.lrc"
python3 -c "import speak, sys; from pathlib import Path; Path(sys.argv[1], 'cfg.yaml').write_text(speak.sptlrx_config(Path(sys.argv[1])))" "$S"
mpv --no-terminal --ao=null "$S/selection-smoke.opus" & MPV=$!
sleep 1; timeout 5 sptlrx --config "$S/cfg.yaml" pipe; kill $MPV
```

Expected: it prints `First line.` and then, about 2 s later, `Second line.`. If sptlrx rejects the config, fix `sptlrx_config` (and its test) before committing.

- [ ] **Step 6: Commit**

```bash
git add speak.py tests/test_speak.py herdr-plugin.toml
git commit -m "Show a selection as its text, live sptlrx captions and a player"
```

---

### Task 4: The `speak.selection` action and its worker

**Files:**
- Modify: `speak.py` (module docstring; `open_picker` at lines 840-858; new section after `worker_plan`; `main`)
- Modify: `herdr-plugin.toml` (new action, `min_herdr_version`)
- Modify: `README.md`
- Test: `tests/test_speak.py`

**Interfaces:**
- Consumes: `render(text, config, base, prompt, timeout, keep_source)` and `spoken_file` (Task 2); `show_selection(base)` (Task 3); `spoken_location(source)`, `spoken_dir(config)`, `ensure_kokoro`, `stop()`, `start_worker(args)`, `notify` (existing).
- Produces:
  - `speak.plugin_context() -> dict`
  - `speak.selection_base(cwd, config, now: float | None = None) -> Path`
  - `speak.speak_selection() -> None` (the action)
  - `speak.worker_selection() -> None`
  - CLI: `speak.py selection`, `speak.py worker --selection`

- [ ] **Step 1: Write the failing tests**

Add after `class SpokenPathsTest`:

```python
class SelectionBaseTest(unittest.TestCase):
    NOW = time.mktime((2026, 9, 30, 13, 30, 5, 0, 0, -1))

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.config = dict(speak.DEFAULTS, spokenDir=str(self.tmp / "spoken"))

    def git(self, *args):
        subprocess.run(["git", *args], check=True, capture_output=True)

    def test_named_by_date_and_time_under_the_main_repository(self):
        repo = self.tmp / "myrepo"
        self.git("init", "-q", str(repo))
        self.git("-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                 "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "init")
        self.git("-C", str(repo), "worktree", "add", "-q", str(self.tmp / "wt-feature"))
        (self.tmp / "wt-feature" / "src").mkdir()
        base = speak.selection_base(self.tmp / "wt-feature" / "src", self.config, self.NOW)
        self.assertEqual(base, self.tmp / "spoken" / "myrepo" / "selection-2026-09-30-133005")

    def test_outside_git_the_project_is_the_folder_name(self):
        (self.tmp / "notes").mkdir()
        base = speak.selection_base(self.tmp / "notes", self.config, self.NOW)
        self.assertEqual(base, self.tmp / "spoken" / "notes" / "selection-2026-09-30-133005")
```

Add after `class OpenPickerTest`:

```python
class SpeakSelectionTest(unittest.TestCase):
    def run_action(self, context, stopped=False):
        env = {"HERDR_PLUGIN_CONTEXT_JSON": json.dumps(context)}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(speak, "stop", return_value=stopped), \
                mock.patch.object(speak, "start_worker") as start, \
                mock.patch.object(speak, "notify") as notify:
            speak.speak_selection()
        return start, notify

    def test_a_selection_is_handed_to_a_worker(self):
        start, notify = self.run_action({"selected_text": "Some answer.", "focused_pane_cwd": "/w"})
        start.assert_called_once_with(["--selection"])
        notify.assert_not_called()

    def test_nothing_selected_is_reported(self):
        for context in ({}, {"selected_text": None}, {"selected_text": "  \n"}):
            start, notify = self.run_action(context)
            start.assert_not_called()
            notify.assert_called_once_with("Speak: nothing selected",
                                           "Select some text in a pane, then press the key.")

    def test_pressing_it_while_preparing_only_cancels(self):
        start, notify = self.run_action({"selected_text": "Some answer."}, stopped=True)
        start.assert_not_called()
        notify.assert_not_called()

    def test_the_cli_command_runs_the_action(self):
        with mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONTEXT_JSON": "{}"}), \
                mock.patch.object(speak, "stop", return_value=False), \
                mock.patch.object(speak, "notify") as notify:
            self.assertEqual(speak.main(["speak.py", "selection"]), 0)
        self.assertEqual(notify.call_args.args[0], "Speak: nothing selected")


class WorkerSelectionTest(unittest.TestCase):
    SELECTED = "The **selected** part of an answer."

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "proj").mkdir()
        self.config = dict(speak.DEFAULTS, spokenDir=str(self.tmp / "spoken"))
        self.rewrites = []

    def fake_rewrite(self, text, config, prompt, timeout):
        self.rewrites.append((text, prompt, timeout))
        return iter(["The selected part. Said aloud."])

    def run_worker(self, **patches):
        fakes = dict(load_config=lambda: self.config, ensure_kokoro=lambda config: None,
                     synthesize_captioned=fake_captioned, rewrite_stream=self.fake_rewrite)
        fakes.update(patches)
        mocks = {name: mock.DEFAULT for name in ("show_selection", "notify") if name not in patches}
        context = {"selected_text": self.SELECTED, "focused_pane_cwd": str(self.tmp / "proj")}
        with mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONTEXT_JSON": json.dumps(context)}), \
                mock.patch.multiple(speak, **fakes), mock.patch.multiple(speak, **mocks) as used, \
                contextlib.redirect_stderr(io.StringIO()):
            speak.worker_selection()
        return used

    def saved(self):
        folder = self.tmp / "spoken" / "proj"
        return sorted(p.name for p in folder.iterdir()) if folder.exists() else []

    def test_saves_the_selection_its_rewrite_captions_and_audio(self):
        self.run_worker()
        names = self.saved()
        self.assertEqual(len(names), 4)
        self.assertRegex(names[0], r"^selection-\d{4}-\d\d-\d\d-\d{6}\.lrc$")
        stem = names[0][:-len(".lrc")]
        self.assertEqual(names, [f"{stem}.lrc", f"{stem}.md", f"{stem}.opus", f"{stem}.txt"])
        folder = self.tmp / "spoken" / "proj"
        self.assertEqual((folder / f"{stem}.txt").read_text(), self.SELECTED)
        self.assertEqual((folder / f"{stem}.md").read_text(), "The selected part. Said aloud.\n")

    def test_rewrites_with_the_plan_prompt_and_the_answer_timeout(self):
        self.run_worker()
        self.assertEqual(self.rewrites, [(self.SELECTED, speak.PLAN_PROMPT, 60)])

    def test_shows_the_panes_once_the_audio_is_saved(self):
        def show(base):
            self.assertTrue(speak.spoken_file(base, ".opus").exists(), "the panes opened before the audio was saved")

        used = self.run_worker(show_selection=mock.Mock(side_effect=show))
        [call] = used["show_selection"].call_args_list
        self.assertRegex(call.args[0].name, r"^selection-\d{4}-\d\d-\d\d-\d{6}$")
        self.assertEqual([c.args[0] for c in used["notify"].call_args_list], ["Speak: preparing the selection"])

    def test_a_failed_rewrite_is_reported_and_saves_and_shows_nothing(self):
        def broken(text, config, prompt, timeout):
            yield "The selected part. "
            raise RuntimeError("claude exited 1")

        used = self.run_worker(rewrite_stream=broken)
        self.assertEqual(self.saved(), [])
        used["show_selection"].assert_not_called()
        self.assertEqual(used["notify"].call_args.args, ("Speak: couldn't prepare the selection", "claude exited 1"))

    def test_a_kokoro_failure_is_reported_as_tts(self):
        def down(text, config):
            raise speak.urllib.error.URLError("connection refused")

        used = self.run_worker(synthesize_captioned=down)
        self.assertEqual(used["notify"].call_args.args[0], "Speak: TTS failed")
        used["show_selection"].assert_not_called()
        self.assertEqual(self.saved(), [])

    def test_kokoro_not_starting_is_reported(self):
        def fails(config):
            raise RuntimeError("docker start failed")

        used = self.run_worker(ensure_kokoro=fails)
        self.assertEqual(used["notify"].call_args.args, ("Speak: couldn't start Kokoro", "docker start failed"))
        self.assertEqual(self.rewrites, [])
```

Add before `if __name__ == "__main__":`:

```python
class ManifestTest(unittest.TestCase):
    def setUp(self):
        import tomllib
        self.manifest = tomllib.loads((ROOT / "herdr-plugin.toml").read_text())

    def test_the_selection_action_runs_in_panes_and_the_selection_menu(self):
        [action] = [a for a in self.manifest["actions"] if a["id"] == "selection"]
        self.assertEqual(action["command"], ["python3", "speak.py", "selection"])
        self.assertEqual(sorted(action["contexts"]), ["pane", "selection"])

    def test_the_text_pane_runs_glow_on_the_rewrite(self):
        [pane] = [p for p in self.manifest["panes"] if p["id"] == "text-view"]
        self.assertEqual(pane["command"], ["sh", "-c", 'exec glow -t "$SPEAK_TEXT"'])

    def test_the_captions_pane_runs_speak_captions(self):
        [pane] = [p for p in self.manifest["panes"] if p["id"] == "captions"]
        self.assertTrue(pane["command"][-1].endswith('speak.py" captions'))

    def test_requires_the_herdr_the_selection_context_was_checked_on(self):
        self.assertEqual(self.manifest["min_herdr_version"], "0.9.1")
```

If Task 1 found that one of the two contexts doesn't expose `selected_text`, change the expected `contexts` here and in Step 3 to what the probe showed.

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest tests.test_speak.SelectionBaseTest tests.test_speak.SpeakSelectionTest tests.test_speak.WorkerSelectionTest tests.test_speak.ManifestTest -v`
Expected: ERROR with `AttributeError: module 'speak' has no attribute 'selection_base'` (likewise `speak_selection` and `worker_selection`). `ManifestTest` fails on the missing action and version.

- [ ] **Step 3: Implement**

Just above `open_picker`, add `plugin_context`, and make `open_picker` use it:

```python
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
```

Delete `open_picker`'s old `try: context = json.loads(...)` block and its old `cwd =` line. Everything from `args = [` onward is unchanged.

Add a new section after `worker_plan` and before `# --- Picking a plan ---`:

```python
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
        notify("Speak: nothing selected", "Select some text in a pane, then press the key.")
        return
    # The worker inherits this environment, so it reads the selection from it too.
    start_worker(["--selection"])


def worker_selection():
    """Rewrite the selected text and save its speech, then show it with captions and a player."""
    config = load_config()
    context = plugin_context()
    text = context.get("selected_text") or ""
    base = selection_base(context.get("focused_pane_cwd") or Path.home(), config)
    try:
        ensure_kokoro(config)
    except (OSError, RuntimeError) as error:
        notify("Speak: couldn't start Kokoro", str(error))
        return
    notify("Speak: preparing the selection", "It plays once the audio is ready. Press the key again to cancel.")
    try:
        render(text, config, base, prompt=PLAN_PROMPT, timeout=config["rewriteTimeout"], keep_source=True)
    except (OSError, RuntimeError) as error:
        if isinstance(error, urllib.error.URLError):
            notify("Speak: TTS failed", f"{config['baseUrl']}: {error}")
        else:
            notify("Speak: couldn't prepare the selection", str(error))
        return
    show_selection(base)
```

In `main`, extend the worker branch and add the action:

```python
    elif command == "worker":
        try:
            if "--plan" in argv:
                worker_plan(argv[argv.index("--plan") + 1])
            elif "--selection" in argv:
                worker_selection()
            else:
                worker(verbatim)
        finally:
            if running_worker() == os.getpid():
                PID_FILE.unlink(missing_ok=True)
    elif command == "plan":
        open_picker()
    elif command == "selection":
        speak_selection()
```

and the usage line:

```python
        print(f"usage: {argv[0]} toggle [--verbatim] | stop | plan | selection | pick | player | captions",
              file=sys.stderr)
```

Replace the module docstring at the top of `speak.py`:

```python
"""Read the focused herdr pane's last agent answer, a chosen plan file, or selected text aloud via Kokoro.

`speak.py toggle [--verbatim]` starts speaking, or stops speech already in
progress. `speak.py stop` only stops. `speak.py plan` stops speech in progress,
or opens a picker pane (`speak.py pick`) whose choice is spoken and saved for
replay. `speak.py selection` cancels a selection being prepared, or rewrites
the selected text and shows it beside the pane with live captions
(`speak.py captions`) and a player (`speak.py player`). The work runs in a
detached worker so the herdr action returns at once.
"""
```

In `herdr-plugin.toml`, change `min_herdr_version = "0.7.0"` to `min_herdr_version = "0.9.1"`, because the `selection` context and `selected_text` were checked only on 0.9.1. Insert after the `plan` action:

```toml
# Select text in a pane, then press to hear it rewritten, shown beside the pane
# with live captions and a player; press again while it is preparing to cancel.
[[actions]]
id = "selection"
title = "Speak: read the selected text aloud (again to cancel preparing)"
contexts = ["pane", "selection"]
command = ["python3", "speak.py", "selection"]
```

- [ ] **Step 4: Run the whole suite**

Run: `python3 -m unittest discover -s tests -v`
Expected: all PASS.

- [ ] **Step 5: Document it in the README**

In `README.md`:

1. At the end of the first paragraph, after "Press `prefix+shift+p` to pick a markdown plan and hear it.", add: ``Select text in a pane and press `prefix+shift+r` to hear just that part, with live captions.``
2. After the paragraph about saved files (the one Task 2 changed), add:

```markdown
`speak.selection` reads the text you selected in the focused pane. It rewrites the selection with `prompt-plan.md` and prepares the audio, which takes a few seconds; a notification says so, and pressing the key again cancels it. Then three panes open in a column to the right, without taking focus: the whole spoken rewrite rendered by [glow](https://github.com/charmbracelet/glow), live captions from [sptlrx](https://github.com/raitonoberu/sptlrx) that highlight the sentence being spoken, and the `mpv` player, already playing. Pause and seek in the player and the captions follow. `q` closes each pane. A selection replaces an open plan's panes, and picking a plan replaces the selection's. If nothing is selected, a notification says so.

Each selection is saved as `spoken/<project>/selection-<date>-<time>` with four files: `.txt` (the text you selected), `.md` (the spoken rewrite), `.lrc` (its captions) and `.opus`. The project is named the same way as for plans, from the focused pane's folder. A run that is cancelled or fails saves nothing. sptlrx runs with its own settings from the plugin state directory, so your own sptlrx config is left alone.
```

3. Add a table row after `speak.plan`:

```markdown
| `speak.selection` | Speak the selected text, rewritten for listening, and show it beside the pane with live captions and a player. Invoke again to cancel preparing. |
```

4. In Requirements, change "herdr 0.7 or newer" to "herdr 0.9.1 or newer". In the tools bullet, after "`mpv` for the replay player", add "`glow`, `sptlrx` and `mpv-mpris` for a selection's text and live captions (`sudo apt install glow sptlrx mpv-mpris`)". Add a bullet: "A Kokoro-FastAPI server with the `/dev/captioned_speech` endpoint, which gives the word timings behind the captions."
5. In Install, add after the `prefix+shift+p` block:

```toml
[[keys.command]]
key = "prefix+shift+r"
type = "plugin_action"
command = "speak.selection"
description = "speak: read the selected text aloud (again to cancel)"
```

- [ ] **Step 6: Commit**

```bash
git add speak.py tests/test_speak.py herdr-plugin.toml README.md
git commit -m "Add a speak.selection action that shows the rewrite with live captions"
```

---

### Task 5: End-to-end check in herdr (manual, with the user)

**Files:** none unless a defect turns up. If one does, fix it with a failing test first in `tests/test_speak.py`, then commit.

- [ ] **Step 1: Run the suite once more**

Run: `python3 -m unittest discover -s tests -v`
Expected: all PASS.

- [ ] **Step 2: Load the branch into herdr**

The linked plugin is normally `~/Projects/herdr-speak` (`herdr plugin list`). Ask the user how they want to try the branch: check it out there, or `herdr plugin link` this worktree for the test and link back afterwards. Ask them to add the `prefix+shift+r` key from the README and run `herdr server reload-config`.

- [ ] **Step 3: Walk through the "Done when" list with the user**

1. Select part of an agent's answer, press `prefix+shift+r`. Expected: "Speak: preparing the selection" appears, then three panes to the right: the rewrite rendered by glow, captions with the current sentence bold, and mpv playing. Focus stays in the original pane.
2. In mpv, space pauses and the arrows seek. Expected: the captions follow within about a quarter of a second.
3. Select again, press the key, and press it again while preparing. Expected: no panes open, and `ls ~/.local/state/herdr/plugins/speak/spoken/<project>/` shows no new files and no `.opus.part`.
4. After a full run, the folder has `selection-<stamp>.txt`, `.md`, `.lrc` and `.opus`. `cat` of the `.lrc` shows one line per sentence with increasing stamps.
5. Pick a plan with `prefix+shift+p`. Expected: all three selection panes close, and the plan and mpv open. Then run a selection again. Expected: the plan and its player close.
6. Press the key with nothing selected. Expected: "Speak: nothing selected".
7. `~/.config/sptlrx` is untouched (or doesn't exist).

- [ ] **Step 4: Record the outcome**

`task c5e5991e-3818-470c-8459-a7877d6b1805 annotate "Verified in herdr: <what passed, anything odd>"`
