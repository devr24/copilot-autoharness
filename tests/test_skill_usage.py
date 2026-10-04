import json
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.cli import main
from harness.errors import HarnessError
from harness.skills import promote
from harness.storage import Storage
from harness.transcript import read_skill_invocations


def line(name, source="project", at="2026-10-04T12:38:14.500Z"):
    return json.dumps({"type": "skill.invoked", "timestamp": at, "data": {"name": name, "source": source, "pluginName": None}})


class SkillUsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_parses_only_skill_invoked_records(self):
        path = self.root / "events.jsonl"
        path.write_text(
            "\n".join([line("a"), "not json", json.dumps({"type": "user.message", "data": {"content": "x"}}),
                       line("b", source="personal"), json.dumps({"type": "skill.invoked", "data": {}})]),
            encoding="utf-8",
        )
        uses = read_skill_invocations(path)
        self.assertEqual([(u["name"], u["scope"]) for u in uses], [("a", "project"), ("b", "personal")])
        self.assertEqual(read_skill_invocations(None), [])
        self.assertEqual(read_skill_invocations(self.root / "missing.jsonl"), [])

    def test_recording_is_idempotent_and_aggregated(self):
        storage = Storage(self.root / "s.sqlite3")
        uses = [{"name": "a", "scope": "project", "at": "2026-10-04T10:00:00Z"},
                {"name": "a", "scope": "project", "at": "2026-10-05T10:00:00Z"}]
        self.assertEqual(storage.record_skill_uses("s1", uses), 2)
        self.assertEqual(storage.record_skill_uses("s1", uses), 0)
        self.assertEqual(storage.skill_usage()[("project", "a")], (2, "2026-10-05T10:00:00Z"))

    def test_feedback_command_records_rating_for_current_version(self):
        project = self.root / "repo"
        home = self.root / "harness-state"
        project.mkdir()
        proposal = {
            "id": "proposal-1",
            "action": "create",
            "scope": "project",
            "name": "build-workflow",
            "description": "Build and test the repository.",
            "reason": "A repeatable workflow was discovered.",
            "confidence": 0.9,
            "risk_level": "low",
            "skill_md": (
                "---\nname: build-workflow\n"
                "description: Build and test the repository.\n---\n\n# Build\n"
            ),
            "evidence": ["event:1"],
        }
        promote(proposal, project)
        original_cwd = Path.cwd()
        try:
            os.chdir(project)
            with (
                patch.dict(os.environ, {"COPILOT_HARNESS_HOME": str(home)}),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    main(["feedback", "build-workflow", "--rating", "helpful"]),
                    0,
                )
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(["skills", "--scope", "project"]), 0)
        finally:
            os.chdir(original_cwd)

        storage = Storage(home / "state.sqlite3")
        self.assertEqual(
            storage.skill_feedback(str(project))[str(project), "project", "build-workflow"],
            (1, 0),
        )
        with storage._connect() as connection:
            row = connection.execute(
                "SELECT version, rating FROM skill_feedback"
            ).fetchone()
        self.assertEqual((row["version"], row["rating"]), (1, "helpful"))
        self.assertIn("feedback 1 helpful/0 not helpful", output.getvalue())

    def test_feedback_rejects_unknown_rating(self):
        storage = Storage(self.root / "s.sqlite3")
        with self.assertRaisesRegex(HarnessError, "Unsupported skill feedback rating"):
            storage.record_skill_feedback(
                str(self.root), "build-workflow", "project", 1, "maybe", "2026-10-04T00:00:00Z"
            )


if __name__ == "__main__":
    unittest.main()
