import base64
import contextlib
import http.server
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
# speak.py reads these at import time; keep tests away from real plugin state.
os.environ["HERDR_PLUGIN_STATE_DIR"] = tempfile.mkdtemp()
os.environ["HERDR_PLUGIN_CONFIG_DIR"] = tempfile.mkdtemp()
# Never reach the real herdr (panes, notifications) from a test; tests that need it fake it.
_no_herdr = Path(tempfile.mkdtemp()) / "herdr"
_no_herdr.write_text("#!/bin/sh\nexit 1\n")
_no_herdr.chmod(0o755)
os.environ["HERDR_BIN_PATH"] = str(_no_herdr)
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


def fake_synthesize(text, config):
    yield b"\0" * 4800  # 0.1 s of silence


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


class PlayTest(unittest.TestCase):
    def setUp(self):
        self.config = dict(speak.DEFAULTS, player=["sh", "-c", "cat > /dev/null"])

    def play(self, chunks):
        with mock.patch.object(speak, "synthesize", fake_synthesize):
            return speak.play(chunks, self.config)

    def test_spoken_file_keeps_dots_in_the_name(self):
        self.assertEqual(speak.spoken_file(Path("proj/v1.2-plan"), ".opus").name, "v1.2-plan.opus")

    def test_every_chunk_is_spoken(self):
        self.assertTrue(self.play(["First sentence.", "Second sentence."]))

    def test_a_rewrite_that_dies_partway_keeps_what_was_spoken(self):
        def chunks():
            yield "First sentence."
            raise RuntimeError("rewrite died")

        with contextlib.redirect_stderr(io.StringIO()) as note:
            self.assertTrue(self.play(chunks()))
        self.assertIn("stopped partway", note.getvalue())


class SpokenPathsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def git(self, *args):
        subprocess.run(["git", *args], check=True, capture_output=True)

    def commit(self, repo):
        self.git("-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                 "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "init")

    def test_project_is_the_repository_name(self):
        repo = self.tmp / "myrepo"
        (repo / "docs").mkdir(parents=True)
        self.git("init", "-q", str(repo))
        self.assertEqual(speak.spoken_location(repo / "docs" / "plan.md"), ("myrepo", "docs--plan"))

    def test_project_of_a_worktree_is_the_main_repository(self):
        repo = self.tmp / "myrepo"
        self.git("init", "-q", str(repo))
        self.commit(repo)
        self.git("-C", str(repo), "worktree", "add", "-q", str(self.tmp / "wt-feature"))
        self.assertEqual(speak.spoken_location(self.tmp / "wt-feature" / "plan.md"), ("myrepo", "plan"))

    def test_project_outside_git_is_the_folder_name(self):
        (self.tmp / "plans").mkdir()
        self.assertEqual(speak.spoken_location(self.tmp / "plans" / "x.md"), ("plans", "x"))

    def test_folder_name_is_used_when_git_cannot_run(self):
        (self.tmp / "plans").mkdir()
        with mock.patch.object(speak.subprocess, "run", side_effect=FileNotFoundError("git")):
            self.assertEqual(speak.spoken_location(self.tmp / "plans" / "x.md"), ("plans", "x"))

    def test_same_file_name_in_different_folders_gets_different_bases(self):
        repo = self.tmp / "myrepo"
        (repo / "docs").mkdir(parents=True)
        self.git("init", "-q", str(repo))
        config = dict(speak.DEFAULTS, spokenDir=str(self.tmp / "spoken"))
        top = speak.spoken_base(repo / "README.md", config)
        nested = speak.spoken_base(repo / "docs" / "README.md", config)
        self.assertEqual(top, self.tmp / "spoken" / "myrepo" / "README")
        self.assertEqual(nested, self.tmp / "spoken" / "myrepo" / "docs--README")

    def test_a_worktree_file_has_the_same_base_as_in_the_main_checkout(self):
        repo = self.tmp / "myrepo"
        (repo / "docs").mkdir(parents=True)
        self.git("init", "-q", str(repo))
        self.commit(repo)
        self.git("-C", str(repo), "worktree", "add", "-q", str(self.tmp / "wt-feature"))
        (self.tmp / "wt-feature" / "docs").mkdir(exist_ok=True)
        config = dict(speak.DEFAULTS, spokenDir=str(self.tmp / "spoken"))
        self.assertEqual(
            speak.spoken_base(self.tmp / "wt-feature" / "docs" / "plan.md", config),
            speak.spoken_base(repo / "docs" / "plan.md", config),
        )

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


