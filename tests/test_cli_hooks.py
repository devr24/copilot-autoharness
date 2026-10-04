import contextlib
import io
import json
import os
import sys
import unittest
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from harness.cli import _handle
from harness.storage import Event, Storage


class SessionEndHookTests(unittest.TestCase):
    def test_session_end_hook_auto_creates_skill(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "repo"
            home = root / "harness-state"
            project.mkdir()
            skill = {
                "action": "create",
                "scope": "project",
                "name": "build-workflow",
                "description": "Build and test the repository.",
                "reason": "A repeatable test sequence was discovered.",
                "confidence": 0.9,
                "skill_md": "---\nname: build-workflow\ndescription: Build and test the repository.\n---\n\n# Build\n",
                "target_name": "",
            }
            code = (
                "import json, sys; prompt=sys.stdin.read(); "
                "assert 'toolArgs' in prompt; "
                "print(json.dumps(" + repr(skill) + "))"
            )
            command = [sys.executable, "-c", code]
            config = (
                'mode = "auto"\n'
                "[reflection]\n"
                "tool_calls = 1\n"
                "timeout_seconds = 30\n"
                f"command = {json.dumps(command)}\n"
                "[skills.project]\ncapacity = 50\n"
                "[skills.personal]\ncapacity = 20\n"
            )
            home.mkdir()
            (home / "config.toml").write_text(config, encoding="utf-8")
            storage = Storage(home / "state.sqlite3")
            storage.add_event(
                Event(
                    "session-1",
                    "postToolUse",
                    "2026-01-01T00:00:00+00:00",
                    str(project),
                    "bash",
                    "success",
                    {"toolArgs": {"command": "pytest"}},
                )
            )
            payload = json.dumps(
                {
                    "sessionId": "session-1",
                    "timestamp": 1_767_225_600_000,
                    "cwd": str(project),
                    "reason": "complete",
                }
            )
            stdout, stderr = io.StringIO(), io.StringIO()
            original_cwd = Path.cwd()
            try:
                os.chdir(project)
                with (
                    patch.dict(os.environ, {"COPILOT_HARNESS_HOME": str(home)}),
                    patch("sys.stdin", io.StringIO(payload)),
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    result = _handle(Namespace(command="hook", event="sessionEnd"))
            finally:
                os.chdir(original_cwd)

            skill_file = project / ".github" / "skills" / "build-workflow" / "SKILL.md"
            self.assertEqual(result, 0)
            self.assertTrue(skill_file.exists())
            self.assertIn("Learned build-workflow", stderr.getvalue())
            self.assertEqual(stdout.getvalue().strip(), "{}")


if __name__ == "__main__":
    unittest.main()
