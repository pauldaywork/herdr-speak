import contextlib
import io
import json
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


    def test_encoder_that_dies_midway_does_not_stop_playback(self):
        fake_command(self.tmp, "ffmpeg", "#!/bin/sh\nexit 1\n")
        out = self.tmp / "played.pcm"
        config = dict(self.config, player=["sh", "-c", f"cat > '{out}'"])

        def big(text, config):
            for _ in range(5):
                yield b"\0" * 200_000

        with mock.patch.dict(os.environ, {"PATH": f"{self.tmp}:{os.environ['PATH']}"}), \
                mock.patch.object(speak, "synthesize", big), \
                contextlib.redirect_stderr(io.StringIO()) as note:
            spoke = speak.play(["Speak this."], config, speak.Recording(self.base))
        self.assertTrue(spoke)
        self.assertEqual(note.getvalue().count("encoder failed"), 1)
        self.assertEqual(out.stat().st_size, 1_000_000)
        self.assertEqual(list(self.base.parent.iterdir()), [])


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
        ), mock.patch.object(speak, "play_file") as replay, \
                mock.patch.object(speak, "open_player") as self.player:
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

    def test_first_play_opens_the_player_paused_once_the_audio_is_saved(self):
        self.run_worker()
        self.player.assert_called_once_with(
            str(self.source.resolve()), self.tmp / "spoken" / "proj" / "plan.opus", paused=True,
        )

    def test_second_play_of_an_unchanged_file_replays_the_saved_audio(self):
        self.run_worker()
        replay = self.run_worker()
        replay.assert_called_once_with(self.tmp / "spoken" / "proj" / "plan.opus", self.config)
        self.assertEqual(len(self.prompts), 1)
        self.player.assert_not_called()

    def test_an_edited_file_is_rewritten_again(self):
        self.run_worker()
        future = time.time() + 60
        os.utime(self.source, (future, future))
        replay = self.run_worker()
        replay.assert_not_called()
        self.assertEqual(len(self.prompts), 2)


    def test_a_failed_rewrite_speaks_the_fallback_and_saves_nothing(self):
        spoken = []

        def broken(text, config, prompt, timeout):
            raise RuntimeError("claude exited 1")
            yield

        def record(text, config):
            spoken.append(text)
            yield b"\0" * 4800

        with mock.patch.multiple(
            speak,
            load_config=lambda: self.config,
            ensure_kokoro=lambda config: None,
            synthesize=record,
            rewrite_stream=broken,
        ), mock.patch.object(speak, "play_file"), \
                mock.patch.object(speak, "open_player") as player, \
                contextlib.redirect_stderr(io.StringIO()):
            speak.worker_plan(str(self.source))
        player.assert_not_called()
        self.assertEqual(spoken, [speak.strip_markdown(self.source.read_text())])
        self.assertFalse((self.tmp / "spoken").exists() and any((self.tmp / "spoken").rglob("*.*")))


    def test_a_corrupt_saved_audio_is_reported_and_replaced(self):
        self.run_worker()
        saved = self.tmp / "spoken" / "proj" / "plan.opus"
        saved.write_bytes(b"not audio")
        with mock.patch.multiple(
            speak,
            load_config=lambda: self.config,
            ensure_kokoro=lambda config: None,
            synthesize=fake_synthesize,
            rewrite_stream=self.fake_rewrite,
            notify=mock.DEFAULT,
            open_player=mock.DEFAULT,
        ) as patched, contextlib.redirect_stderr(io.StringIO()):
            speak.worker_plan(str(self.source))
        patched["open_player"].assert_called_once_with(str(self.source.resolve()), saved, paused=True)
        patched["notify"].assert_called_once()
        self.assertEqual(patched["notify"].call_args.args[0], "Speak: couldn't replay the saved audio")
        self.assertEqual(len(self.prompts), 2)
        replayed = self.tmp / "played.pcm"
        speak.play_file(saved, dict(self.config, player=["sh", "-c", f"cat > '{replayed}'"]))
        self.assertGreater(replayed.stat().st_size, 0)


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

    def test_a_new_plan_is_shown_and_handed_to_a_worker(self):
        self.pick(self.chosen())
        [(command, kwargs)] = self.fzf_calls
        self.assertIn(str(self.plan), kwargs["input"])
        self.show.assert_called_once_with(str(self.plan))
        self.start.assert_called_once_with(["--plan", str(self.plan)])
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


if __name__ == "__main__":
    unittest.main()
