import unittest
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from harness.hooks import handle_hook, normalize_hook
from harness.storage import Storage


class HookTests(unittest.TestCase):
    def test_normalizes_camel_case_hook(self):
        event = normalize_hook(
            {
                "sessionId": "session-a",
                "timestamp": 1_700_000_000_000,
                "cwd": "C:\\repo",
                "eventName": "postToolUse",
                "toolName": "powershell",
                "toolArgs": {"command": "echo password=topsecret"},
                "toolResult": {"textResultForLlm": "done"},
            }
        )
        self.assertEqual(event.session_id, "session-a")
        self.assertEqual(event.tool_name, "powershell")
        self.assertEqual(event.outcome, "success")
        self.assertNotIn("topsecret", str(event.details))

    def test_normalizes_pascal_case_hook_and_captures(self):
        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "state.db")
            input_text = (
                '{"hook_event_name":"SessionEnd","session_id":"s1",'
                '"timestamp":"2026-01-01T00:00:00Z","cwd":"/repo","reason":"complete"}'
            )
            output = StringIO()
            handle_hook(storage, StringIO(input_text), output)
            self.assertEqual(output.getvalue(), "{}\n")
            events = storage.session_events("s1", "/repo")
            self.assertEqual(events[0]["event"], "sessionEnd")

    def test_event_name_from_hook_configuration_is_used(self):
        event = normalize_hook(
            {
                "sessionId": "s2",
                "timestamp": 1_700_000_000_000,
                "cwd": "/repo",
                "toolName": "bash",
            },
            event_name="postToolUse",
        )
        self.assertEqual(event.event_name, "postToolUse")


if __name__ == "__main__":
    unittest.main()
