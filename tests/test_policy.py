import contextlib
import io
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from harness.audit import audit_skills
from harness.cli import main
from harness.errors import HarnessError
from harness.policy import check_policy, enforce_policy, find_policy, load_policy, sign_export, verify_signature
from harness.skills import export_skill, promote, set_governance, trust_skill
from test_shadow_governance import future, make


def write_policy(path: Path, **fields):
    path.write_text(json.dumps({"format_version": 1, **fields}), encoding="utf-8")
    return load_policy(path)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name).resolve()
        self.shared = self.cwd / "shared"
        promote(make(), self.cwd)
        trust_skill("build-workflow", "project", self.cwd)
        set_governance("build-workflow", "project", self.cwd, "platform-team", "high", future())
        self.export = export_skill("build-workflow", "project", self.cwd, self.shared)

    def test_deny_list_owner_and_criticality(self):
        digest = json.loads((self.export / "provenance.json").read_text(encoding="utf-8"))["skill_sha256"]
        policy = write_policy(self.cwd / "p.json", deny_names=["build-workflow"], deny_sha256=[digest.upper()],
                              allowed_owners=["other-team"], max_criticality="medium")
        problems = " | ".join(check_policy(self.export, policy))
        for text in ("deny list", "content hash", "allowed_owners", "exceeds policy maximum"):
            self.assertIn(text, problems)
        with self.assertRaisesRegex(HarnessError, "blocks build-workflow"):
            enforce_policy(self.export, policy)

    def test_permissive_policy_passes_and_invalid_is_rejected(self):
        policy = write_policy(self.cwd / "p.json", allowed_owners=["platform-team"], max_criticality="high",
                              require_governance=True)
        self.assertEqual(check_policy(self.export, policy), [])
        (self.cwd / "bad.json").write_text(json.dumps({"format_version": 1, "require_signature": True}), encoding="utf-8")
        with self.assertRaisesRegex(HarnessError, "allowed_signers"):
            load_policy(self.cwd / "bad.json")
        (self.cwd / "worse.json").write_text("[]", encoding="utf-8")
        with self.assertRaises(HarnessError):
            load_policy(self.cwd / "worse.json")

    def test_audit_and_import_enforce_policy(self):
        write_policy(self.cwd / "harness-policy.json", deny_names=["build-workflow"])
        self.assertEqual(find_policy(self.cwd), self.cwd / "harness-policy.json")
        _, findings = audit_skills(self.shared, load_policy(self.cwd / "harness-policy.json"))
        self.assertEqual([f.check for f in findings], ["policy"])
        other = self.cwd / "consumer"
        other.mkdir()
        write_policy(other / "harness-policy.json", deny_names=["build-workflow"])
        old = Path.cwd()
        os.chdir(other)
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(main(["import", str(self.export)]), 1)
        finally:
            os.chdir(old)
        self.assertIn("blocks build-workflow", err.getvalue())


@unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen not available")
class SignatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name).resolve()
        promote(make(), self.cwd)
        trust_skill("build-workflow", "project", self.cwd)
        self.export = export_skill("build-workflow", "project", self.cwd, self.cwd / "shared")
        self.key = self.cwd / "key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.key)], check=True)
        public = (self.cwd / "key.pub").read_text(encoding="utf-8").strip()
        self.signers = self.cwd / "allowed_signers"
        self.signers.write_text(f"skills-team@example.com {public}\n", encoding="utf-8")

    def test_sign_verify_and_tamper(self):
        sign_export(self.export, self.key)
        self.assertEqual(verify_signature(self.export, self.signers), "skills-team@example.com")
        with self.assertRaisesRegex(HarnessError, "Already signed"):
            sign_export(self.export, self.key)
        manifest = json.loads((self.export / "provenance.json").read_text(encoding="utf-8"))
        manifest["skill_version"] = 99
        (self.export / "provenance.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(HarnessError, "not from an allowed signer"):
            verify_signature(self.export, self.signers)

    def test_unknown_key_and_missing_signature_fail_policy(self):
        policy = write_policy(self.cwd / "p.json", require_signature=True, allowed_signers="allowed_signers")
        self.assertIn("missing signature", check_policy(self.export, policy))
        other = self.cwd / "other"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)], check=True)
        sign_export(self.export, other)
        self.assertIn("signature is not from an allowed signer", check_policy(self.export, policy))

    def test_signed_export_passes_policy_and_cli(self):
        policy = write_policy(self.cwd / "p.json", require_signature=True, allowed_signers="allowed_signers")
        self.assertEqual(main(["sign", str(self.export), "--key", str(self.key)]), 0)
        self.assertEqual(check_policy(self.export, policy), [])
        self.assertEqual(main(["verify-signature", str(self.export), "--allowed-signers", str(self.signers)]), 0)


if __name__ == "__main__":
    unittest.main()
