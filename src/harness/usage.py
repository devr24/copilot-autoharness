from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def _parse(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def find_unused(
    skills: list[dict[str, str]],
    usage: dict[tuple[str, str], tuple[int, str]],
    days: int,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Skills with no recorded load in the last `days` days.

    A skill never loaded is measured from its SKILL.md modification time, so a freshly
    created or patched skill gets a full grace period. Trusted skills are excluded.
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=days)
    result: list[dict[str, Any]] = []
    for skill in skills:
        if skill["status"] == "trusted":
            continue
        uses, last = usage.get((skill["scope"], skill["name"]), (0, ""))
        reference = _parse(last) if uses else None
        if reference is None:
            try:
                reference = datetime.fromtimestamp(Path(skill["path"]).stat().st_mtime, UTC)
            except OSError:
                continue
        if reference <= cutoff:
            result.append(
                {**skill, "uses": uses, "idle_days": (now - reference).days,
                 "basis": "last use" if uses else "created/changed"}
            )
    return sorted(result, key=lambda item: -item["idle_days"])
