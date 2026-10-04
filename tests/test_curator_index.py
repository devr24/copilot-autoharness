import contextlib
import io
import json
import sys
import unittest
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from harness.cli import _handle
from harness.config import Config, load_config
from harness.curator import consolidate, maybe_consolidate
from harness.errors import HarnessError
from harness.hooks import handle_hook
from harness.index import format_index_lines, session_start_context
from harness.reflection import build_reflection_prompt, select_relevant_skills
from harness.skills import list_skills, merge_skills, move_skill, promote, skill_index, trust_skill
from harness.storage import Event, Storage


def skill_proposal(name, description="Run the project checks.", body="Run checks."):
    return {
        "id": f"p-{name}",
        "action": "create",
        "scope": "project",
        "name": name,
        "description": description,
        "reason": "Repeatable routine.",
        "confidence": 0.9,
        "skill_md": f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n{body}\n",
        "evidence": ["event:1"],
    }


def reflector(merges):
    code = "import json,sys; sys.stdin.read(); print(json.dumps({'merges': %r}))" % (merges,)
    return (sys.executable, "-c", code)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        root = Path(self.directory.name)
        self.project = root / "repo"
        self.project.mkdir()
        self.home = root / "home"
        self.storage = Storage(self.home / "state.sqlite3")

    def tearDown(self):
        self.directory.cleanup()

    def config(self, merges=(), **overrides):
        return Config(
            home=self.home, mode="auto", reflect_command=reflector(list(merges)),
            reflect_timeout=30, **overrides,
        )

    def make(self, *names, **kwargs):
        for name in names:
            promote(skill_proposal(name, **kwargs), self.project)


def merge_item(keep, absorb, **extra):
    description = "Run the project test suite and linters."
    item = {
        "keep": keep,
        "absorb": absorb,
        "description": description,
        "reason": "Both describe running the project checks.",
        "confidence": 0.9,
        "skill_md": f"---\nname: {keep}\ndescription: {description}\n---\n\n# {keep}\n\nRun tests then lint.\n",
    }
    item.update(extra)
    return item


class IndexTests(Fixture):
    def test_index_lists_descriptions_and_orders_trusted_first(self):
        self.make("alpha-skill", "beta-skill", description="Do alpha things.")
        trust_skill("beta-skill", "project", self.project)
        entries = skill_index(self.project)
        lines = format_index_lines(entries, 100, 30)
        self.assertTrue(lines[0].startswith("- beta-skill (project, trusted)"))
        self.assertIn("Do alpha things.", lines[1])

    def test_index_caps_lines_and_descriptions(self):
        self.make(*[f"skill-{n}" for n in range(5)], description="x" * 300)
        lines = format_index_lines(skill_index(self.project), 20, 3)
        self.assertEqual(len(lines), 4)
        self.assertIn("+2 more", lines[-1])
        self.assertLess(len(lines[0]), 80)

    def test_session_start_injects_index_only_when_enabled_and_skills_exist(self):
        self.assertIsNone(session_start_context(self.config(), self.project))
        self.make("alpha-skill")
        self.assertIn("alpha-skill", session_start_context(self.config(), self.project))
        self.assertIsNone(session_start_context(self.config(inject_index=False), self.project))
        off = Config(home=self.home, mode="off")
        self.assertIsNone(session_start_context(off, self.project))

    def test_session_start_hook_returns_additional_context(self):
        self.make("alpha-skill")
        payload = json.dumps({
            "sessionId": "s", "timestamp": 1_767_225_600_000, "cwd": str(self.project),
            "source": "new",
        })
        out = io.StringIO()
        handle_hook(
            self.storage, stdin=io.StringIO(payload), stdout=out, event_name="sessionStart",
            context_provider=lambda ev: session_start_context(self.config(), Path(ev.cwd)),
        )
        self.assertIn("alpha-skill", json.loads(out.getvalue())["additionalContext"])
        quiet = io.StringIO()
        handle_hook(self.storage, stdin=io.StringIO(payload), stdout=quiet, event_name="sessionStart")
        self.assertEqual(json.loads(quiet.getvalue()), {})

    def test_hook_survives_provider_failure(self):
        payload = json.dumps({"sessionId": "s", "timestamp": 1_767_225_600_000, "cwd": str(self.project)})
        out = io.StringIO()

        def boom(_):
            raise HarnessError("nope")

        handle_hook(
            self.storage, stdin=io.StringIO(payload), stdout=out, event_name="sessionStart",
            context_provider=boom,
        )
        self.assertEqual(json.loads(out.getvalue()), {})

    def test_reflection_prompt_has_full_index_and_relevant_bodies(self):
        self.make("npm-release", body="Publish with npm publish.")
        self.make("uv-workflow", body="Use uv add and uv run pytest.")
        context = [
            {"name": "npm-release", "scope": "project", "content": "Publish with npm publish."},
            {"name": "uv-workflow", "scope": "project", "content": "Use uv add and uv run pytest."},
        ]
        relevant = select_relevant_skills(context, "please run pytest using uv", limit=1)
        self.assertEqual(relevant[0]["name"], "uv-workflow")
        prompt = build_reflection_prompt("s", [], relevant, None, skill_index(self.project))
        self.assertIn("npm-release (project", prompt)
        self.assertIn("uv-workflow (project", prompt)
        self.assertIn("Use uv add", prompt)
        self.assertNotIn("Publish with npm publish.", prompt)


