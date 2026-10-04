import json
import os
import sys
from datetime import UTC, datetime
from typing import Any, Callable

from .errors import HarnessError
from .redaction import redact_value
from .storage import Event, Storage


_EVENT_ALIASES = {
    "sessionStart": "sessionStart",
    "SessionStart": "sessionStart",
    "userPromptSubmitted": "userPromptSubmitted",
    "UserPromptSubmit": "userPromptSubmitted",
    "postToolUse": "postToolUse",
    "PostToolUse": "postToolUse",
    "postToolUseFailure": "postToolUseFailure",
    "PostToolUseFailure": "postToolUseFailure",
    "agentStop": "agentStop",
    "Stop": "agentStop",
    "sessionEnd": "sessionEnd",
    "SessionEnd": "sessionEnd",
}
def _timestamp(value: Any) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat()
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC).isoformat()
        except ValueError as exc:
            raise HarnessError("Hook timestamp must be Unix milliseconds or ISO 8601.") from exc
    raise HarnessError("Hook payload is missing a valid timestamp.")


def normalize_hook(payload: Any, event_name: str | None = None) -> Event:
    if not isinstance(payload, dict):
        raise HarnessError("Hook input must be a JSON object.")
    raw_name = payload.get("hook_event_name")
    camel_case = not isinstance(raw_name, str)
    if camel_case:
        raw_name = payload.get("eventName") or payload.get("event_name") or event_name
    if not isinstance(raw_name, str) or raw_name not in _EVENT_ALIASES:
        raise HarnessError("Hook event name is missing or unsupported.")
    event_name = _EVENT_ALIASES[raw_name]

    def field(camel: str, snake: str) -> Any:
        return payload.get(camel if camel_case else snake)

    session_id = field("sessionId", "session_id")
    timestamp = field("timestamp", "timestamp")
    cwd = field("cwd", "cwd")
    if not isinstance(session_id, str) or not session_id.strip():
        raise HarnessError("Hook payload is missing sessionId.")
    if not isinstance(cwd, str) or not cwd.strip():
        raise HarnessError("Hook payload is missing cwd.")
    tool_name = field("toolName", "tool_name")
    tool_args = field("toolArgs", "tool_input")
    tool_result = field("toolResult", "tool_result")
    error = field("error", "error")

    details: dict[str, Any] = {}
    for key in (
        "source", "initialPrompt", "initial_prompt", "prompt", "reason",
        "stopReason", "stop_reason", "transcriptPath", "transcript_path",
    ):
        if key in payload:
            details[key] = payload[key]
    if tool_args is not None:
        details["toolArgs"] = tool_args
    if tool_result is not None:
        details["toolResult"] = tool_result
    if error is not None:
        details["error"] = error
    details = redact_value(details)

    outcome = "failure" if event_name == "postToolUseFailure" else (
        "success" if event_name == "postToolUse" else None
    )
    return Event(
        session_id=session_id.strip(),
        event_name=event_name,
        occurred_at=_timestamp(timestamp),
        cwd=cwd,
        tool_name=str(tool_name) if tool_name is not None else None,
        outcome=outcome,
        details=details,
    )


def handle_hook(
    storage: Storage,
    stdin: Any = None,
    stdout: Any = None,
    event_name: str | None = None,
    context_provider: Callable[[Event], str | None] | None = None,
) -> Event:
    if os.environ.get("COPILOT_HARNESS_REFLECTION") == "1":
        (stdout or sys.stdout).write("{}\n")
        return Event("", "", "", "", None, None, {})
    source = stdin or sys.stdin
    destination = stdout or sys.stdout
    try:
        payload = json.load(source)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HarnessError("Copilot hook input was not valid JSON.") from exc
    event = normalize_hook(payload, event_name=event_name)
    storage.add_event(event)
    output: dict[str, str] = {}
    if event.event_name == "sessionStart" and context_provider is not None:
        try:
            context = context_provider(event)
        except (HarnessError, OSError):
            context = None
        if context:
            output["additionalContext"] = context
    destination.write(json.dumps(output, ensure_ascii=False) + "\n")
    return event
