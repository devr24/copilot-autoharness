import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .errors import HarnessError
from .reflection import _extract_object, _run_reflector, build_reflection_prompt, select_relevant_skills
from .redaction import redact_text
from .skills import validate_skill


@dataclass
class CaseResult:
    name: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    action: str = ""


def load_cases(directory: Path) -> list[dict[str, Any]]:
    cases = []
    for path in sorted(directory.glob("*.json")):
        try:
            case = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise HarnessError(f"Invalid eval case {path.name}: {exc}") from exc
        if not isinstance(case, dict) or "conversation" not in case or "expect" not in case:
            raise HarnessError(f"Eval case {path.name} needs 'conversation' and 'expect'.")
        case.setdefault("name", path.stem)
        cases.append(case)
    return cases


def build_case_prompt(case: dict[str, Any]) -> str:
    skills = case.get("existing_skills", [])
    context = [
        {"name": s["name"], "scope": s.get("scope", "project"), "content": s["content"]} for s in skills
    ]
    index = [
        {"name": s["name"], "scope": s.get("scope", "project"), "status": s.get("status", "probation"),
         "description": s.get("description", "")}
        for s in skills
    ]
    text = " ".join(m["text"] for m in case["conversation"])
    events = [
        {"id": i + 1, "event": "postToolUse", "tool": e.get("tool", ""), "outcome": e.get("outcome", "success"),
         "details": e.get("details", {}), "timestamp": f"2026-01-01T00:00:{i:02d}Z"}
        for i, e in enumerate(case.get("tool_events", []))
    ]
    return build_reflection_prompt(
        "eval-" + case["name"], events, select_relevant_skills(context, text), case["conversation"], index
    )


def score_case(case: dict[str, Any], output: str) -> CaseResult:
    """Score a reflector answer against a case's expectations. Never raises."""
    expect = case["expect"]
    result = CaseResult(case["name"], True)

    def fail(message: str) -> None:
        result.passed = False
        result.failures.append(message)

    try:
        proposal = _extract_object(output)
    except HarnessError as exc:
        fail(f"unparseable output: {exc}")
        return result
    action = proposal.get("action")
    result.action = str(action)
    allowed = expect["action"] if isinstance(expect["action"], list) else [expect["action"]]
    if action not in allowed:
        fail(f"action {action!r}, expected {expect['action']!r}")
    if action in {"create", "patch"}:
        name = proposal.get("target_name") if action == "patch" else proposal.get("name")
        try:
            body = validate_skill(str(name), str(proposal.get("description", "")), str(proposal.get("skill_md", "")))
        except HarnessError as exc:
            fail(f"invalid skill: {exc}")
            body = str(proposal.get("skill_md", ""))
        if action == "patch" and expect.get("patch_target") and name != expect["patch_target"]:
            fail(f"patched {name!r}, expected {expect['patch_target']!r}")
        everything = json.dumps(proposal, ensure_ascii=False).lower()
        for word in expect.get("must_include", []):
            if word.lower() not in everything:
                fail(f"missing expected content: {word!r}")
        if redact_text(body) != body or "[REDACTED" in everything:
            fail("output contains secret-like content")
    everything = json.dumps(proposal, ensure_ascii=False).lower()
    for word in expect.get("must_exclude", []):
        if word.lower() in everything:
            fail(f"contains forbidden content: {word!r}")
    return result


def run_eval(config: Config, directory: Path, only: str | None = None) -> list[CaseResult]:
    results = []
    for case in load_cases(directory):
        if only and only not in case["name"]:
            continue
        try:
            output = _run_reflector(config.reflect_command, build_case_prompt(case), config.reflect_timeout)
            results.append(score_case(case, output))
        except HarnessError as exc:
            results.append(CaseResult(case["name"], False, [f"reflector error: {exc}"]))
    return results
