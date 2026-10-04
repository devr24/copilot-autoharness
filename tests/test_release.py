import json
import re
import tomllib
import unittest
from pathlib import Path

from harness import __version__

ROOT = Path(__file__).resolve().parent.parent


class ReleaseConsistencyTests(unittest.TestCase):
    """The same version is declared in four places; a release must keep them in step."""

    def test_versions_match(self):
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        plugin = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        market = json.loads((ROOT / ".github" / "plugin" / "marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual(pyproject["project"]["version"], __version__)
        self.assertEqual(plugin["version"], __version__)
        self.assertEqual(market["metadata"]["version"], __version__)
        self.assertEqual(market["plugins"][0]["version"], __version__)

    def test_plugin_and_marketplace_names_agree(self):
        plugin = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        market = json.loads((ROOT / ".github" / "plugin" / "marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual(market["plugins"][0]["name"], plugin["name"])
        self.assertTrue((ROOT / plugin["hooks"]).is_file())

    def test_no_personal_paths_in_published_files(self):
        for name in ("README.md", "plugin.json", "pyproject.toml"):
            self.assertIsNone(re.search(r"C:\\Users\\[A-Za-z]", (ROOT / name).read_text(encoding="utf-8")), name)
        for path in (ROOT / "docs").glob("*.md"):
            self.assertIsNone(re.search(r"C:\\Users\\[A-Za-z]", path.read_text(encoding="utf-8")), path.name)


if __name__ == "__main__":
    unittest.main()