class MergeTests(Fixture):
    def test_auto_merge_patches_keeper_and_archives_absorbed(self):
        self.make("test-runner", "lint-runner")
        cfg = self.config([merge_item("test-runner", ["lint-runner"])])
        results = consolidate(self.storage, cfg, self.project, "project", auto=True)
        self.assertTrue(results[0]["applied"])
        self.assertEqual([s["name"] for s in list_skills(self.project, "project")], ["test-runner"])
        body = (self.project / ".github" / "skills" / "test-runner" / "SKILL.md").read_text()
        self.assertIn("Run tests then lint.", body)
        archived = self.project / ".github" / "copilot-harness" / "archive" / "lint-runner"
        sidecar = json.loads((archived / ".sidecar.json").read_text())
        self.assertEqual(sidecar["merged_into"], "test-runner")
        ledger = (self.project / ".github" / "skills" / "test-runner" / ".ledger.jsonl").read_text()
        self.assertIn('"action": "merge"', ledger)
        self.assertIn("lint-runner", ledger)
        self.assertEqual(self.storage.proposal(results[0]["proposal"]["id"])["status"], "accepted")
        # Absorbed skills are restorable, and restoring clears the merge marker.
        move_skill("lint-runner", "project", self.project, archive=False)
        restored = json.loads(
            (self.project / ".github" / "skills" / "lint-runner" / ".sidecar.json").read_text()
        )
        self.assertNotIn("merged_into", restored)

    def test_trusted_skills_are_held_for_review(self):
        self.make("test-runner", "lint-runner")
        trust_skill("lint-runner", "project", self.project)
        cfg = self.config([merge_item("test-runner", ["lint-runner"])])
        results = consolidate(self.storage, cfg, self.project, "project", auto=True)
        self.assertFalse(results[0]["applied"])
        self.assertEqual(len(list_skills(self.project, "project")), 2)
        with self.assertRaises(HarnessError):
            merge_skills(results[0]["proposal"], self.project)
        merge_skills({**results[0]["proposal"], "approved_by": "reviewer"}, self.project)
        self.assertEqual(len(list_skills(self.project, "project")), 1)

    def test_sensitive_merges_are_held(self):
        self.make("test-runner", "lint-runner")
        item = merge_item("test-runner", ["lint-runner"], reason="Both manage production deployment.")
        results = consolidate(self.storage, self.config([item]), self.project, "project", auto=True)
        self.assertEqual(results[0]["proposal"]["risk_level"], "sensitive")
        self.assertFalse(results[0]["applied"])
        self.assertEqual(len(list_skills(self.project, "project")), 2)

    def test_invalid_merges_are_ignored(self):
        self.make("test-runner", "lint-runner", "other-skill")
        merges = [
            merge_item("test-runner", ["missing-skill"]),
            merge_item("test-runner", ["test-runner"]),
            merge_item("test-runner", ["lint-runner"], confidence=7),
            merge_item("test-runner", ["lint-runner"], skill_md="no frontmatter"),
            merge_item("test-runner", ["lint-runner"], skill_md="---\nname: other\ndescription: x\n---\nbody"),
        ]
        results = consolidate(self.storage, self.config(merges), self.project, "project", auto=True)
        self.assertEqual(results, [])
        self.assertEqual(len(list_skills(self.project, "project")), 3)

    def test_overlapping_merges_use_each_skill_once(self):
        self.make("test-runner", "lint-runner", "fmt-runner")
        merges = [
            merge_item("test-runner", ["lint-runner"]),
            merge_item("fmt-runner", ["lint-runner"]),
        ]
        results = consolidate(self.storage, self.config(merges), self.project, "project", auto=True)
        self.assertEqual(len(results), 1)

    def test_review_mode_leaves_merge_pending_and_accept_applies_it(self):
        self.make("test-runner", "lint-runner")
        cfg = self.config([merge_item("test-runner", ["lint-runner"])])
        results = consolidate(self.storage, cfg, self.project, "project", auto=False)
        proposal_id = results[0]["proposal"]["id"]
        self.assertEqual(self.storage.proposal(proposal_id)["absorbs"], ["lint-runner"])
        self.assertEqual(len(list_skills(self.project, "project")), 2)
        (self.home).mkdir(exist_ok=True)
        (self.home / "config.toml").write_text(
            'mode = "review"\n[reflection]\ncommand = ["x"]\n', encoding="utf-8"
        )
        with patch.dict("os.environ", {"COPILOT_HARNESS_HOME": str(self.home)}), \
             contextlib.redirect_stdout(io.StringIO()):
            code = _handle(Namespace(command="accept", proposal_id=proposal_id))
        self.assertEqual(code, 0)
        self.assertEqual([s["name"] for s in list_skills(self.project, "project")], ["test-runner"])

    def test_periodic_trigger_respects_threshold_and_minimum_skills(self):
        self.make("test-runner", "lint-runner", "fmt-runner")
        cfg = self.config(
            [merge_item("test-runner", ["lint-runner"])], consolidate_every=3, consolidate_min_skills=3
        )
        for n in range(2):
            self.storage.add_event(Event(
                "s", "postToolUse", "2026-01-01T00:00:00+00:00", str(self.project), "bash", "success", {},
            ))
        self.assertEqual(maybe_consolidate(self.storage, cfg, self.project), [])
        self.storage.add_event(Event(
            "s", "postToolUse", "2026-01-01T00:00:00+00:00", str(self.project), "bash", "success", {},
        ))
        results = maybe_consolidate(self.storage, cfg, self.project)
        self.assertEqual(len(results), 1)
        # The window is consumed; nothing re-runs until more activity accrues.
        self.assertEqual(maybe_consolidate(self.storage, cfg, self.project), [])
        disabled = self.config([], consolidate_every=1, consolidate_enabled=False)
        self.assertEqual(maybe_consolidate(self.storage, disabled, self.project), [])


