import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import doctor
from harness.config import load_config


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        env = {"COPILOT_HARNESS_HOME": str(self.root / "home"), "COPILOT_HOME": str(self.root / "chome")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        (self.root / "chome").mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()

    def checks(self):
        return {name: (ok, detail) for name, ok, detail in doctor.run_checks(load_config(), self.repo)}

    def test_missing_everything(self):
        with mock.patch("shutil.which", return_value=None):
            result = self.checks()
        self.assertFalse(result["harness executable"][0])
        self.assertFalse(result["hooks"][0])
        self.assertFalse(result["configuration"][0])

    def test_windows_shim_rejected(self):
        with mock.patch("shutil.which", return_value="C:\\bin\\harness.cmd"), mock.patch("sys.platform", "win32"):
            self.assertFalse(self.checks()["harness executable"][0])

    def test_repo_hooks_detected(self):
        hooks = self.repo / ".github" / "hooks"
        hooks.mkdir(parents=True)
        (hooks / "h.json").write_text('{"exec": "harness"}', encoding="utf-8")
        self.assertTrue(self.checks()["hooks"][0])

    def test_plugin_detected_and_double_install_flagged(self):
        (self.root / "chome" / "config.json").write_text(
            '// managed\n{"installedPlugins": [{"name": "copilot-harness", "enabled": true}]}',
            encoding="utf-8",
        )
        self.assertEqual(self.checks()["hooks"], (True, "plugin"))
        hooks = self.repo / ".github" / "hooks"
        hooks.mkdir(parents=True)
        (hooks / "h.json").write_text('{"exec": "harness"}', encoding="utf-8")
        self.assertFalse(self.checks()["hooks"][0])


if __name__ == "__main__":
    unittest.main()
