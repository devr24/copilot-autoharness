import sys
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from harness.config import Config
from harness.errors import HarnessError
from harness.reflection import _extract_object, auto_learn, learn
from harness.skills import promote, trust_skill
from harness.storage import Event, Storage


class StorageReflectionTests(unittest.TestCase):
    def test_migrates_existing_proposal_database(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    """CREATE TABLE proposals (
                       id TEXT PRIMARY KEY, action TEXT NOT NULL, scope TEXT NOT NULL,
                       name TEXT NOT NULL, description TEXT NOT NULL, reason TEXT NOT NULL,
                       confidence REAL NOT NULL, skill_md TEXT NOT NULL,
                       evidence_json TEXT NOT NULL, status TEXT NOT NULL,
                       created_at TEXT NOT NULL, cwd TEXT NOT NULL)"""
                )
                connection.commit()
            finally:
                connection.close()
            Storage(database)
            connection = sqlite3.connect(database)
            try:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(proposals)")}
            finally:
                connection.close()
            self.assertIn("risk_level", columns)
            self.assertIn("source_sessions_json", columns)

    def test_stores_events_and_selects_latest_session(self):
        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "state.db")
            storage.add_event(Event("s", "sessionStart", "2026-01-01T00:00:00+00:00", str(Path("/repo")), None, None, {}))
            cwd = str(Path("/repo"))
            self.assertEqual(storage.latest_session(cwd), "s")
            self.assertEqual(len(storage.session_events("s", cwd)), 1)

    def test_refuses_to_learn_below_threshold(self):
        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "state.db")
            storage.add_event(Event("s", "sessionStart", "2026-01-01T00:00:00+00:00", str(Path("/repo")), None, None, {}))
            with self.assertRaisesRegex(HarnessError, "threshold"):
                learn(
                    storage,
                    Config(home=Path(directory), mode="auto", tool_calls_before_learn=1),
                    Path("/repo"),
                )

    def test_extracts_json_object(self):
        self.assertEqual(_extract_object('Result: {"action":"ignore"}'), {"action": "ignore"})

    def test_learn_creates_pending_proposal_from_reflector_output(self):
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            storage = Storage(cwd / "state.db")
            storage.add_event(Event("s", "sessionStart", "2026-01-01T00:00:00+00:00", str(cwd), None, None, {}))
            storage.add_event(Event("s", "postToolUse", "2026-01-01T00:00:01+00:00", str(cwd), "bash", "success", {}))
            output = {
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
                "import json, sys; prompt = sys.stdin.read(); "
                "assert 'evidence below was redacted' in prompt; "
                "print(json.dumps(" + repr(output) + "))"
            )
            config = Config(
                home=cwd,
                mode="review",
                tool_calls_before_learn=1,
                reflect_command=(sys.executable, "-c", code),
            )
            result = learn(storage, config, cwd)
            self.assertEqual(result["action"], "create")
            self.assertEqual(storage.proposal(result["id"])["status"], "pending")
            self.assertEqual(storage.proposal(result["id"])["source_session_ids"], ["s"])

    def test_auto_learn_promotes_on_threshold_and_marks_proposal_accepted(self):
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            storage = Storage(cwd / "state.db")
            storage.add_event(Event("s", "sessionStart", "2026-01-01T00:00:00+00:00", str(cwd), None, None, {}))
            storage.add_event(Event("s", "postToolUse", "2026-01-01T00:00:01+00:00", str(cwd), "bash", "success", {}))
            output = {
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
                "import json, sys; assert 'Existing Harness-owned skills' in sys.stdin.read(); "
                "print(json.dumps(" + repr(output) + "))"
            )
            config = Config(
                home=cwd,
                mode="auto",
                tool_calls_before_learn=1,
                reflect_command=(sys.executable, "-c", code),
            )
            proposal, skill_path = auto_learn(storage, config, cwd, "s")
            self.assertEqual(proposal["action"], "create")
            self.assertTrue((skill_path / "SKILL.md").exists())
            self.assertEqual(storage.proposal(proposal["id"])["status"], "accepted")

    def test_auto_learn_skips_short_sessions(self):
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            storage = Storage(cwd / "state.db")
            storage.add_event(Event("s", "sessionStart", "2026-01-01T00:00:00+00:00", str(cwd), None, None, {}))
            config = Config(home=cwd, mode="auto", tool_calls_before_learn=1)
            self.assertEqual(auto_learn(storage, config, cwd, "s"), (None, None))

    def test_sensitive_auto_learn_creates_candidate_without_live_skill(self):
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            storage = Storage(cwd / "state.db")
            storage.add_event(Event("s", "sessionStart", "2026-01-01T00:00:00+00:00", str(cwd), None, None, {}))
            storage.add_event(Event("s", "postToolUse", "2026-01-01T00:00:01+00:00", str(cwd), "bash", "success", {}))
            output = {
                "action": "create",
                "scope": "project",
                "name": "production-deployment",
                "description": "Deploy to production safely.",
                "reason": "A repeatable deployment procedure was discovered.",
                "confidence": 0.9,
                "skill_md": (
                    "---\nname: production-deployment\n"
                    "description: Deploy to production safely.\n---\n\n# Deployment\n"
                ),
                "target_name": "",
            }
            code = "import json; print(json.dumps(" + repr(output) + "))"
            config = Config(
                home=cwd,
                mode="auto",
                tool_calls_before_learn=1,
                reflect_command=(sys.executable, "-c", code),
            )
            candidate, path = auto_learn(storage, config, cwd, "s")
            self.assertEqual(candidate["risk_level"], "sensitive")
            self.assertIsNone(path)
            self.assertFalse((cwd / ".github" / "skills" / candidate["name"]).exists())
            self.assertEqual(storage.proposal(candidate["id"])["status"], "pending")
            self.assertEqual(storage.proposal(candidate["id"])["source_session_ids"], ["s"])

    def test_auto_patch_of_trusted_skill_waits_for_review(self):
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            existing = {
                "id": "created",
                "action": "create",
                "scope": "project",
                "name": "build-workflow",
                "description": "Build and test the repository.",
                "reason": "A repeatable build workflow was verified.",
                "confidence": 0.9,
                "risk_level": "low",
                "skill_md": (
                    "---\nname: build-workflow\n"
                    "description: Build and test the repository.\n---\n\n# Build\n"
                ),
                "evidence": ["event:old"],
            }
            skill_path = promote(existing, cwd)
            trust_skill("build-workflow", "project", cwd)
            original_body = (skill_path / "SKILL.md").read_text(encoding="utf-8")

            storage = Storage(cwd / "state.db")
            storage.add_event(Event("s", "sessionStart", "2026-01-01T00:00:00+00:00", str(cwd), None, None, {}))
            storage.add_event(Event("s", "postToolUse", "2026-01-01T00:00:01+00:00", str(cwd), "bash", "success", {}))
            patch = {
                "action": "patch",
                "scope": "project",
                "name": "build-workflow",
                "target_name": "build-workflow",
                "description": "Build and test the repository.",
                "reason": "A new validation step was confirmed.",
                "confidence": 0.9,
                "skill_md": (
                    "---\nname: build-workflow\n"
                    "description: Build and test the repository.\n---\n\n# Build\nRun one more check.\n"
                ),
            }
            code = "import json; print(json.dumps(" + repr(patch) + "))"
            config = Config(
                home=cwd,
                mode="auto",
                tool_calls_before_learn=1,
                reflect_command=(sys.executable, "-c", code),
            )
            candidate, path = auto_learn(storage, config, cwd, "s")
            self.assertEqual(candidate["action"], "patch")
            self.assertIsNone(path)
            self.assertEqual((skill_path / "SKILL.md").read_text(encoding="utf-8"), original_body)
            self.assertEqual(storage.proposal(candidate["id"])["status"], "pending")


if __name__ == "__main__":
    unittest.main()
