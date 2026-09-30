import os
import subprocess
import sys
import tempfile
import time
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

    def test_encoder_failure_is_caught_and_does_not_fail_playback(self):
        # Make ffmpeg fail by using an invalid audio format.
        bad_format = ["-f", "nosuchformat"]
        with mock.patch.object(speak, "AUDIO_FORMAT", bad_format):
            # Should not raise; should return True and complete playback normally.
            result = self.play(["Speak this."], speak.Recording(self.base))
        self.assertTrue(result)
        # No audio or text files should exist since encoding failed.
        self.assertFalse((self.base.parent / "v1.2-plan.opus").exists())
        self.assertFalse((self.base.parent / "v1.2-plan.md").exists())
        self.assertFalse((self.base.parent / "v1.2-plan.opus.part").exists())


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


if __name__ == "__main__":
    unittest.main()
