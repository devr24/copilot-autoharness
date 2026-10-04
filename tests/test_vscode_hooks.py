import json
import unittest
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from harness.hooks import handle_hook, normalize_hook
from harness.storage import Storage


class VSCodePayloadTests(unittest.TestCase):
    def test_minimal_session_start_defaults_cwd_and_timestamp(self):
        event = normalize_hook({"hook_event_name": "SessionStart", "session_id": "s1", "source": "new"})
        self.assertEqual(event.event_name, "sessionStart")
        self.assertTrue(event.cwd)
        self.assertTrue(event.occurred_at)

    def test_post_tool_use_reads_tool_response(self):
        event = normalize_hook({
            "hook_event_name": "PostToolUse", "session_id": "s1", "tool_name": "runInTerminal",
            "tool_input": {"command": "ls"}, "tool_response": "ok",
        })
        self.assertEqual(event.outcome, "success")
        self.assertEqual(event.details["toolResult"], "ok")

    def test_stop_maps_to_agent_stop(self):
        event = normalize_hook({"hook_event_name": "Stop", "session_id": "s1", "stop_hook_active": False})
        self.assertEqual(event.event_name, "agentStop")

    def test_session_id_still_required(self):
        with self.assertRaises(Exception):
            normalize_hook({"hook_event_name": "Stop"})

    def test_session_start_output_includes_hook_specific_output(self):
        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "state.db")
            out = StringIO()
            handle_hook(
                storage,
                stdin=StringIO(json.dumps({"hook_event_name": "SessionStart", "session_id": "s1"})),
                stdout=out,
                context_provider=lambda event: "INDEX",
            )
        result = json.loads(out.getvalue())
        self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertEqual(result["hookSpecificOutput"]["additionalContext"], "INDEX")
        self.assertEqual(result["additionalContext"], "INDEX")


if __name__ == "__main__":
    unittest.main()
