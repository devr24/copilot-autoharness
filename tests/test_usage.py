import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from harness.usage import find_unused


class UnusedTests(unittest.TestCase):
    def test_find_unused(self):
        now = datetime(2026, 10, 4, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as tmp:
            def skill(name, status="probation", age_days=60):
                path = Path(tmp) / name
                path.write_text("x", encoding="utf-8")
                stamp = (now - timedelta(days=age_days)).timestamp()
                os.utime(path, (stamp, stamp))
                return {"name": name, "scope": "project", "status": status, "path": str(path)}

            skills = [skill("old-never"), skill("new-never", age_days=3), skill("trusted-old", "trusted"),
                      skill("recent-use"), skill("stale-use")]
            usage = {("project", "recent-use"): (4, "2026-10-01T00:00:00Z"),
                     ("project", "stale-use"): (1, "2026-07-01T00:00:00Z")}
            found = find_unused(skills, usage, 30, now)
        self.assertEqual([s["name"] for s in found], ["stale-use", "old-never"])
        self.assertEqual(found[0]["basis"], "last use")
        self.assertEqual(found[1]["basis"], "created/changed")


if __name__ == "__main__":
    unittest.main()

