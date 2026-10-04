import json
import os
import re
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Config
from .errors import HarnessError
from .index import format_index_lines
from .redaction import redact_text
from .skills import (
    classify_risk,
    list_skills,
    managed_skill_context,
    promote,
    skill_index,
    validate_skill,
)
from .storage import Storage
from .transcript import message_hash, read_conversation


_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", re.IGNORECASE)
_TOOL_EVENTS = {"postToolUse", "postToolUseFailure"}


def select_relevant_skills(
    skills: list[dict[str, str]], text: str, limit: int = 6
) -> list[dict[str, str]]:
    words = {word for word in re.findall(r"[a-z0-9]{4,}", text.lower())}

    def score(skill: dict[str, str]) -> int:
        own = set(re.findall(r"[a-z0-9]{4,}", f"{skill['name']} {skill['content']}".lower()))
        return len(words & own)

    return sorted(skills, key=score, reverse=True)[:limit]


def build_reflection_prompt(
    session_id: str,
    events: list[dict[str, Any]],
    skills: list[dict[str, str]],
    conversation: list[dict[str, str]] | None = None,
    index: list[dict[str, str]] | None = None,
) -> str:
    bounded_events = []
    for event in events[-15:]:
        item = dict(event)
        details = json.dumps(item.get("details", {}), ensure_ascii=False)
        if len(details) > 500:
            item["details"] = details[:500] + " [TRUNCATED]"
        bounded_events.append(item)
    bounded_skills = [
        {**skill, "content": skill["content"][:1000]}
        for skill in skills[:6]
    ]
    evidence = json.dumps(bounded_events, ensure_ascii=False, indent=2)
    existing_skills = json.dumps(bounded_skills, ensure_ascii=False, indent=2)
    index_lines = "\n".join(format_index_lines(index or [], 100, 60)) or "(none yet)"
    dialogue = (
        "\n".join(f"[{item['role']}] {item['text']}" for item in conversation)
        if conversation
        else "(conversation text was not available for this window)"
    )
    prompt = f"""You are proposing reusable GitHub Copilot Agent Skills from one engineering session.
Return exactly one JSON object, with no Markdown fences or other text, using this schema:
{{
  "action": "create" | "patch" | "ignore",
  "scope": "project" | "personal",
  "name": "lowercase-kebab-case",
  "description": "Copilot skill-selection description",
  "reason": "why the lesson is reusable",
  "confidence": 0.0,
  "skill_md": "complete SKILL.md content, including YAML frontmatter",
  "target_name": "existing Harness skill name for patch, otherwise empty"
}}
Choose ignore unless the evidence demonstrates a repeatable, non-trivial lesson.
Use the conversation as the primary evidence: user corrections, stated preferences, repeated
instructions, and dead ends that were later resolved are the strongest signals. Use the tool
events to confirm what was actually run. Never include credentials, private data, or one-off
task details. Prefer project scope when repository-specific. Compare with the existing
Harness-owned skills below: patch a same-scenario skill instead of creating a near-duplicate.
For patch, target_name must exactly identify a listed Harness-owned skill and name must equal
target_name. Do not change an unrelated skill merely to reduce the skill count. Do not invent
evidence or claim skill usage was observed.
The session identifier is {session_id!r}. The evidence below was redacted before use.

Conversation (oldest first, truncated):

{dialogue}

Tool events:

{evidence}

Existing Harness-owned skills (complete index; check it before proposing a create):

{index_lines}

Full text of the existing skills most relevant to this session (patch targets must come from the index):

{existing_skills}
"""
    if len(prompt) > 32000:
        prompt = prompt[:31900] + "\n[Reflection context truncated. Return valid JSON.]\n"
    return prompt