class PlayFileTest(unittest.TestCase):
    def test_saved_audio_is_decoded_into_the_player(self):
        tmp = Path(tempfile.mkdtemp())
        out = tmp / "played.pcm"
        config = dict(speak.DEFAULTS, player=["sh", "-c", f"cat > '{out}'"])
        with mock.patch.multiple(speak, synthesize_captioned=fake_captioned,
                                 rewrite_stream=lambda text, config, prompt, timeout: iter(["Hello."])):
            speak.render("# Hello", config, tmp / "hello", timeout=5)
        speak.play_file(tmp / "hello.opus", config)
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
        self.audio = self.tmp / "spoken" / "proj" / "plan.opus"
        self.prompts = []

    def fake_rewrite(self, text, config, prompt, timeout):
        self.prompts.append((prompt, timeout))
        return iter(["Spoken plan. It has one step."])

    def run_worker(self, **patches):
        """Run worker_plan with fakes for Claude, Kokoro and herdr; return the mocks it used."""
        fakes = dict(
            load_config=lambda: self.config,
            ensure_kokoro=lambda config: None,
            synthesize=fake_synthesize,
            synthesize_captioned=fake_captioned,
            rewrite_stream=self.fake_rewrite,
        )
        fakes.update(patches)
        mocks = dict(play_file=mock.DEFAULT, show_plan=mock.DEFAULT, open_player=mock.DEFAULT,
                     notify=mock.DEFAULT, play=mock.DEFAULT)
        mocks = {name: value for name, value in mocks.items() if name not in patches}
        with mock.patch.multiple(speak, **fakes), mock.patch.multiple(speak, **mocks) as used, \
                contextlib.redirect_stderr(io.StringIO()):
            speak.worker_plan(str(self.source))
        return used

    def titles(self, notify):
        return [call.args[0] for call in notify.call_args_list]

    def test_first_play_rewrites_with_the_plan_prompt_and_saves_outside_the_project(self):
        self.run_worker()
        self.assertEqual(self.prompts, [(speak.PLAN_PROMPT, 300)])
        folder = self.tmp / "spoken" / "proj"
        self.assertEqual((folder / "plan.md").read_text(), "Spoken plan. It has one step.\n")
        self.assertTrue(self.audio.exists())
        self.assertEqual(sorted(p.name for p in (self.tmp / "proj").iterdir()), ["plan.md"])

    def test_first_play_renders_the_audio_then_shows_the_plan_and_plays_it(self):
        def open_player(path, audio, paused):
            self.assertTrue(audio.exists(), "the player opened before the audio was saved")

        player = mock.Mock(side_effect=open_player)
        used = self.run_worker(open_player=player)
        player.assert_called_once()
        used["play"].assert_not_called()
        used["show_plan"].assert_called_once_with(str(self.source.resolve()))
        self.assertEqual(self.titles(used["notify"]), ["Speak: preparing the plan"])

    def test_the_player_starts_playing(self):
        player = mock.Mock()
        self.run_worker(open_player=player)
        player.assert_called_once_with(str(self.source.resolve()), self.audio, paused=False)

    def test_second_play_of_an_unchanged_file_replays_the_saved_audio(self):
        self.run_worker()
        used = self.run_worker()
        used["play_file"].assert_called_once_with(self.audio, self.config)
        used["open_player"].assert_not_called()
        self.assertEqual(len(self.prompts), 1)

    def test_an_edited_file_is_rewritten_again(self):
        self.run_worker()
        future = time.time() + 60
        os.utime(self.source, (future, future))
        used = self.run_worker()
        used["play_file"].assert_not_called()
        self.assertEqual(len(self.prompts), 2)

    def test_a_failed_rewrite_shows_the_plan_and_speaks_the_fallback_unsaved(self):
        spoken = []

        def broken(text, config, prompt, timeout):
            raise RuntimeError("claude exited 1")
            yield

        def record(text, config):
            spoken.append(text)
            yield b"\0" * 4800

        used = self.run_worker(rewrite_stream=broken, synthesize=record, play=speak.play)
        self.assertEqual(spoken, [speak.strip_markdown(self.source.read_text())])
        self.assertFalse((self.tmp / "spoken").exists() and any((self.tmp / "spoken").rglob("*.*")))
        used["show_plan"].assert_called_once_with(str(self.source.resolve()))
        used["open_player"].assert_not_called()

    def test_a_kokoro_failure_is_reported_and_nothing_plays(self):
        def down(text, config):
            raise speak.urllib.error.URLError("connection refused")

        used = self.run_worker(synthesize_captioned=down)
        self.assertEqual(self.titles(used["notify"])[-1], "Speak: TTS failed")
        used["play"].assert_not_called()
        used["open_player"].assert_not_called()
        self.assertFalse(self.audio.exists())

    def test_a_corrupt_saved_audio_is_reported_and_replaced(self):
        self.run_worker()
        self.audio.write_bytes(b"not audio")
        used = self.run_worker(play_file=speak.play_file)
        self.assertEqual(self.titles(used["notify"])[0], "Speak: couldn't replay the saved audio")
        used["open_player"].assert_called_once_with(str(self.source.resolve()), self.audio, paused=False)
        self.assertEqual(len(self.prompts), 2)
        replayed = self.tmp / "played.pcm"
        speak.play_file(self.audio, dict(self.config, player=["sh", "-c", f"cat > '{replayed}'"]))
        self.assertGreater(replayed.stat().st_size, 0)


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp()) / "proj" / "plan"
        self.config = dict(speak.DEFAULTS)

    def render(self, rewrite, captioned=fake_captioned, **kwargs):
        with mock.patch.multiple(speak, rewrite_stream=rewrite, synthesize_captioned=captioned):
            speak.render("# Plan", self.config, self.base, timeout=5, **kwargs)

    def test_saves_the_text_and_audio(self):
        self.render(lambda text, config, prompt, timeout: iter(["First step. Second step."]))
        self.assertEqual(speak.spoken_file(self.base, ".md").read_text(), "First step. Second step.\n")
        self.assertGreater(speak.spoken_file(self.base, ".opus").stat().st_size, 0)
        self.assertFalse(speak.spoken_file(self.base, ".txt").exists())

    def test_a_failed_rewrite_raises_and_saves_nothing(self):
        def broken(text, config, prompt, timeout):
            yield "First step. "
            raise RuntimeError("claude exited 1")

        with self.assertRaises(RuntimeError):
            self.render(broken)
        self.assertEqual(list(self.base.parent.iterdir()), [])

    def test_an_encoder_that_fails_raises_and_saves_nothing(self):
        with mock.patch.object(speak, "AUDIO_FORMAT", ["-f", "nosuchformat"]), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(RuntimeError):
            self.render(lambda text, config, prompt, timeout: iter(["Speak this."]))
        self.assertEqual(list(self.base.parent.iterdir()), [])

    def test_an_encoder_that_dies_midway_raises_and_saves_nothing(self):
        bin_dir = self.base.parent.parent
        fake_command(bin_dir, "ffmpeg", "#!/bin/sh\nexit 1\n")

        with mock.patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"}), \
                mock.patch.multiple(speak, synthesize_captioned=lambda text, config: (b"\0" * 1_000_000, []),
                                    rewrite_stream=lambda text, config, prompt, timeout: iter(["Speak this."])), \
                contextlib.redirect_stderr(io.StringIO()) as note, self.assertRaises(RuntimeError):
            speak.render("# Plan", self.config, self.base, timeout=5)
        self.assertEqual(note.getvalue().count("encoder failed"), 1)
        self.assertEqual(list(self.base.parent.iterdir()), [])

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

    def test_sigterm_mid_render_leaves_no_partial_files(self):
        child = (
            "import sys, time\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from pathlib import Path\n"
            "import speak\n"
            "def rewrite(text, config, prompt, timeout):\n"
            "    yield 'First step of the plan. Then more.'\n"
            "    time.sleep(60)\n"
            "speak.rewrite_stream = rewrite\n"
            "speak.synthesize_captioned = lambda text, config: (b'\\0' * 480000, [])\n"
            "speak.exit_on_sigterm()\n"
            "speak.render('# Plan', dict(speak.DEFAULTS), Path(sys.argv[1]), keep_source=True)\n"
        )
        env = dict(os.environ, HERDR_PLUGIN_STATE_DIR=tempfile.mkdtemp())
        process = subprocess.Popen([sys.executable, "-c", child, str(self.base)], env=env)
        try:
            deadline = time.monotonic() + 10
            part = speak.spoken_file(self.base, ".opus.part")
            while not part.exists():
                self.assertLess(time.monotonic(), deadline, "the render never started saving")
                time.sleep(0.05)
            process.terminate()
            self.assertEqual(process.wait(timeout=10), 143)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        self.assertEqual(list(self.base.parent.iterdir()), [])

    def test_a_failed_rewrite_keeping_the_source_saves_nothing(self):
        def broken(text, config, prompt, timeout):
            yield "First step. "
            raise RuntimeError("claude exited 1")

        with self.assertRaises(RuntimeError):
            self.render(broken, keep_source=True)
        self.assertEqual(list(self.base.parent.iterdir()), [])

    def test_a_failure_writing_the_captions_saves_nothing(self):
        real_write = Path.write_text

        def write_text(path, *args, **kwargs):
            if path.suffix == ".lrc":
                raise OSError("disk full")
            return real_write(path, *args, **kwargs)

        with mock.patch.object(speak.Path, "write_text", write_text), self.assertRaises(OSError):
            self.render(lambda text, config, prompt, timeout: iter(["Spoken."]), keep_source=True)
        self.assertEqual(list(self.base.parent.iterdir()), [])

    def test_an_empty_rewrite_raises_and_saves_nothing(self):
        with self.assertRaises(RuntimeError):
            self.render(lambda text, config, prompt, timeout: iter([]))
        self.assertEqual(list(self.base.parent.iterdir()), [])


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
    def test_a_file_deleted_after_the_walk_is_skipped(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        (tmp / "here.md").write_text("# x")
        lines = speak.picker_lines([tmp / "gone.md", tmp / "here.md"], tmp, tmp)
        self.assertEqual([line.split("\t")[0] for line in lines], [str(tmp / "here.md")])

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
            "HERDR_PANE_ID": "pane-7",
            "HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"focused_pane_cwd": "/work/proj"}),
        }

    def test_opens_the_overlay_picker_in_the_focused_pane_cwd(self):
        with mock.patch.dict(os.environ, self.env), mock.patch.object(speak, "stop", return_value=False):
            speak.open_picker()
        self.assertEqual(self.args.read_text().split("\n")[:-1], [
            "plugin", "pane", "open", "--plugin", "speak", "--entrypoint", "plan-picker",
            "--placement", "overlay", "--cwd", "/work/proj", "--focus",
            "--env", "SPEAK_TARGET_PANE=pane-7",
        ])

    def test_pressing_it_while_speaking_only_stops(self):
        with mock.patch.dict(os.environ, self.env), mock.patch.object(speak, "stop", return_value=True):
            speak.open_picker()
        self.assertFalse(self.args.exists())


