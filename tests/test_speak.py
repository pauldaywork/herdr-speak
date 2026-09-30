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


if __name__ == "__main__":
    unittest.main()
