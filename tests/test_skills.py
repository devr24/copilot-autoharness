import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from harness.errors import HarnessError
from harness.skills import (
    export_skill,
    move_skill,
    promote,
    quarantine_skill,
    list_skills,
    classify_risk,
    restore_quarantined_skill,
    rollback_skill,
    trust_skill,
    validate_skill,
    verify_export,
)


def proposal(name="build-workflow", action="create"):
    return {
        "id": "proposal-1",
        "action": action,
        "scope": "project",
        "name": name,
        "description": "Build and test this repository reliably.",
        "reason": "The session discovered a repeatable build procedure.",
        "confidence": 0.9,
        "skill_md": (
            f"---\nname: {name}\ndescription: Build and test this repository reliably.\n"
            "---\n\n# Build workflow\n\nRun the repository's documented checks.\n"
        ),
        "evidence": ["event:1", "event:2"],
    }


class SkillTests(unittest.TestCase):
    def test_sensitive_classification_covers_common_high_impact_terms(self):
        for term in ("production deployment", "IAM permissions", "secrets", "data migrations", "firewall"):
            with self.subTest(term=term):
                self.assertEqual(classify_risk("example-skill", term, "reason", "body"), "sensitive")
        self.assertEqual(
            classify_risk("format-code", "Format source files.", "Routine formatting.", "Use ruff."),
            "low",
        )

    def test_validates_and_writes_native_skill_with_provenance(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = promote(proposal(), root)
            self.assertTrue((path / "SKILL.md").is_file())
            sidecar = json.loads((path / ".sidecar.json").read_text())
            self.assertEqual(sidecar["managed_by"], "copilot-harness")
            self.assertEqual(sidecar["trust_state"], "probation")
            self.assertEqual(sidecar["version"], 1)
            self.assertTrue((path.parent.parent / "copilot-harness" / "versions" / "build-workflow" / "000001" / "SKILL.md").exists())
            ledger = json.loads((path / ".ledger.jsonl").read_text())
            self.assertEqual(ledger["evidence"], ["event:1", "event:2"])
            self.assertEqual(ledger["provenance"]["source_event_ids"], ["event:1", "event:2"])

    def test_does_not_overwrite_human_owned_skill(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / ".github" / "skills" / "build-workflow"
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text("human-owned")
            with self.assertRaises(HarnessError):
                promote(proposal(), Path(directory))
            self.assertEqual((target / "SKILL.md").read_text(), "human-owned")

    def test_patch_and_archive_restore_require_harness_marker(self):
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            target = promote(proposal(), cwd)
            update = proposal(action="patch")
            update["id"] = "proposal-2"
            update["reason"] = "A new successful run confirmed the steps."
            promote(update, cwd)
            self.assertIn("build-workflow", (target / "SKILL.md").read_text())
            update["skill_md"] = update["skill_md"].replace(
                "Run the repository's documented checks.",
                "Run the repository's documented checks before committing.",
            )
            update["id"] = "proposal-3"
            update["reason"] = "Validation confirmed the commit check."
            promote(update, cwd)
            self.assertEqual(json.loads((target / ".sidecar.json").read_text())["version"], 3)
            rollback_skill("build-workflow", "project", cwd)
            self.assertNotIn("before committing", (target / "SKILL.md").read_text())
            self.assertEqual(json.loads((target / ".sidecar.json").read_text())["trust_state"], "probation")
            trust_skill("build-workflow", "project", cwd)
            self.assertEqual(json.loads((target / ".sidecar.json").read_text())["trust_state"], "trusted")
            archived = move_skill("build-workflow", "project", cwd, archive=True)
            self.assertTrue(archived.exists())
            restored = move_skill("build-workflow", "project", cwd, archive=False)
            self.assertTrue(restored.exists())

    def test_sensitive_skills_require_explicit_approval(self):
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            sensitive = proposal(name="production-deployment")
            sensitive["skill_md"] = sensitive["skill_md"].replace(
                "build-workflow", "production-deployment"
            ).replace("Build and test this repository reliably.", "Deploy to production safely.")
            sensitive["description"] = "Deploy to production safely."
            sensitive["risk_level"] = "low"
            with self.assertRaisesRegex(HarnessError, "explicit human approval"):
                promote(sensitive, cwd)
            sensitive["approved_by"] = "reviewer"
            path = promote(sensitive, cwd)
            metadata = json.loads((path / ".sidecar.json").read_text())
            self.assertEqual(metadata["risk_level"], "sensitive")
            self.assertEqual(metadata["approval"]["approved_by"], "reviewer")

    def test_export_requires_trust_and_verifies_privacy_minimized_manifest(self):
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            item = proposal()
            item["source_session_ids"] = ["session-private-1", "session-private-2"]
            promote(item, cwd)
            destination = cwd / "shared-skills"
            with self.assertRaisesRegex(HarnessError, "Only trusted skills"):
                export_skill("build-workflow", "project", cwd, destination)

            trust_skill("build-workflow", "project", cwd)
            exported = export_skill("build-workflow", "project", cwd, destination)
            manifest = json.loads((exported / "provenance.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["skill_name"], "build-workflow")
            self.assertEqual(manifest["source_session_count"], 2)
            self.assertEqual(len(manifest["skill_sha256"]), 64)
            self.assertNotIn("session-private-1", json.dumps(manifest))
            self.assertNotIn("repository", manifest)
            self.assertEqual(verify_export(exported), manifest["skill_sha256"])

            with self.assertRaisesRegex(HarnessError, "already exists"):
                export_skill("build-workflow", "project", cwd, destination)
            (exported / "SKILL.md").write_text("modified", encoding="utf-8")
            with self.assertRaisesRegex(HarnessError, "integrity check failed"):
                verify_export(exported)

    def test_quarantine_removes_skill_from_recall_and_restores_to_probation(self):
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            promote(proposal(), cwd)
            quarantine = quarantine_skill("build-workflow", "project", cwd, "investigate suspected bad guidance")
            self.assertTrue(quarantine.exists())
            self.assertEqual(list_skills(cwd, "project"), [])
            restored = restore_quarantined_skill("build-workflow", "project", cwd)
            self.assertTrue(restored.exists())
            self.assertEqual(json.loads((restored / ".sidecar.json").read_text())["trust_state"], "probation")

    def test_rollback_rejects_tampered_version_snapshot(self):
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            path = promote(proposal(), cwd)
            update = proposal(action="patch")
            update["id"] = "proposal-2"
            update["reason"] = "A new run confirmed the build sequence."
            promote(update, cwd)
            version = path.parent.parent / "copilot-harness" / "versions" / "build-workflow" / "000001" / "SKILL.md"
            version.write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(HarnessError, "integrity check"):
                rollback_skill("build-workflow", "project", cwd)

    def test_capacity_and_patch_ownership_are_enforced(self):
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            first = proposal()
            promote(first, cwd, capacity=1)
            second = proposal(name="test-workflow")
            second["skill_md"] = second["skill_md"].replace("build-workflow", "test-workflow")
            with self.assertRaisesRegex(HarnessError, "capacity"):
                promote(second, cwd, capacity=1)

            human = cwd / ".github" / "skills" / "human-skill"
            human.mkdir()
            (human / "SKILL.md").write_text("human-owned")
            patch = proposal(name="human-skill", action="patch")
            patch["skill_md"] = patch["skill_md"].replace("build-workflow", "human-skill")
            with self.assertRaises(HarnessError):
                promote(patch, cwd)

    def test_rejects_path_traversal_and_mismatched_frontmatter(self):
        with self.assertRaises(HarnessError):
            validate_skill("../bad", "desc", "---\nname: bad\ndescription: desc\n---\n")
        with self.assertRaises(HarnessError):
            validate_skill("good-name", "desc", "---\nname: other\ndescription: desc\n---\n")


if __name__ == "__main__":
    unittest.main()
