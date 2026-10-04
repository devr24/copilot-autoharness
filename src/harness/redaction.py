import re
from collections.abc import Mapping, Sequence
from typing import Any


_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b(password|passwd|pwd|token|api[_-]?key|client[_-]?secret|access[_-]?token)\s*[:=]\s*[\"']?[^\"'\s,;]+"),
    re.compile(r"(?i)\b([A-Z][A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|ACCESS_KEY))\s*=\s*[\"']?[^\"'\s,;]+"),
    re.compile(r"(?i)(AccountKey|SharedAccessKey|ClientSecret)\s*=\s*[^;\s]+"),
    re.compile(r"(://[^:/\s]+:)[^@/\s]+(@)"),
)


def redact_text(value: str) -> str:
    """Replace common credential formats before text is persisted."""
    value = _PATTERNS[0].sub("[REDACTED PRIVATE KEY]", value)
    value = _PATTERNS[1].sub("[REDACTED TOKEN]", value)
    value = _PATTERNS[2].sub("[REDACTED AWS KEY]", value)
    value = _PATTERNS[3].sub("Bearer [REDACTED TOKEN]", value)
    value = _PATTERNS[4].sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    value = _PATTERNS[5].sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    value = _PATTERNS[6].sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    return _PATTERNS[7].sub(r"\1[REDACTED]\2", value)


def redact_value(value: Any, *, max_string: int = 8000, depth: int = 0) -> Any:
    """Bound and redact nested hook data so captured evidence stays local and small."""
    if depth > 12:
        return "[TRUNCATED]"
    if isinstance(value, str):
        text = redact_text(value)
        if len(text) > max_string:
            return text[:max_string] + " [TRUNCATED]"
        return text
    if isinstance(value, Mapping):
        return {
            redact_text(str(key)): redact_value(item, max_string=max_string, depth=depth + 1)
            for key, item in list(value.items())[:200]
        }
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [redact_value(item, max_string=max_string, depth=depth + 1) for item in value[:200]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(str(value))
