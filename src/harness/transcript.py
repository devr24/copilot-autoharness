import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .redaction import redact_text

_MAX_MESSAGE_CHARS = 700
_MAX_TOTAL_CHARS = 9000
_MAX_LINE_BYTES = 2_000_000
_MIN_DEDUPE_CHARS = 40


def message_hash(message: dict[str, str]) -> str | None:
    """Stable fingerprint used to recognise messages already reflected on (forks, resumes).

    Very short messages ("ok", "thanks") are never fingerprinted so they are not dropped.
    """
    if len(message["text"]) < _MIN_DEDUPE_CHARS:
        return None
    payload = f"{message['role']}\n{message['text']}".encode("utf-8", "replace")
    return hashlib.sha256(payload).hexdigest()[:24]


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def read_conversation(
    path: Path | None,
    since: str | None = None,
    max_total: int = _MAX_TOTAL_CHARS,
    max_message: int = _MAX_MESSAGE_CHARS,
    skip: frozenset[str] | set[str] = frozenset(),
) -> list[dict[str, str]]:
    """Extract redacted user and assistant text from a Copilot session transcript.

    Subagent records (which carry a top-level agentId) are skipped: their prompts are written by the
    parent agent, not the user, and the parent relays their results. Messages whose fingerprint is in
    `skip` (already reflected, e.g. history copied into a forked session) are dropped.
    Tool calls, system prompts, and model telemetry are skipped. Unknown or unreadable
    transcripts yield an empty conversation so reflection falls back to captured events.
    """
    if path is None or path.suffix != ".jsonl":
        return []
    cutoff = _parse_time(since)
    messages: list[dict[str, str]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if len(line) > _MAX_LINE_BYTES:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or record.get("agentId"):
                    continue
                kind = record.get("type")
                role = {"user.message": "user", "assistant.message": "assistant"}.get(kind)
                data = record.get("data")
                if role is None or not isinstance(data, dict):
                    continue
                text = data.get("content")
                if not isinstance(text, str) or not text.strip():
                    continue
                when = _parse_time(record.get("timestamp"))
                if cutoff and when and when <= cutoff:
                    continue
                text = redact_text(text.strip())
                if len(text) > max_message:
                    text = text[:max_message] + " [TRUNCATED]"
                message = {"role": role, "text": text}
                if skip and message_hash(message) in skip:
                    continue
                messages.append(message)
    except OSError:
        return []
    if not messages:
        return []
    first, rest = messages[0], messages[1:]
    selected: list[dict[str, str]] = []
    budget = max_total - len(first["text"])
    for message in reversed(rest):
        budget -= len(message["text"])
        if budget < 0:
            break
        selected.append(message)
    selected.reverse()
    return [first, *selected]


def read_skill_invocations(path: Path | None) -> list[dict[str, str]]:
    """Return skill.invoked records: Copilot's authoritative signal that a skill was loaded."""
    if path is None or path.suffix != ".jsonl":
        return []
    found: list[dict[str, str]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if '"skill.invoked"' not in line or len(line) > _MAX_LINE_BYTES:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                data = record.get("data") if isinstance(record, dict) else None
                if record.get("type") != "skill.invoked" or not isinstance(data, dict):
                    continue
                name = data.get("name")
                if not isinstance(name, str) or not name:
                    continue
                source = data.get("source")
                found.append(
                    {
                        "name": name,
                        "scope": "project" if source == "project" else "personal",
                        "at": str(record.get("timestamp") or ""),
                        "plugin": str(data.get("pluginName") or ""),
                    }
                )
    except OSError:
        return []
    return found
