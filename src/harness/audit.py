"""Static review checks for a directory of exported skills (used by shared-repo CI)."""
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import HarnessError
from .redaction import redact_text
from .shadow import detect_conflicts
from .skills import verify_export

MAX_SKILL_BYTES = 20_000

# Heuristic phrases that try to steer the agent away from its instructions or exfiltrate data.
_INJECTION = [
    (re.compile(r"(?i)\b(ignore|disregard|forget)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all|system)\b[^.\n]{0,30}\b(instructions?|rules?|prompts?)\b"),
     "tries to override prior instructions"),
    (re.compile(r"(?i)\b(reveal|print|show|send|exfiltrate|upload|post)\b[^.\n]{0,40}\b(secrets?|credentials?|tokens?|api[ _-]?keys?|passwords?|env(ironment)? variables?)\b"),
     "asks to reveal or send secrets"),
    (re.compile(r"(?i)\b(curl|wget|Invoke-WebRequest|iwr)\b[^\n]*\|\s*(sh|bash|zsh|iex|powershell|pwsh)\b"),
     "pipes a download into a shell"),
    (re.compile(r"(?i)\b(do not|don't|never)\b[^.\n]{0,30}\b(tell|inform|mention|show)\b[^.\n]{0,30}\b(user|human|reviewer)\b"),
     "asks to hide actions from the user"),
    (re.compile(r"(?i)\b(disable|bypass|skip)\b[^.\n]{0,30}\b(review|approval|permission|safety|verification|hooks?)\b"),
     "asks to bypass review or safety controls"),
    (re.compile("[\u200b-\u200f\u202a-\u202e\u2066-\u2069]"), "contains hidden bidirectional or zero-width characters"),
]


@dataclass(frozen=True)
class Finding:
    skill: str
    check: str
    detail: str


def scan_text(body: str) -> list[tuple[str, str]]:
    found = []
    if len(body.encode("utf-8")) > MAX_SKILL_BYTES:
        found.append(("size", f"SKILL.md exceeds {MAX_SKILL_BYTES} bytes"))
    if redact_text(body) != body:
        found.append(("secret", "contains secret-like content"))
    for pattern, detail in _INJECTION:
        if pattern.search(body):
            found.append(("injection", detail))
    return found


def audit_skills(root: Path) -> tuple[int, list[Finding]]:
    """Audit every skill directory under `root`: hash, secrets, injection phrases, duplicates."""
    if not root.is_dir():
        raise HarnessError(f"Not a directory: {root}")
    directories = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    findings: list[Finding] = []
    bodies: dict[str, str] = {}
    for directory in directories:
        name = directory.name
        try:
            verify_export(directory)
        except HarnessError as exc:
            findings.append(Finding(name, "verify", str(exc)))
            continue
        body = (directory / "SKILL.md").read_text(encoding="utf-8")
        bodies[name] = body
        findings.extend(Finding(name, check, detail) for check, detail in scan_text(body))
    for name, body in bodies.items():
        others = [{"scope": "shared", "name": n, "content": b} for n, b in bodies.items() if n > name]
        for conflict in detect_conflicts(body, others):
            findings.append(Finding(name, conflict.kind, f"{conflict.other}: {conflict.detail}"))
    return len(directories), findings
