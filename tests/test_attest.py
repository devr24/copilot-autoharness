import hashlib
import json
import tarfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from unittest.mock import patch

from harness.attest import build_bundle, verify_attestation
from harness.cli import main
from harness.errors import HarnessError
from harness.policy import load_policy
from harness.sharing import FILES


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        skills = self.root / "skills"
        (skills / "a").mkdir(parents=True)
        (skills / "a" / "SKILL.md").write_text("one\n", encoding="utf-8")
        (skills / "a" / "provenance.json").write_text("{}\n", encoding="utf-8")
        (skills / ".gitkeep").write_text("", encoding="utf-8")
        self.skills = skills

    def test_bundle_is_reproducible_and_skips_dotfiles(self):
        first = build_bundle(self.skills, self.root / "one.tar.gz")
        (self.skills / "a" / "SKILL.md").touch()
        second = build_bundle(self.skills, self.root / "two.tar.gz")
        self.assertEqual(hashlib.sha256(first.read_bytes()).hexdigest(), hashlib.sha256(second.read_bytes()).hexdigest())
        with tarfile.open(first) as archive:
            self.assertEqual(archive.getnames(), ["a/SKILL.md", "a/provenance.json"])

    def test_bundle_errors(self):
        out = self.root / "x.tar.gz"
        build_bundle(self.skills, out)
        with self.assertRaisesRegex(HarnessError, "already exists"):
            build_bundle(self.skills, out)
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(HarnessError, "No skills"):
            build_bundle(empty, self.root / "y.tar.gz")


class AttestationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.bundle = self.root / "skills-bundle.tar.gz"
        self.bundle.write_bytes(b"data")

    def test_passes_expected_gh_arguments(self):
        with patch("harness.attest.shutil.which", return_value="gh"), patch(
            "harness.attest.subprocess.run", return_value=CompletedProcess([], 0, "ok", "")
        ) as run:
            result = verify_attestation(self.bundle, "org/skills", "org/skills/.github/workflows/release-skills.yml")
        command = run.call_args.args[0]
        self.assertEqual(command[1:3], ["attestation", "verify"])
        self.assertIn("--repo", command)
        self.assertIn("--signer-workflow", command)
        self.assertIn("org/skills", result)

    def test_failure_missing_gh_and_bad_repo(self):
        with patch("harness.attest.shutil.which", return_value="gh"), patch(
            "harness.attest.subprocess.run", return_value=CompletedProcess([], 1, "", "no matching attestations")
        ):
            with self.assertRaisesRegex(HarnessError, "no matching attestations"):
                verify_attestation(self.bundle, "org/skills")
        with patch("harness.attest.shutil.which", return_value=None):
            with self.assertRaisesRegex(HarnessError, "gh"):
                verify_attestation(self.bundle, "org/skills")
        with self.assertRaisesRegex(HarnessError, "owner/name"):
            verify_attestation(self.bundle, "nonsense")

    def test_cli_uses_policy_and_requires_repo(self):
        policy = self.root / "p.json"
        policy.write_text(json.dumps({"format_version": 1, "trusted_repo": "org/skills"}), encoding="utf-8")
        self.assertEqual(load_policy(policy).trusted_repo, "org/skills")
        with patch("harness.attest.shutil.which", return_value="gh"), patch(
            "harness.attest.subprocess.run", return_value=CompletedProcess([], 0, "", "")
        ) as run:
            self.assertEqual(main(["verify-attestation", str(self.bundle), "--policy", str(policy)]), 0)
        self.assertIn("org/skills", run.call_args.args[0])
        empty = self.root / "none.json"
        empty.write_text(json.dumps({"format_version": 1}), encoding="utf-8")
        self.assertEqual(main(["verify-attestation", str(self.bundle), "--policy", str(empty)]), 1)

    def test_scaffold_includes_release_workflow(self):
        release = FILES[".github/workflows/release-skills.yml"]
        self.assertIn("actions/attest-build-provenance", release)
        self.assertIn("${{ github.ref_name }}", release)
        self.assertIn("attestations: write", release)


if __name__ == "__main__":
    unittest.main()