def _extract_object(output: str) -> dict[str, Any]:
    text = output.strip()
    fenced = _FENCED_JSON.search(text)
    if fenced:
        text = fenced.group(1)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise HarnessError("Reflector output did not contain a JSON object.")
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise HarnessError(f"Reflector returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise HarnessError("Reflector output must be a JSON object.")
    return value


def _run_reflector(command: tuple[str, ...], prompt: str, timeout: int) -> str:
    if not command:
        raise HarnessError(
            "No reflection command is configured. Run `harness config init`, then set "
            "[reflection].command to a trusted command that reads a prompt from stdin and returns JSON."
        )
    args = list(command)
    prompt = prompt.encode("utf-8", "replace").decode("utf-8")
    try:
        environment = os.environ.copy()
        environment["COPILOT_HARNESS_REFLECTION"] = "1"
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            input=prompt,
            timeout=timeout,
            check=False,
            env=environment,
        )
    except FileNotFoundError as exc:
        raise HarnessError(f"Reflection command not found: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise HarnessError(f"Reflection command timed out after {timeout} seconds.") from exc
    except OSError as exc:
        raise HarnessError(f"Could not run reflection command: {exc}") from exc
    if result.returncode != 0:
        error = result.stderr.strip()[:1500] or "no error output"
        raise HarnessError(f"Reflection command failed ({result.returncode}): {error}")
    return result.stdout


def learn(
    storage: Storage,
    config: Config,
    cwd: Path,
    session_id: str | None = None,
    force: bool = False,
) -> dict[str, Any] | None:
    if config.mode == "off":
        raise HarnessError("Learning is disabled by mode = 'off'.")
    chosen_id = session_id or storage.latest_session(str(cwd))
    if not chosen_id:
        raise HarnessError("No captured Copilot session found for this directory.")
    marked_id, marked_at = storage.reflection_mark(chosen_id, str(cwd))
    events = storage.session_events(chosen_id, str(cwd), after_id=marked_id)
    if not events:
        raise HarnessError(
            f"No new captured events for session {chosen_id} in this directory since the last reflection."
        )
    if not force and not session_is_ready(events, config):
        tool_count = count_tool_events(events)
        raise HarnessError(
            f"Session has {tool_count} new relevant tool events; "
            f"the configured reflection threshold is {config.tool_calls_before_learn}. "
            "Use `harness learn --now` to reflect anyway."
        )
    transcript = storage.latest_transcript_path(chosen_id, str(cwd))
    conversation = read_conversation(
        Path(transcript) if transcript else None,
        since=marked_at,
        skip=storage.seen_message_hashes(str(cwd)),
    )
    reflected_hashes = {h for h in map(message_hash, conversation) if h}
    if not conversation:
        conversation = [
            {"role": "user", "text": event["details"]["prompt"]}
            for event in events
            if event["event"] == "userPromptSubmitted"
            and isinstance(event["details"].get("prompt"), str)
        ][-12:]
    prompt = build_reflection_prompt(
        chosen_id,
        events,
        select_relevant_skills(
            managed_skill_context(cwd),
            " ".join(item["text"] for item in conversation)
            + " "
            + json.dumps([event["details"] for event in events[-15:]], ensure_ascii=False),
        ),
        conversation,
        skill_index(cwd),
    )
    output = _run_reflector(config.reflect_command, prompt, config.reflect_timeout)
    # The reflector answered, so this window is consumed even if the answer is rejected below.
    storage.add_message_hashes(str(cwd), reflected_hashes)
    storage.set_reflection_mark(
        chosen_id,
        str(cwd),
        events[-1]["id"],
        events[-1]["timestamp"],
        datetime.now(UTC).isoformat(),
    )
    proposal = _extract_object(output)
    action = proposal.get("action")
    if action == "ignore":
        return None
    if action not in {"create", "patch"}:
        raise HarnessError("Reflector action must be create, patch, or ignore.")
    scope = proposal.get("scope")
    if scope not in {"project", "personal"}:
        raise HarnessError("Reflector scope must be project or personal.")
    name = proposal.get("target_name") if action == "patch" else proposal.get("name")
    description = proposal.get("description")
    reason = proposal.get("reason")
    confidence = proposal.get("confidence")
    skill_md = proposal.get("skill_md")
    if action == "patch" and proposal.get("name") != name:
        raise HarnessError("Patch name must match target_name.")
    if not isinstance(name, str) or not isinstance(description, str) or not isinstance(reason, str):
        raise HarnessError("Reflector omitted required name, description, or reason fields.")
    description = redact_text(description)
    reason = redact_text(reason)
    if "[REDACTED" in description or "[REDACTED" in reason:
        raise HarnessError("Reflector output contained a value matching a secret pattern.")
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise HarnessError("Reflector confidence must be a number between 0 and 1.")
    if not isinstance(skill_md, str):
        raise HarnessError("Reflector omitted skill_md.")
    skill_md = validate_skill(name, description, redact_text(skill_md))
    evidence_ids = [f"event:{event['id']}" for event in events]
    if not evidence_ids:
        raise HarnessError("Refusing to create a proposal without captured evidence.")
    result = {
        "id": str(uuid.uuid4()),
        "action": action,
        "scope": scope,
        "name": name,
        "description": description,
        "reason": reason,
        "confidence": float(confidence),
        "risk_level": classify_risk(name, description, reason, skill_md),
        "skill_md": skill_md,
        "evidence": evidence_ids,
        "source_session_ids": [chosen_id],
    }
    if action == "patch":
        target = next(
            (
                item
                for item in managed_skill_context(cwd)
                if item["scope"] == scope and item["name"] == name
            ),
            None,
        )
        if target is None:
            raise HarnessError(f"Patch target is not a Harness-managed {scope} skill: {name}")
    storage.add_proposal(result, str(cwd), datetime.now(UTC).isoformat())
    return result


def count_tool_events(events: list[dict[str, Any]]) -> int:
    return sum(1 for event in events if event["tool"] or event["event"] in _TOOL_EVENTS)


def session_is_ready(events: list[dict[str, Any]], config: Config) -> bool:
    return count_tool_events(events) >= config.tool_calls_before_learn


def auto_learn(
    storage: Storage,
    config: Config,
    cwd: Path,
    session_id: str,
    force: bool = False,
) -> tuple[dict[str, Any] | None, Path | None]:
    if config.mode != "auto":
        return None, None
    marked_id, _ = storage.reflection_mark(session_id, str(cwd))
    events = storage.session_events(session_id, str(cwd), after_id=marked_id)
    if not events or not (force or session_is_ready(events, config)):
        return None, None
    proposal = learn(storage, config, cwd, session_id, force=force)
    if proposal is None:
        return None, None
    if proposal["risk_level"] == "sensitive":
        return proposal, None
    if proposal["action"] == "patch":
        target = next(
            (
                skill
                for skill in list_skills(cwd, proposal["scope"])
                if skill["name"] == proposal["name"]
            ),
            None,
        )
        if target and target["status"] == "trusted":
            return proposal, None
    capacity = (
        config.project_skill_capacity
        if proposal["scope"] == "project"
        else config.personal_skill_capacity
    )
    path = promote(proposal, cwd, capacity)
    storage.set_proposal_status(proposal["id"], "accepted")
    return proposal, path
