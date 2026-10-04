import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import debug
from harness.background import should_reflect_mid_session
from harness.cli import main
from harness.config import Config, default_config_text, load_config
from harness.errors import HarnessError
from harness.storage import Storage


class DebugLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        patcher = mock.patch.dict(os.environ, {"COPILOT_HARNESS_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("COPILOT_HARNESS_DEBUG", None)

    def log_text(self):
        path = self.home / debug.DEBUG_LOG_NAME
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def write_config(self, text):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.toml").write_text(text, encoding="utf-8")

    def test_off_by_default_and_default_config_documents_it(self):
        debug.debug_log(Config(home=self.home), "hello")
        self.assertEqual(self.log_text(), "")
        self.assertIn("[logging]\ndebug = false", default_config_text())

    def test_config_flag_and_env_override(self):
        self.write_config("[logging]\ndebug = true\n")
        config = load_config()
        self.assertTrue(config.debug)
        debug.debug_log(config, "visible")
        self.assertIn("visible", self.log_text())
        with mock.patch.dict(os.environ, {"COPILOT_HARNESS_DEBUG": "0"}):
            debug.debug_log(config, "suppressed")
        self.assertNotIn("suppressed", self.log_text())
        with mock.patch.dict(os.environ, {"COPILOT_HARNESS_DEBUG": "1"}):
            debug.debug_log(None, "from-env")
        self.assertIn("from-env", self.log_text())

    def test_invalid_flag_rejected(self):
        self.write_config('[logging]\ndebug = "yes"\n')
        with self.assertRaises(HarnessError):
            load_config()

    def test_rotation_and_never_raises(self):
        config = Config(home=self.home, debug=True)
        self.home.mkdir()
        (self.home / debug.DEBUG_LOG_NAME).write_text("x" * (debug._MAX_BYTES + 1), encoding="utf-8")
        debug.debug_log(config, "after rotation")
        self.assertTrue((self.home / (debug.DEBUG_LOG_NAME + ".1")).exists())
        self.assertIn("after rotation", self.log_text())
        with mock.patch("pathlib.Path.open", side_effect=OSError("disk full")):
            debug.debug_log(config, "should not raise")

    def test_hook_run_logs_decisions_but_not_content(self):
        self.write_config('mode = "auto"\n[logging]\ndebug = true\n')
        payload = json.dumps({"sessionId": "sess-1", "cwd": str(self.home), "toolName": "bash",
                              "toolArgs": {"command": "SECRET-COMMAND-TEXT"}, "timestamp": 1})
        with mock.patch("sys.stdin", io.StringIO(payload)), mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(main(["hook", "--event", "postToolUse"]), 0)
        text = self.log_text()
        self.assertIn("hook postToolUse received", text)
        self.assertIn("session=sess-1 tool=bash", text)
        self.assertNotIn("SECRET-COMMAND-TEXT", text)

    def test_failed_command_logs_traceback(self):
        self.write_config("[logging]\ndebug = true\n")
        with mock.patch("sys.stdin", io.StringIO("not json")), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(main(["hook", "--event", "postToolUse"]), 1)
        self.assertIn("ERROR in command hook", self.log_text())
        self.assertIn("Traceback", self.log_text())

    def test_mid_session_decision_reason_logged(self):
        config = Config(home=self.home, mode="auto", reflect_command=("x",), debug=True,
                        tool_calls_before_learn=5)
        storage = Storage(config.database)
        self.assertFalse(should_reflect_mid_session(storage, config, self.home, "s"))
        self.assertIn("below threshold (0/5", self.log_text())

    def test_logs_command(self):
        self.write_config("[logging]\ndebug = true\n")
        debug.debug_log(load_config(), "line-one")
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            self.assertEqual(main(["logs", "-n", "5"]), 0)
        self.assertIn("line-one", out.getvalue())


if __name__ == "__main__":
    unittest.main()
