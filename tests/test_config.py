import tomllib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from harness.config import default_config_text, load_config


class ConfigTests(unittest.TestCase):
    def test_auto_reflector_defaults_are_valid_toml_on_all_platforms(self):
        for platform in ("win32", "linux"):
            with patch("harness.config.sys.platform", platform):
                values = tomllib.loads(default_config_text())
            self.assertEqual(values["mode"], "auto")
            self.assertIn("copilot", " ".join(values["reflection"]["command"]))
            self.assertIn("--available-tools=harness_no_tools", " ".join(values["reflection"]["command"]))
            self.assertIn("--excluded-tools=bash,powershell", " ".join(values["reflection"]["command"]))
            self.assertIn("--disable-builtin-mcps", " ".join(values["reflection"]["command"]))
            self.assertNotIn(" -p ", " ".join(values["reflection"]["command"]))

    def test_missing_config_keeps_capture_only_mode(self):
        with TemporaryDirectory() as directory:
            config = load_config(Path(directory))
        self.assertEqual(config.mode, "off")
        self.assertEqual(config.reflect_command, ())


if __name__ == "__main__":
    unittest.main()