class ConfigTests(unittest.TestCase):
    def test_index_and_consolidation_config(self):
        with TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "config.toml").write_text(
                '[index]\nsession_start = false\ndescription_chars = 50\nmax_skills = 5\n'
                '[consolidation]\nenabled = false\nevery_tool_calls = 10\nmin_skills = 2\n',
                encoding="utf-8",
            )
            config = load_config(home)
            self.assertFalse(config.inject_index)
            self.assertEqual((config.index_desc_chars, config.index_max_lines), (50, 5))
            self.assertFalse(config.consolidate_enabled)
            self.assertEqual((config.consolidate_every, config.consolidate_min_skills), (10, 2))
            (home / "config.toml").write_text("[index]\nmax_skills = 0\n", encoding="utf-8")
            with self.assertRaises(HarnessError):
                load_config(home)

    def test_migrates_proposals_table_without_absorbs_column(self):
        import sqlite3

        with TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE proposals (id TEXT PRIMARY KEY, action TEXT NOT NULL, scope TEXT NOT NULL,"
                    " name TEXT NOT NULL, description TEXT NOT NULL, reason TEXT NOT NULL,"
                    " confidence REAL NOT NULL, skill_md TEXT NOT NULL, evidence_json TEXT NOT NULL,"
                    " status TEXT NOT NULL, created_at TEXT NOT NULL, cwd TEXT NOT NULL)"
                )
            connection.close()
            storage = Storage(database)
            storage.add_proposal({
                "id": "x", "action": "merge", "scope": "project", "name": "a", "description": "d",
                "reason": "r", "confidence": 0.5, "risk_level": "low", "skill_md": "m",
                "evidence": [], "absorbs": ["b"],
            }, "cwd", "now")
            self.assertEqual(storage.proposal("x")["absorbs"], ["b"])


if __name__ == "__main__":
    unittest.main()