FAKE_HERDR_PANES = """#!/bin/sh
# Logs each call and keeps a list of live panes, like enough of herdr for these tests.
echo "$*" >> "$FAKE_LOG"
case "$1 $2" in
    "pane get") grep -qx "$3" "$FAKE_LIVE" ;;
    "pane close") grep -vx "$3" "$FAKE_LIVE" > "$FAKE_LIVE.new"; mv "$FAKE_LIVE.new" "$FAKE_LIVE" ;;
    "plugin pane")
        n=$(( $(wc -l < "$FAKE_LIVE") + $(grep -c close "$FAKE_LOG") + 1 ))
        echo "p$n" >> "$FAKE_LIVE"
        printf '{"result":{"plugin_pane":{"pane":{"pane_id":"p%s"}}}}\\n' "$n" ;;
esac
"""


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

        shown = mock.Mock(side_effect=show)
        used = self.run_worker(show_selection=shown)
        [call] = shown.call_args_list
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


class PanesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.log = self.tmp / "log"
        self.live = self.tmp / "live"
        self.live.write_text("")
        self.env = {
            "HERDR_BIN_PATH": str(fake_command(self.tmp, "herdr", FAKE_HERDR_PANES)),
            "FAKE_LOG": str(self.log),
            "FAKE_LIVE": str(self.live),
            "HERDR_PLUGIN_ID": "speak",
            "SPEAK_TARGET_PANE": "w1",
            "HERDR_PANE_ID": "w2",
        }
        patches = [
            mock.patch.dict(os.environ, self.env),
            mock.patch.object(speak, "PANES_FILE", self.tmp / "panes.json"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def calls(self, prefix=""):
        return [line for line in self.log.read_text().splitlines() if line.startswith(prefix)]

    def test_a_plan_opens_read_only_beside_the_pane_the_key_was_pressed_in(self):
        speak.show_plan("/work/proj/docs/plan.md")
        self.assertEqual(self.calls("plugin"), [
            "plugin pane open --plugin speak --entrypoint plan-view --placement split"
            " --direction right --cwd /work/proj/docs --env SPEAK_PLAN=/work/proj/docs/plan.md"
            " --no-focus --target-pane w1",
        ])

    def test_the_same_plan_reuses_its_open_pane(self):
        speak.show_plan("/work/plan.md")
        speak.show_plan("/work/plan.md")
        self.assertEqual(len(self.calls("plugin")), 1)

    def test_a_plan_pane_you_closed_opens_again(self):
        speak.show_plan("/work/plan.md")
        self.live.write_text("")
        speak.show_plan("/work/plan.md")
        self.assertEqual(len(self.calls("plugin")), 2)

    def test_the_player_opens_under_the_plan(self):
        speak.show_plan("/work/plan.md")
        speak.open_player("/work/plan.md", Path("/s/plan.opus"), paused=True)
        self.assertEqual(self.calls("plugin")[-1],
            "plugin pane open --plugin speak --entrypoint plan-player --placement split"
            " --direction down --cwd /work --env SPEAK_AUDIO=/s/plan.opus --env SPEAK_PAUSE=1"
            " --no-focus --target-pane p1")

    def test_a_playing_player_has_no_pause_flag(self):
        speak.show_plan("/work/plan.md")
        speak.open_player("/work/plan.md", Path("/s/plan.opus"), paused=False)
        self.assertNotIn("SPEAK_PAUSE", self.calls("plugin")[-1])

    def test_a_new_player_replaces_the_old_one(self):
        speak.show_plan("/work/plan.md")
        speak.open_player("/work/plan.md", Path("/s/plan.opus"), paused=True)
        speak.open_player("/work/plan.md", Path("/s/plan.opus"), paused=False)
        self.assertEqual(self.calls("pane close"), ["pane close p2"])

    def test_a_different_plan_replaces_the_plan_and_player_panes(self):
        speak.show_plan("/work/a.md")
        speak.open_player("/work/a.md", Path("/s/a.opus"), paused=True)
        speak.show_plan("/work/b.md")
        self.assertEqual(sorted(self.calls("pane close")), ["pane close p1", "pane close p2"])
        self.assertIn("SPEAK_PLAN=/work/b.md", self.calls("plugin")[-1])

    def test_without_a_plan_pane_the_player_opens_beside_the_key_pane(self):
        speak.open_player("/work/plan.md", Path("/s/plan.opus"), paused=True)
        self.assertIn("--direction right", self.calls("plugin")[-1])
        self.assertTrue(self.calls("plugin")[-1].endswith("--target-pane w1"))

    def test_a_failure_is_reported(self):
        failing = fake_command(self.tmp, "herdr-fails", "#!/bin/sh\necho 'no such pane' >&2\nexit 1\n")
        with mock.patch.dict(os.environ, {"HERDR_BIN_PATH": str(failing)}), \
                mock.patch.object(speak, "notify") as notify:
            speak.show_plan("/work/plan.md")
        notify.assert_called_once_with("Speak: couldn't open a pane", "no such pane")

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


class PlayerCommandTest(unittest.TestCase):
    def test_mpv_stays_open_with_a_progress_bar(self):
        command = speak.player_command("/s/plan.opus", paused=False)
        self.assertEqual(command[0], "mpv")
        self.assertEqual(command[-1], "/s/plan.opus")
        for flag in ("--no-video", "--keep-open=yes", "--term-osd-bar"):
            self.assertIn(flag, command)
        self.assertNotIn("--pause", command)

    def test_paused_starts_paused(self):
        self.assertIn("--pause", speak.player_command("/s/plan.opus", paused=True))


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


class PickTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.plan = self.tmp / "plan.md"
        self.plan.write_text("# Plan")
        past = time.time() - 60
        os.utime(self.plan, (past, past))
        self.config = dict(speak.DEFAULTS, spokenDir=str(self.tmp / "spoken"))

    def pick(self, fzf):
        real_run, self.fzf_calls = subprocess.run, []

        def run(command, *args, **kwargs):
            if command[0] != "fzf":
                return real_run(command, *args, **kwargs)
            self.fzf_calls.append((command, kwargs))
            return fzf

        with mock.patch.object(speak, "load_config", return_value=self.config), \
                mock.patch.object(speak.Path, "cwd", return_value=self.tmp), \
                mock.patch.object(speak.subprocess, "run", side_effect=run), \
                mock.patch.object(speak, "stop"), \
                mock.patch.object(speak, "start_worker") as self.start, \
                mock.patch.object(speak, "show_plan") as self.show, \
                mock.patch.object(speak, "open_player") as self.player:
            speak.pick()

    def chosen(self):
        return subprocess.CompletedProcess([], 0, stdout=f"{self.plan}\t2026-09-30 10:00  plan.md\n")

    def test_a_new_plan_is_handed_to_a_worker_to_prepare(self):
        self.pick(self.chosen())
        [(command, kwargs)] = self.fzf_calls
        self.assertIn(str(self.plan), kwargs["input"])
        self.start.assert_called_once_with(["--plan", str(self.plan)])
        self.show.assert_not_called()  # the worker shows it once the audio is ready
        self.player.assert_not_called()

    def test_a_plan_with_saved_audio_plays_in_the_player(self):
        audio = speak.spoken_file(speak.spoken_base(self.plan, self.config), ".opus")
        audio.parent.mkdir(parents=True)
        audio.write_bytes(b"x")
        self.pick(self.chosen())
        self.show.assert_called_once_with(str(self.plan))
        self.player.assert_called_once_with(str(self.plan), audio, paused=False)
        self.start.assert_not_called()

    def test_a_missing_fzf_is_reported(self):
        with mock.patch.object(speak, "load_config", return_value=self.config), \
                mock.patch.object(speak.Path, "cwd", return_value=self.tmp), \
                mock.patch.object(speak.subprocess, "run", side_effect=FileNotFoundError("fzf")), \
                mock.patch.object(speak, "notify") as notify, \
                mock.patch.object(speak, "start_worker") as start:
            speak.pick()
        notify.assert_called_once_with("Speak: fzf not found", "Install fzf to pick a plan.")
        start.assert_not_called()

    def test_escape_starts_nothing(self):
        self.pick(subprocess.CompletedProcess([], 130, stdout=""))
        self.start.assert_not_called()
        self.show.assert_not_called()
        self.player.assert_not_called()


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


if __name__ == "__main__":
    unittest.main()
