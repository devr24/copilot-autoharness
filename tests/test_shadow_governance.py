import contextlib
import io
import json
import os
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from harness.cli import main
from harness.errors import HarnessError
from harness.shadow import detect_conflicts, run_shadow, score_answer
from harness.sharing import FILES, scaffold_shared_repo
from harness.skills import (
    export_skill,
    import_proposal,
    inspect_skill,
    promote,
    set_governance,
    share_proposal,
    trust_skill,
    verify_export,
)


def make(name="build-workflow", marker="", action="create", scope="project", description="Build and test this repository reliably.", extra=""):
    return {
        "id": f"proposal-{name}-{action}-{len(marker)}",
        "action": action,
        "scope": scope,
        "name": name,
        "description": description,
        "reason": "A repeatable build procedure was found.",
        "confidence": 0.9,
        "skill_md": f"---\nname: {name}\ndescription: {description}\n---\n\n# Build\n\n{marker}\n{extra}\n",
        "evidence": ["event:1"],
    }


def future(days=30):
    return (datetime.now(UTC).date() + timedelta(days=days)).isoformat()


class GovernanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name).resolve()

    def test_sensitive_skill_needs_governance_to_export_and_expiry_blocks_verify(self):
        item = make("deploy-prod", description="Deploy to production safely.")
        item["approved_by"] = "reviewer"
        promote(item, self.cwd)
        trust_skill("deploy-prod", "project", self.cwd)
        destination = self.cwd / "shared"
        with self.assertRaisesRegex(HarnessError, "no governance metadata"):
            export_skill("deploy-prod", "project", self.cwd, destination)

        with self.assertRaisesRegex(HarnessError, "must not be in the past"):
            set_governance("deploy-prod", "project", self.cwd, "platform-team", "high", "2020-01-01")
        with self.assertRaisesRegex(HarnessError, "Criticality"):
            set_governance("deploy-prod", "project", self.cwd, "platform-team", "huge", future())
        set_governance("deploy-prod", "project", self.cwd, "platform-team", "high", future())
        exported = export_skill("deploy-prod", "project", self.cwd, destination)
        manifest = json.loads((exported / "provenance.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["governance"]["owner"], "platform-team")
        self.assertNotIn("set_by", manifest["governance"])
        verify_export(exported)

        manifest["governance"]["expires"] = "2020-01-01"
        (exported / "provenance.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(HarnessError, "review expired"):
            verify_export(exported)

    def test_low_risk_skill_without_governance_still_exports(self):
        promote(make(), self.cwd)
        trust_skill("build-workflow", "project", self.cwd)
        exported = export_skill("build-workflow", "project", self.cwd, self.cwd / "shared")
        self.assertNotIn("governance", json.loads((exported / "provenance.json").read_text(encoding="utf-8")))

    def test_import_creates_proposal_only_and_rejects_tampering(self):
        promote(make(), self.cwd)
        trust_skill("build-workflow", "project", self.cwd)
        exported = export_skill("build-workflow", "project", self.cwd, self.cwd / "shared")
        other = self.cwd / "other"
        other.mkdir()
        proposal = import_proposal(exported, "project", other)
        self.assertEqual(proposal["action"], "create")
        self.assertFalse((other / ".github" / "skills" / "build-workflow").exists())
        with self.assertRaisesRegex(HarnessError, "already exists"):
            import_proposal(exported, "project", self.cwd)
        (exported / "SKILL.md").write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(HarnessError, "integrity check failed"):
            import_proposal(exported, "project", other)

    def test_share_requires_trust_and_copies_across_scopes(self):
        home = self.cwd / "home"
        home.mkdir()
        with patch.object(Path, "home", return_value=home):
            promote(make(scope="personal"), self.cwd)
            with self.assertRaisesRegex(HarnessError, "Only trusted"):
                share_proposal("build-workflow", "personal", "project", self.cwd)
            trust_skill("build-workflow", "personal", self.cwd)
            proposal = share_proposal("build-workflow", "personal", "project", self.cwd)
            self.assertEqual(proposal["scope"], "project")
            with self.assertRaisesRegex(HarnessError, "differ"):
                share_proposal("build-workflow", "personal", "personal", self.cwd)
            self.assertEqual(inspect_skill("build-workflow", "personal", self.cwd)[1]["scope"], "personal")


class ShadowTests(unittest.TestCase):
    def test_score_answer(self):
        expect = {"must_include": ["ruff"], "must_exclude": ["rm -rf"]}
        self.assertEqual(score_answer(expect, "Run RUFF first"), [])
        self.assertEqual(len(score_answer(expect, "rm -rf it")), 2)

    def test_conflict_detection(self):
        candidate = make(description="Format python source files with ruff", extra="Always run ruff format before committing code.")["skill_md"]
        others = [
            {"scope": "project", "name": "dup", "content": make(description="Format python source files using ruff")["skill_md"]},
            {"scope": "project", "name": "opposed", "content": "---\nname: opposed\ndescription: Something else entirely\n---\nNever run ruff format before committing code.\n"},
            {"scope": "project", "name": "unrelated", "content": "---\nname: unrelated\ndescription: Database backups\n---\nAlways snapshot the volume first.\n"},
        ]
        kinds = {(c.kind, c.other) for c in detect_conflicts(candidate, others)}
        self.assertIn(("overlap", "project/dup"), kinds)
        self.assertIn(("contradiction", "project/opposed"), kinds)
        self.assertFalse(any(other == "project/unrelated" for _, other in kinds))

    def test_run_shadow_reports_regression_and_improvement(self):
        code = (
            "import sys, json; p = sys.stdin.read(); "
            "print(json.dumps({'answer': 'use ruff' if 'USE-RUFF' in p else 'nothing'}))"
        )
        config = SimpleNamespace(reflect_command=(sys.executable, "-c", code), reflect_timeout=30)
        cases = [{"name": "lint", "task": "Lint the code", "expect": {"must_include": ["ruff"]}}]
        better = run_shadow(config, cases, "USE-RUFF", None)[0]
        self.assertEqual(better.outcome, "improvement")
        worse = run_shadow(config, cases, "no marker", "USE-RUFF")[0]
        self.assertEqual(worse.outcome, "regression")
        self.assertEqual(run_shadow(config, cases, "USE-RUFF", "USE-RUFF")[0].outcome, "same")


class ShadowCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.project, self.home, self.cases = root / "repo", root / "home", root / "cases"
        for path in (self.project, self.home, self.cases):
            path.mkdir()
        code = (
            "import sys, json; p = sys.stdin.read(); "
            "print(json.dumps({'answer': 'use ruff' if 'USE-RUFF' in p else 'nothing'}))"
        )
        (self.home / "config.toml").write_text(
            f'mode = "auto"\n[reflection]\ncommand = {json.dumps([sys.executable, "-c", code])}\n', encoding="utf-8"
        )
        (self.cases / "lint.json").write_text(
            json.dumps({"name": "lint", "task": "Lint the code", "expect": {"must_include": ["ruff"]}}), encoding="utf-8"
        )

    def run_cli(self, *argv):
        original = Path.cwd()
        output = io.StringIO()
        try:
            os.chdir(self.project)
            with patch.dict(os.environ, {"COPILOT_HARNESS_HOME": str(self.home)}), contextlib.redirect_stdout(output):
                code = main(list(argv))
        finally:
            os.chdir(original)
        return code, output.getvalue()

    def test_shadow_flags_regression_and_leaves_skill_untouched(self):
        promote(make(marker="USE-RUFF"), self.project)
        promote(make(marker="nothing useful", action="patch"), self.project)
        body = (self.project / ".github" / "skills" / "build-workflow" / "SKILL.md").read_text(encoding="utf-8")
        code, output = self.run_cli("shadow", "build-workflow", "--cases", str(self.cases))
        self.assertEqual(code, 1)
        self.assertIn("regression", output)
        self.assertIn("The live skill was not changed", output)
        self.assertEqual(
            (self.project / ".github" / "skills" / "build-workflow" / "SKILL.md").read_text(encoding="utf-8"), body
        )

    def test_shadow_reports_improvement_and_no_cases_error(self):
        promote(make(marker="USE-RUFF"), self.project)
        code, output = self.run_cli("shadow", "build-workflow", "--cases", str(self.cases))
        self.assertEqual(code, 0)
        self.assertIn("improvement", output)
        empty = self.cases.parent / "empty"
        empty.mkdir()
        code, _ = self.run_cli("shadow", "build-workflow", "--cases", str(empty))
        self.assertEqual(code, 1)


class ScaffoldTests(unittest.TestCase):
    def test_scaffold_writes_files_and_refuses_overwrite(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "shared"
            written = scaffold_shared_repo(target)
            self.assertEqual(len(written), len(FILES))
            workflow = (target / ".github" / "workflows" / "verify-skills.yml").read_text(encoding="utf-8")
            self.assertIn('harness verify "${dir%/}"', workflow)
            with self.assertRaisesRegex(HarnessError, "overwrite"):
                scaffold_shared_repo(target)


if __name__ == "__main__":
    unittest.main()
