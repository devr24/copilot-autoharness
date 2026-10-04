import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from harness.background import reflect_session, should_reflect_mid_session
from harness.config import Config
from harness.errors import HarnessError
from harness.reflection import build_reflection_prompt, learn
from harness.storage import Event, Storage
from harness.transcript import read_conversation


def _record(kind, content, stamp):
    return json.dumps({"type": kind, "data": {"content": content}, "timestamp": stamp})


def _write_transcript(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TranscriptTests(unittest.TestCase):
    def test_extracts_and_redacts_conversation_only(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            _write_transcript(path, [
                json.dumps({"type": "system.message", "data": {"content": "SYSTEM PROMPT"}}),
                _record("user.message", "Always run pytest -q. token=abc12345secret", "2026-01-01T00:00:00Z"),
                json.dumps({"type": "model.response", "data": {"content": "noise"}}),
                _record("assistant.message", "", "2026-01-01T00:00:01Z"),
                _record("assistant.message", "Understood, using pytest -q.", "2026-01-01T00:00:02Z"),
                "not json",
            ])
            messages = read_conversation(path)
        self.assertEqual([m["role"] for m in messages], ["user", "assistant"])
        self.assertNotIn("abc12345secret", messages[0]["text"])
        self.assertFalse(any("SYSTEM" in m["text"] or "noise" in m["text"] for m in messages))

    def test_since_filters_and_budget_keeps_first_and_latest(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            lines = [_record("user.message", "goal", "2026-01-01T00:00:00Z")]
            lines += [
                _record("assistant.message", f"step {i} " + "x" * 300, f"2026-01-01T00:01:{i:02d}Z")
                for i in range(30)
            ]
            _write_transcript(path, lines)
            bounded = read_conversation(path, max_total=1500)
            windowed = read_conversation(path, since="2026-01-01T00:01:25Z")
        self.assertEqual(bounded[0]["text"], "goal")
        self.assertTrue(bounded[-1]["text"].startswith("step 29"))
        self.assertLess(sum(len(m["text"]) for m in bounded), 1700)
        self.assertEqual(len(windowed), 4)

    def test_missing_or_wrong_file_is_empty(self):
        self.assertEqual(read_conversation(None), [])
        self.assertEqual(read_conversation(Path("missing.jsonl")), [])
        self.assertEqual(read_conversation(Path("events.txt")), [])

    def test_prompt_includes_conversation(self):
        prompt = build_reflection_prompt(
            "s", [], [], [{"role": "user", "text": "use uv, never pip"}]
        )
        self.assertIn("[user] use uv, never pip", prompt)


class WindowedReflectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        root = Path(self.directory.name)
        self.project = root / "repo"
        self.project.mkdir()
        self.transcript = root / "events.jsonl"
        self.prompts = root / "prompts.txt"
        skill = {
            "action": "create", "scope": "project", "name": "uv-workflow",
            "description": "Use uv for Python dependency management.",
            "reason": "The user corrected pip usage.", "confidence": 0.9,
            "skill_md": "---\nname: uv-workflow\ndescription: Use uv for Python dependency management.\n---\n\n# uv\n",
            "target_name": "",
        }
        code = (
            "import json, sys; p=sys.stdin.read(); "
            f"open({str(self.prompts)!r}, 'a', encoding='utf-8').write(p + '\\n=====\\n'); "
            "print(json.dumps(" + repr(skill) + "))"
        )
        self.config = Config(
            home=root / "home", mode="auto", tool_calls_before_learn=2,
            reflect_command=(sys.executable, "-c", code), reflect_timeout=30,
        )
        self.storage = Storage(self.config.database)

    def tearDown(self):
        self.directory.cleanup()

    def _tool(self, n):
        self.storage.add_event(Event(
            "s1", "postToolUse", "2026-01-01T00:00:00+00:00", str(self.project),
            "bash", "success", {"toolArgs": {"command": f"cmd{n}"}},
        ))

    def _stop(self):
        self.storage.add_event(Event(
            "s1", "agentStop", "2026-01-01T00:00:05+00:00", str(self.project), None, None,
            {"transcriptPath": str(self.transcript)},
        ))

    def test_reflection_uses_conversation_and_advances_window(self):
        _write_transcript(self.transcript, [
            _record("user.message", "Stop using pip, use uv always.", "2026-01-01T00:00:00Z"),
            _record("assistant.message", "Switching to uv.", "2026-01-01T00:00:01Z"),
        ])
        self._tool(1), self._tool(2), self._stop()
        message = reflect_session(self.storage, self.config, self.project, "s1")
        self.assertIn("Learned uv-workflow", message)
        self.assertIn("[user] Stop using pip, use uv always.", self.prompts.read_text(encoding="utf-8"))
        # The consumed window does not re-trigger until two more tool events arrive.
        self.assertFalse(should_reflect_mid_session(self.storage, self.config, self.project, "s1"))
        self._tool(3)
        self.assertFalse(should_reflect_mid_session(self.storage, self.config, self.project, "s1"))
        self._tool(4)
        self.assertTrue(should_reflect_mid_session(self.storage, self.config, self.project, "s1"))

    def test_force_reflects_below_threshold_and_plain_run_does_not(self):
        self._tool(1)
        self.assertIsNone(reflect_session(self.storage, self.config, self.project, "s1"))
        self.assertFalse(self.prompts.exists())
        with self.assertRaises(HarnessError):
            learn(self.storage, self.config, self.project, "s1")
        self.assertIn(
            "Learned", reflect_session(self.storage, self.config, self.project, "s1", force=True)
        )

    def test_falls_back_to_captured_prompts_without_transcript(self):
        self.storage.add_event(Event(
            "s1", "userPromptSubmitted", "2026-01-01T00:00:00+00:00", str(self.project),
            None, None, {"prompt": "prefer uv over pip"},
        ))
        self._tool(1), self._tool(2)
        reflect_session(self.storage, self.config, self.project, "s1")
        self.assertIn("[user] prefer uv over pip", self.prompts.read_text(encoding="utf-8"))

    def test_lone_surrogates_do_not_break_reflector_input(self):
        _write_transcript(self.transcript, [
            json.dumps({
                "type": "user.message",
                "data": {"content": "bad \udc9d char, use uv"},
                "timestamp": "2026-01-01T00:00:00Z",
            }),
        ])
        self._tool(1), self._tool(2), self._stop()
        self.assertIn("Learned", reflect_session(self.storage, self.config, self.project, "s1"))

    def test_failure_sets_cooldown_and_keeps_window(self):
        broken = Config(
            home=self.config.home, mode="auto", tool_calls_before_learn=2,
            reflect_command=(sys.executable, "-c", "import sys; sys.exit(3)"),
            reflect_timeout=30,
        )
        self._tool(1), self._tool(2)
        with self.assertRaises(HarnessError):
            reflect_session(self.storage, broken, self.project, "s1")
        self.assertEqual(self.storage.reflection_mark("s1", str(self.project))[0], 0)
        self.assertFalse(should_reflect_mid_session(self.storage, broken, self.project, "s1"))

    def test_mid_session_can_be_disabled(self):
        quiet = Config(
            home=self.config.home, mode="auto", tool_calls_before_learn=1,
            reflect_command=self.config.reflect_command, reflect_during_session=False,
        )
        self._tool(1)
        self.assertFalse(should_reflect_mid_session(self.storage, quiet, self.project, "s1"))


class AgentStopTriggerTests(unittest.TestCase):
    def test_agent_stop_spawns_background_reflection(self):
        import contextlib, io
        from argparse import Namespace
        from harness.cli import _handle

        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "repo"
            home = root / "home"
            project.mkdir(), home.mkdir()
            (home / "config.toml").write_text(
                'mode = "auto"\n[reflection]\ntool_calls = 1\n'
                f"command = {json.dumps([sys.executable, '-c', 'pass'])}\n",
                encoding="utf-8",
            )
            Storage(home / "state.sqlite3").add_event(Event(
                "s9", "postToolUse", "2026-01-01T00:00:00+00:00", str(project),
                "bash", "success", {"toolArgs": {}},
            ))
            payload = json.dumps({
                "sessionId": "s9", "timestamp": 1_767_225_600_000, "cwd": str(project),
                "transcriptPath": str(root / "events.jsonl"), "stopReason": "end_turn",
            })
            with patch.dict("os.environ", {"COPILOT_HARNESS_HOME": str(home)}), \
                 patch("sys.stdin", io.StringIO(payload)), \
                 patch("harness.cli.spawn_background_reflection") as spawn, \
                 contextlib.redirect_stdout(io.StringIO()):
                code = _handle(Namespace(command="hook", event="agentStop"))
            self.assertEqual(code, 0)
            spawn.assert_called_once()
            self.assertEqual(spawn.call_args.args[1], "s9")


class SubagentAndForkTests(unittest.TestCase):
    def test_subagent_records_are_skipped(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            sub = lambda kind, text: json.dumps(
                {"type": kind, "agentId": "a1", "data": {"content": text}, "timestamp": "2026-01-01T00:00:01Z"}
            )
            _write_transcript(path, [
                _record("user.message", "Find what a.txt contains please.", "2026-01-01T00:00:00Z"),
                sub("user.message", "Locate a file named a.txt in the working directory"),
                sub("assistant.message", "Searching with glob now"),
                _record("assistant.message", "a.txt contains: hello world", "2026-01-01T00:00:02Z"),
            ])
            messages = read_conversation(path)
        self.assertEqual([m["text"] for m in messages],
                         ["Find what a.txt contains please.", "a.txt contains: hello world"])

    def test_skip_hashes_drop_only_long_known_messages(self):
        from harness.transcript import message_hash

        long_text = "Always run the tests with PYTHONPATH=src before committing."
        with TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            _write_transcript(path, [
                _record("user.message", long_text, "2026-01-01T00:00:00Z"),
                _record("user.message", "ok thanks", "2026-01-01T00:00:01Z"),
                _record("user.message", "A brand new instruction about linting.", "2026-01-01T00:00:02Z"),
            ])
            known = {message_hash({"role": "user", "text": long_text})}
            messages = read_conversation(path, skip=known)
        self.assertEqual([m["text"] for m in messages], ["ok thanks", "A brand new instruction about linting."])
        self.assertIsNone(message_hash({"role": "user", "text": "ok thanks"}))


class ForkedSessionTests(WindowedReflectionTests):
    def test_forked_history_is_not_reflected_twice(self):
        old = "Stop using pip, use uv always for this repository."
        _write_transcript(self.transcript, [_record("user.message", old, "2026-01-01T00:00:00Z")])
        self._tool(1), self._tool(2), self._stop()
        learn(self.storage, self.config, self.project, "s1", force=True)
        # A forked session copies the parent's history and then adds a new message.
        fork = self.transcript.parent / "fork.jsonl"
        _write_transcript(fork, [
            _record("user.message", old, "2026-01-01T00:00:00Z"),
            _record("user.message", "Also pin the Python version to 3.12 in pyproject.", "2026-01-02T00:00:00Z"),
        ])
        for n in (1, 2):
            self.storage.add_event(Event(
                "s2", "postToolUse", "2026-01-02T00:00:00+00:00", str(self.project), "bash", "success",
                {"toolArgs": {"command": f"c{n}"}},
            ))
        self.storage.add_event(Event(
            "s2", "agentStop", "2026-01-02T00:00:05+00:00", str(self.project), None, None,
            {"transcriptPath": str(fork)},
        ))
        learn(self.storage, self.config, self.project, "s2", force=True)
        prompts = self.prompts.read_text(encoding="utf-8").split("\n=====\n")
        self.assertIn(old, prompts[0])
        self.assertNotIn(old, prompts[1])
        self.assertIn("pin the Python version", prompts[1])

if __name__ == "__main__":
    unittest.main()
