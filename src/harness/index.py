from pathlib import Path

from .config import Config
from .errors import HarnessError
from .skills import skill_index

_STATE_ORDER = {"trusted": 0, "probation": 1}


def _clean(text: str, limit: int) -> str:
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: max(limit - 1, 1)].rstrip() + "…"


def format_index_lines(
    entries: list[dict[str, str]], desc_chars: int, max_lines: int
) -> list[str]:
    ordered = sorted(
        entries, key=lambda item: (_STATE_ORDER.get(item["status"], 2), item["scope"], item["name"])
    )
    lines = [
        f"- {item['name']} ({item['scope']}, {item['status']}): "
        f"{_clean(item['description'], desc_chars) or 'no description'}"
        for item in ordered[:max_lines]
    ]
    if len(ordered) > max_lines:
        lines.append(f"- (+{len(ordered) - max_lines} more; run `harness skills` to list them)")
    return lines


def session_start_context(config: Config, cwd: Path) -> str | None:
    """Compact index of learned skills for sessionStart additionalContext."""
    if config.mode == "off" or not config.inject_index:
        return None
    try:
        entries = skill_index(cwd)
    except (HarnessError, OSError):
        return None
    if not entries:
        return None
    lines = format_index_lines(entries, config.index_desc_chars, config.index_max_lines)
    return (
        "Copilot Harness has learned these reusable skills from earlier sessions. "
        "They are auto-generated: apply one when its description matches the task, and treat "
        "probation skills as unverified.\n" + "\n".join(lines)
    )
