import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Config
from .errors import HarnessError
from .redaction import redact_text
from .reflection import _extract_object, _run_reflector
from .skills import classify_risk, inspect_skill, list_skills, merge_skills, validate_skill
from .storage import Storage

_MAX_SKILLS = 30
_MAX_BODY = 3000
_MAX_MERGES = 3


def _skill_texts(cwd: Path, scope: str) -> list[dict[str, str]]:
    result = []
    for skill in list_skills(cwd, scope)[:_MAX_SKILLS]:
        _, _, body = inspect_skill(skill["name"], scope, cwd)
        result.append({"name": skill["name"], "status": skill["status"], "content": body[:_MAX_BODY]})
    return result


def build_consolidation_prompt(scope: str, skills: list[dict[str, str]]) -> str:
    listing = json.dumps(skills, ensure_ascii=False, indent=2)
    prompt = f"""You are curating a library of GitHub Copilot Agent Skills ({scope} scope) that were auto-generated from engineering sessions.
Find skills that cover the SAME scenario and should be one skill. Return exactly one JSON object, no Markdown fences:
{{
  "merges": [
    {{
      "keep": "name of the skill that survives",
      "absorb": ["names of skills folded into it"],
      "description": "Copilot skill-selection description for the merged skill",
      "reason": "why these are the same scenario",
      "confidence": 0.0,
      "skill_md": "complete merged SKILL.md content, including YAML frontmatter whose name is the keep name"
    }}
  ]
}}
Rules: return {{"merges": []}} unless skills clearly duplicate each other. Only use names from the list.
Skills that merely share a topic but cover different tasks must stay separate. The merged skill must
keep every distinct instruction from the originals and must not add anything new. Never include
credentials or private data. Use each skill name at most once across all merges.

Skills:

{listing}
"""
    return prompt if len(prompt) <= 40000 else prompt[:39900] + "\n[Truncated. Return valid JSON.]\n"


def consolidate(
    storage: Storage,
    config: Config,
    cwd: Path,
    scope: str,
    *,
    auto: bool,
) -> list[dict[str, Any]]:
    """Ask the reflector for same-scenario merges; apply low-risk ones when auto is set."""
    skills = _skill_texts(cwd, scope)
    if len(skills) < 2:
        return []
    output = _run_reflector(
        config.reflect_command, build_consolidation_prompt(scope, skills), config.reflect_timeout
    )
    merges = _extract_object(output).get("merges")
    if not isinstance(merges, list):
        raise HarnessError("Consolidation output must contain a merges list.")
    bodies = {item["name"]: item["content"] for item in skills}
    statuses = {item["name"]: item["status"] for item in skills}
    used: set[str] = set()
    results: list[dict[str, Any]] = []
    for merge in merges[:_MAX_MERGES]:
        if not isinstance(merge, dict):
            continue
        keep, absorb = merge.get("keep"), merge.get("absorb")
        description, reason = merge.get("description"), merge.get("reason")
        confidence, skill_md = merge.get("confidence"), merge.get("skill_md")
        if (
            not isinstance(keep, str)
            or not isinstance(absorb, list)
            or not absorb
            or not all(isinstance(name, str) for name in absorb)
            or not isinstance(description, str)
            or not isinstance(reason, str)
            or not isinstance(skill_md, str)
            or not isinstance(confidence, (int, float))
            or not 0 <= confidence <= 1
        ):
            continue
        involved = [keep, *absorb]
        if (
            any(name not in bodies for name in involved)
            or len(set(involved)) != len(involved)
            or used & set(involved)
        ):
            continue
        description, reason = redact_text(description), redact_text(reason)
        if "[REDACTED" in description or "[REDACTED" in reason:
            continue
        try:
            body = validate_skill(keep, description, redact_text(skill_md))
        except HarnessError:
            continue
        used.update(involved)
        proposal = {
            "id": str(uuid.uuid4()),
            "action": "merge",
            "scope": scope,
            "name": keep,
            "description": description,
            "reason": reason,
            "confidence": float(confidence),
            "risk_level": classify_risk(
                keep, description, reason, "\n".join([body, *(bodies[name] for name in absorb)])
            ),
            "skill_md": body,
            "evidence": [f"skill:{name}" for name in involved],
            "source_session_ids": [],
            "absorbs": list(absorb),
        }
        storage.add_proposal(proposal, str(cwd), datetime.now(UTC).isoformat())
        results.append({"proposal": proposal, "applied": False})
        holds_trusted = any(statuses[name] == "trusted" for name in involved)
        if auto and proposal["risk_level"] == "low" and not holds_trusted:
            merge_skills(proposal, cwd)
            storage.set_proposal_status(proposal["id"], "accepted")
            results[-1]["applied"] = True
    return results


def maybe_consolidate(storage: Storage, config: Config, cwd: Path) -> list[dict[str, Any]]:
    """Run the periodic curator pass when enough tool activity has accrued."""
    if config.mode != "auto" or not config.consolidate_enabled or not config.reflect_command:
        return []
    key = f"consolidated:{cwd}"
    last = int(storage.get_meta(key) or 0)
    tools, newest = storage.tool_events_after(str(cwd), last)
    if tools < config.consolidate_every:
        return []
    scopes = [
        scope
        for scope in ("project", "personal")
        if len(list_skills(cwd, scope)) >= config.consolidate_min_skills
    ]
    storage.set_meta(key, str(newest))
    results: list[dict[str, Any]] = []
    for scope in scopes:
        results.extend(consolidate(storage, config, cwd, scope, auto=True))
    return results
