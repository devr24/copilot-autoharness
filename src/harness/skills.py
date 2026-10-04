import json
import getpass
import hashlib
import os
import re
import shutil
import tempfile
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .errors import HarnessError
from .redaction import redact_text


_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", re.DOTALL)
_DESCRIPTION = re.compile(r"(?m)^description:\s*(.+?)\s*$")
_NAME_FIELD = re.compile(r"(?m)^name:\s*([a-z0-9-]+)\s*$")
_UNSAFE_PATH = re.compile(r"(?m)(?:\]\(|\]\s*:\s*)\s*(?:/|[A-Za-z]:[\\/]|(?:\.\.[\\/]))")
_SECRET_MARKER = "[REDACTED"
_MARKER = "copilot-harness"
_RISK_TERMS = re.compile(
    r"\b("
    r"deploy(?:ment)?s?|production|infrastructure|terraform|bicep|kubernetes|"
    r"iam|permissions?|authorization|authentication|credentials?|secrets?|tokens?|"
    r"security|compliance|(?:database|data) migrations?|drop table|delete data|"
    r"destructive|firewalls?|network configuration|package installations?|"
    r"dependency upgrades?|dependencies|sudo|privileged"
    r")\b",
    re.IGNORECASE,
)


def validate_skill(name: str, description: str, skill_md: str) -> str:
    if not isinstance(name, str) or not _NAME.fullmatch(name) or len(name) > 64:
        raise HarnessError("Skill name must be a lowercase slug of at most 64 characters.")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise HarnessError("Skill description must be non-empty and at most 1024 characters.")
    if not isinstance(skill_md, str):
        raise HarnessError("Skill content must be text.")
    content = redact_text(skill_md).replace("\r\n", "\n").strip() + "\n"
    if len(content.encode("utf-8")) > 12_000:
        raise HarnessError("Skill content exceeds the 12 KB MVP limit.")
    match = _FRONTMATTER.match(content)
    if not match:
        raise HarnessError("SKILL.md must start with YAML frontmatter.")
    header = match.group(1)
    name_match = _NAME_FIELD.search(header)
    description_match = _DESCRIPTION.search(header)
    if not name_match or name_match.group(1) != name:
        raise HarnessError("SKILL.md frontmatter name must match the proposed skill name.")
    if not description_match or not description_match.group(1).strip():
        raise HarnessError("SKILL.md frontmatter must include a non-empty description.")
    frontmatter_description = description_match.group(1).strip().strip("\"'")
    if len(frontmatter_description) > 1024:
        raise HarnessError("SKILL.md frontmatter description exceeds 1024 characters.")
    if frontmatter_description != description.strip().strip("\"'"):
        raise HarnessError("SKILL.md frontmatter description must match the proposal description.")
    if _UNSAFE_PATH.search(content):
        raise HarnessError("Skill contains an absolute or parent-relative file reference.")
    if _SECRET_MARKER in content:
        raise HarnessError("Skill content appeared to contain a secret; review and remove it.")
    return content


def skill_roots(scope: str, cwd: Path) -> tuple[Path, Path]:
    if scope == "project":
        base = cwd.resolve()
        root = base / ".github" / "skills"
        archive = base / ".github" / "copilot-harness" / "archive"
    elif scope == "personal":
        base = Path.home().resolve()
        root = base / ".copilot" / "skills"
        archive = base / ".copilot" / "copilot-harness" / "archive"
    else:
        raise HarnessError("Skill scope must be 'project' or 'personal'.")
    for location in (root, archive):
        _assert_no_symlink_components(base, location)
    return root, archive


def _assert_no_symlink_components(base: Path, location: Path) -> None:
    current = base
    for part in location.relative_to(base).parts:
        current = current / part
        if current.is_symlink():
            raise HarnessError(f"Refusing to use a symlink in a skill directory: {current}")
    if not location.resolve().is_relative_to(base):
        raise HarnessError(f"Skill directory escapes its allowed root: {location}")


def _managed(directory: Path) -> dict[str, Any]:
    if directory.is_symlink():
        raise HarnessError(f"Refusing to modify a skill reached through a symlink: {directory}")
    marker = directory / ".sidecar.json"
    if marker.is_symlink():
        raise HarnessError(f"Refusing to read a symlinked Harness marker: {marker}")
    try:
        with marker.open(encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(f"Skill is not safely identified as Harness-managed: {directory}") from exc
    if not isinstance(data, dict) or data.get("managed_by") != _MARKER:
        raise HarnessError(f"Refusing to modify a human-owned skill: {directory}")
    return data


def _assert_identity(metadata: dict[str, Any], name: str, scope: str) -> None:
    if metadata.get("name") != name or metadata.get("scope") != scope:
        raise HarnessError(f"Harness ownership metadata does not match {scope} skill {name}.")


def classify_risk(name: str, description: str, reason: str, body: str) -> str:
    return "sensitive" if _RISK_TERMS.search(
        f"{name}\n{description}\n{reason}\n{body}"
    ) else "low"


def _version_root(scope: str, cwd: Path, name: str) -> Path:
    _, archive = skill_roots(scope, cwd)
    root = archive.parent / "versions" / name
    base = cwd.resolve() if scope == "project" else Path.home().resolve()
    _assert_no_symlink_components(base, root)
    return root


def _save_version(
    scope: str,
    cwd: Path,
    name: str,
    version: int,
    body: str,
    metadata: dict[str, Any],
    reason: str,
) -> Path:
    root = _version_root(scope, cwd, name)
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{version:06d}"
    if target.is_symlink():
        raise HarnessError(f"Skill version already exists: {target}")
    if target.is_dir():
        existing_body = target / "SKILL.md"
        if not existing_body.is_symlink() and existing_body.is_file():
            if existing_body.read_text(encoding="utf-8") == body:
                return target
        raise HarnessError(f"Skill version already exists with different content: {target}")
    if target.exists():
        raise HarnessError(f"Skill version already exists: {target}")
    staging = Path(tempfile.mkdtemp(prefix=".version-", dir=root))
    try:
        (staging / "SKILL.md").write_text(body, encoding="utf-8", newline="\n")
        (staging / "metadata.json").write_text(
            _sidecar(
                {
                    "version": version,
                    "trust_state": metadata.get("trust_state", "probation"),
                    "risk_level": metadata.get("risk_level", "low"),
                    "approval": metadata.get("approval"),
                    "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                    "saved_at": datetime.now(UTC).isoformat(),
                    "reason": reason,
                }
            ),
            encoding="utf-8",
            newline="\n",
        )
        os.replace(staging, target)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return target


def _append_ledger(directory: Path, entry: dict[str, Any]) -> None:
    ledger = directory / ".ledger.jsonl"
    if ledger.is_symlink():
        raise HarnessError(f"Refusing to write a symlinked evidence ledger: {ledger}")
    with ledger.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _sidecar(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def promote(proposal: dict[str, Any], cwd: Path, capacity: int | None = None) -> Path:
    name = proposal["name"]
    scope = proposal["scope"]
    root, _ = skill_roots(scope, cwd)
    target = root / name
    body = validate_skill(name, proposal["description"], proposal["skill_md"])
    action = proposal["action"]
    timestamp = datetime.now(UTC).isoformat()
    evidence = proposal["evidence"]
    detected_risk = classify_risk(name, proposal["description"], proposal["reason"], body)
    proposal_risk = proposal.get("risk_level", "low")
    if proposal_risk not in {"low", "sensitive"}:
        raise HarnessError(f"Invalid skill risk level: {proposal_risk}")
    risk_level = "sensitive" if "sensitive" in {proposal_risk, detected_risk} else "low"
    if risk_level not in {"low", "sensitive"}:
        raise HarnessError(f"Invalid skill risk level: {risk_level}")
    if risk_level == "sensitive" and not proposal.get("approved_by"):
        raise HarnessError("Sensitive skill requires explicit human approval before promotion.")
    provenance = {
        "created_at": timestamp,
        "created_by": getpass.getuser(),
        "repository": cwd.name if scope == "project" else None,
        "source_session_ids": proposal.get("source_session_ids", []),
        "source_event_ids": evidence,
        "source_proposal": proposal["id"],
        "reason": proposal["reason"],
        "harness_version": __version__,
        "model": None,
        "model_note": "The configured reflector does not expose model identity.",
        "validation": {"result": "passed", "validator": __version__},
    }

    if action == "create":
        if target.exists() or target.is_symlink():
            raise HarnessError(f"Refusing to overwrite an existing skill: {target}")
        if capacity is not None and len(list_skills(cwd, scope)) >= capacity:
            raise HarnessError(f"{scope.title()} skill capacity ({capacity}) has been reached.")
        root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{name}.", dir=root))
        version = None
        version_created = False
        try:
            (staging / "SKILL.md").write_text(body, encoding="utf-8", newline="\n")
            metadata = {
                "managed_by": _MARKER,
                "scope": scope,
                "name": name,
                "status": "probation",
                "trust_state": "probation",
                "risk_level": risk_level,
                "version": 1,
                "provenance": provenance,
                "approval": (
                    {
                        "required": True,
                        "approved_by": proposal["approved_by"],
                        "approved_at": timestamp,
                    }
                    if proposal.get("approved_by")
                    else {"required": False, "approved_by": None, "approved_at": None}
                ),
            }
            (staging / ".sidecar.json").write_text(_sidecar(metadata), encoding="utf-8", newline="\n")
            (staging / ".ledger.jsonl").write_text(
                json.dumps(
                    {
                        "action": "create",
                        "timestamp": timestamp,
                        "version": 1,
                        "risk_level": risk_level,
                        "approved_by": proposal.get("approved_by"),
                        "reason": proposal["reason"],
                        "evidence": evidence,
                        "proposal": proposal["id"],
                        "provenance": provenance,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
            )
            history_root = _version_root(scope, cwd, name)
            history_root.mkdir(parents=True, exist_ok=True)
            version = history_root / "000001"
            if version.exists() or version.is_symlink():
                raise HarnessError(f"Skill version already exists: {version}")
            version_staging = Path(tempfile.mkdtemp(prefix=".version-", dir=history_root))
            try:
                (version_staging / "SKILL.md").write_text(body, encoding="utf-8", newline="\n")
                (version_staging / "metadata.json").write_text(
                    _sidecar(
                        {
                            "version": 1,
                            "trust_state": "probation",
                            "risk_level": risk_level,
                            "approval": metadata["approval"],
                            "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                            "saved_at": timestamp,
                            "reason": proposal["reason"],
                        }
                    ),
                    encoding="utf-8",
                    newline="\n",
                )
                os.replace(version_staging, version)
                version_created = True
            except Exception:
                if version_staging.exists():
                    shutil.rmtree(version_staging)
                raise
            os.replace(staging, target)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            if version_created and version is not None and version.exists():
                shutil.rmtree(version)
            raise
        return target

    if action == "patch":
        if not target.is_dir():
            raise HarnessError(f"Cannot patch a missing skill: {target}")
        metadata = _managed(target)
        _assert_identity(metadata, name, scope)
        skill_file = target / "SKILL.md"
        ledger = target / ".ledger.jsonl"
        sidecar = target / ".sidecar.json"
        if skill_file.is_symlink() or ledger.is_symlink() or sidecar.is_symlink():
            raise HarnessError(f"Refusing to modify symlinked skill files in {target}")
        current_version = metadata.get("version", 1)
        if not isinstance(current_version, int) or current_version < 1:
            raise HarnessError(f"Invalid version metadata for {name}.")
        previous_body = skill_file.read_text(encoding="utf-8")
        _save_version(
            scope,
            cwd,
            name,
            current_version,
            previous_body,
            metadata,
            proposal["reason"],
        )
        _atomic_write(skill_file, body)
        metadata["updated_at"] = timestamp
        metadata["version"] = current_version + 1
        metadata["risk_level"] = risk_level
        metadata["trust_state"] = "probation"
        metadata["status"] = "probation"
        if proposal.get("approved_by"):
            metadata["approval"] = {
                "required": True,
                "approved_by": proposal["approved_by"],
                "approved_at": timestamp,
            }
        metadata["provenance"] = {
            **metadata.get("provenance", {}),
            "updated_at": timestamp,
            "updated_by": getpass.getuser(),
            "source_session_ids": proposal.get("source_session_ids", []),
            "source_event_ids": evidence,
            "source_proposal": proposal["id"],
            "reason": proposal["reason"],
        }
        metadata["source_proposal"] = proposal["id"]
        _atomic_write(sidecar, _sidecar(metadata))
        _append_ledger(
            target,
            {
                "action": "patch",
                "timestamp": timestamp,
                "version": current_version + 1,
                "previous_version": current_version,
                "risk_level": risk_level,
                "reason": proposal["reason"],
                "evidence": evidence,
                "proposal": proposal["id"],
                "approved_by": proposal.get("approved_by"),
                "provenance": metadata["provenance"],
            },
        )
        return target
    raise HarnessError(f"Unsupported promotion action: {action}")


def list_skills(cwd: Path, scope: str | None = None) -> list[dict[str, str]]:
    scopes = [scope] if scope else ["project", "personal"]
    result: list[dict[str, str]] = []
    for item_scope in scopes:
        root, _ = skill_roots(item_scope, cwd)
        if not root.is_dir():
            continue
        for directory in sorted(root.iterdir()):
            if directory.is_symlink() or not directory.is_dir():
                continue
            skill_file = directory / "SKILL.md"
            if skill_file.is_symlink() or not skill_file.is_file():
                continue
            try:
                metadata = _managed(directory)
            except HarnessError:
                continue
            if metadata.get("name") != directory.name or metadata.get("scope") != item_scope:
                continue
            result.append(
                {
                    "name": directory.name,
                    "scope": item_scope,
                    "status": str(metadata.get("status", "unknown")),
                    "path": str(skill_file),
                }
            )
    return result


def skill_index(cwd: Path, scope: str | None = None) -> list[dict[str, str]]:
    """List every loadable Harness-managed skill with its one-line description."""
    entries: list[dict[str, str]] = []
    for skill in list_skills(cwd, scope):
        try:
            text = Path(skill["path"]).read_text(encoding="utf-8")
        except OSError:
            continue
        description = ""
        frontmatter = _FRONTMATTER.match(text)
        if frontmatter:
            found = _DESCRIPTION.search(frontmatter.group(1))
            if found:
                description = found.group(1).strip().strip("\"'")
        entries.append({**skill, "description": " ".join(description.split())})
    return entries


def managed_skill_context(cwd: Path) -> list[dict[str, str]]:
    context: list[dict[str, str]] = []
    for skill in list_skills(cwd):
        skill_file = Path(skill["path"])
        try:
            body = skill_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise HarnessError(f"Could not read managed skill {skill_file}: {exc}") from exc
        context.append(
            {
                "name": skill["name"],
                "scope": skill["scope"],
                "content": body[:4000],
            }
        )
    return context


def inspect_skill(name: str, scope: str, cwd: Path) -> tuple[Path, dict[str, Any], str]:
    if not _NAME.fullmatch(name):
        raise HarnessError("Skill name must be a lowercase slug.")
    root, _ = skill_roots(scope, cwd)
    directory = root / name
    metadata = _managed(directory)
    _assert_identity(metadata, name, scope)
    skill_file = directory / "SKILL.md"
    if skill_file.is_symlink():
        raise HarnessError(f"Refusing to inspect a symlinked skill file: {skill_file}")
    try:
        body = skill_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise HarnessError(f"Could not read skill file: {exc}") from exc
    return directory, metadata, body


def move_skill(
    name: str,
    scope: str,
    cwd: Path,
    *,
    archive: bool,
    reason: str = "manual lifecycle operation",
    merged_into: str | None = None,
) -> Path:
    if not _NAME.fullmatch(name):
        raise HarnessError("Skill name must be a lowercase slug.")
    root, archive_root = skill_roots(scope, cwd)
    source = root / name if archive else archive_root / name
    target = archive_root / name if archive else root / name
    metadata = _managed(source)
    _assert_identity(metadata, name, scope)
    if target.exists() or target.is_symlink():
        raise HarnessError(f"Refusing to overwrite existing path: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    state = "archived" if archive else "probation"
    metadata["trust_state"] = state
    metadata["status"] = state
    metadata["updated_at"] = datetime.now(UTC).isoformat()
    if merged_into:
        metadata["merged_into"] = merged_into
    elif not archive:
        metadata.pop("merged_into", None)
    _atomic_write(source / ".sidecar.json", _sidecar(metadata))
    _append_ledger(
        source,
        {
            "action": "archive" if archive else "restore",
            "timestamp": metadata["updated_at"],
            "version": metadata.get("version", 1),
            "reason": reason,
            "merged_into": merged_into,
            "performed_by": getpass.getuser(),
        },
    )
    try:
        source.rename(target)
    except OSError as exc:
        raise HarnessError(f"Could not move skill to {target}: {exc}") from exc
    return target


def merge_skills(proposal: dict[str, Any], cwd: Path) -> Path:
    """Fold absorbed skills into the kept skill, archiving (never deleting) the absorbed ones."""
    scope = proposal["scope"]
    keep = proposal["name"]
    absorbed = list(proposal.get("absorbs") or [])
    if not absorbed or keep in absorbed or len(set(absorbed)) != len(absorbed):
        raise HarnessError("A merge needs distinct absorbed skills that exclude the kept skill.")
    _, archive_root = skill_roots(scope, cwd)
    for name in [keep, *absorbed]:
        _, metadata, _ = inspect_skill(name, scope, cwd)
        if metadata.get("trust_state", metadata.get("status")) == "trusted" and not proposal.get("approved_by"):
            raise HarnessError(f"Merging trusted skill {name} requires human approval.")
    for name in absorbed:
        if (archive_root / name).exists() or (archive_root / name).is_symlink():
            raise HarnessError(f"Cannot absorb {name}: an archived skill with that name already exists.")
    path = promote({**proposal, "action": "patch"}, cwd)
    _append_ledger(
        path,
        {
            "action": "merge",
            "timestamp": datetime.now(UTC).isoformat(),
            "absorbed": absorbed,
            "proposal": proposal["id"],
            "reason": proposal["reason"],
            "performed_by": getpass.getuser(),
        },
    )
    for name in absorbed:
        move_skill(
            name,
            scope,
            cwd,
            archive=True,
            reason=f"merged into {keep}: {proposal['reason']}",
            merged_into=keep,
        )
    return path


def trust_skill(name: str, scope: str, cwd: Path) -> Path:
    directory, metadata, _ = inspect_skill(name, scope, cwd)
    if metadata.get("trust_state", metadata.get("status")) != "probation":
        raise HarnessError(f"Only probation skills can be trusted: {name}")
    timestamp = datetime.now(UTC).isoformat()
    metadata["trust_state"] = "trusted"
    metadata["status"] = "trusted"
    metadata["updated_at"] = timestamp
    _atomic_write(directory / ".sidecar.json", _sidecar(metadata))
    _append_ledger(
        directory,
        {
            "action": "trust",
            "timestamp": timestamp,
            "version": metadata.get("version", 1),
            "performed_by": getpass.getuser(),
            "reason": "manual trust promotion",
        },
    )
    return directory


CRITICALITY_LEVELS = ("low", "medium", "high", "critical")


def _parse_expiry(value: Any) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise HarnessError(f"Invalid expiry date {value!r}; use YYYY-MM-DD.") from exc


def _governance_problem(governance: Any) -> str | None:
    """Return why governance metadata blocks sharing, or None when it is acceptable."""
    if not isinstance(governance, dict):
        return "no governance metadata"
    if not str(governance.get("owner", "")).strip():
        return "no owner"
    if governance.get("criticality") not in CRITICALITY_LEVELS:
        return "no valid criticality"
    try:
        expires = _parse_expiry(governance.get("expires"))
    except HarnessError:
        return "no valid expiry date"
    if expires < datetime.now(UTC).date():
        return f"review expired on {expires.isoformat()}"
    return None


def set_governance(
    name: str, scope: str, cwd: Path, owner: str, criticality: str, expires: str
) -> Path:
    directory, metadata, _ = inspect_skill(name, scope, cwd)
    owner = " ".join(owner.split())
    if not owner or len(owner) > 100:
        raise HarnessError("Owner must be 1-100 characters.")
    if redact_text(owner) != owner:
        raise HarnessError("Owner looks like it contains a secret.")
    if criticality not in CRITICALITY_LEVELS:
        raise HarnessError(f"Criticality must be one of: {', '.join(CRITICALITY_LEVELS)}.")
    if _parse_expiry(expires) < datetime.now(UTC).date():
        raise HarnessError("Expiry date must not be in the past.")
    timestamp = datetime.now(UTC).isoformat()
    metadata["governance"] = {
        "owner": owner,
        "criticality": criticality,
        "expires": expires,
        "set_at": timestamp,
        "set_by": getpass.getuser(),
    }
    metadata["updated_at"] = timestamp
    _atomic_write(directory / ".sidecar.json", _sidecar(metadata))
    _append_ledger(
        directory,
        {
            "action": "govern",
            "timestamp": timestamp,
            "version": metadata.get("version", 1),
            "performed_by": getpass.getuser(),
            "reason": f"owner={owner}, criticality={criticality}, expires={expires}",
        },
    )
    return directory


def read_version(name: str, scope: str, cwd: Path, version: int) -> str:
    """Return a saved skill body after verifying its SHA-256 digest."""
    version_dir = _version_root(scope, cwd, name) / f"{version:06d}"
    version_file = version_dir / "SKILL.md"
    version_meta = version_dir / "metadata.json"
    if version_dir.is_symlink() or version_file.is_symlink() or version_meta.is_symlink():
        raise HarnessError("Refusing to read a symlinked version.")
    try:
        body = version_file.read_text(encoding="utf-8")
        prior = json.loads(version_meta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(f"Skill version not found or unreadable: {version}") from exc
    if not isinstance(prior, dict) or prior.get("sha256") != hashlib.sha256(body.encode("utf-8")).hexdigest():
        raise HarnessError(f"Version {version} failed its integrity check.")
    return body


def _body_description(name: str, body: str) -> str:
    frontmatter = _FRONTMATTER.match(body)
    description = _DESCRIPTION.search(frontmatter.group(1)) if frontmatter else None
    if not description:
        raise HarnessError(f"Skill description is missing from {name}.")
    return description.group(1).strip().strip("\"'")


def _review_proposal(name: str, scope: str, body: str, reason: str, cwd: Path) -> dict[str, Any]:
    body = validate_skill(name, _body_description(name, body), body)
    return {
        "id": str(uuid.uuid4()),
        "action": "create",
        "scope": scope,
        "name": name,
        "description": _body_description(name, body),
        "reason": reason,
        "confidence": 1.0,
        "risk_level": classify_risk(name, _body_description(name, body), reason, body),
        "skill_md": body,
        "evidence": [],
        "source_session_ids": [],
        "absorbs": [],
        "cwd": str(cwd),
    }


def share_proposal(name: str, source_scope: str, target_scope: str, cwd: Path) -> dict[str, Any]:
    """Build a pending proposal copying a trusted skill into another scope for human review."""
    if source_scope == target_scope:
        raise HarnessError("Source and target scope must differ.")
    _, metadata, body = inspect_skill(name, source_scope, cwd)
    if metadata.get("trust_state", metadata.get("status")) != "trusted":
        raise HarnessError(f"Only trusted skills can be shared across scopes: {name}")
    root, _ = skill_roots(target_scope, cwd)
    if (root / name).exists():
        raise HarnessError(f"A {target_scope} skill named {name} already exists.")
    return _review_proposal(
        name, target_scope, body, f"Shared from trusted {source_scope} skill (version {metadata.get('version', 1)}).", cwd
    )


def import_proposal(directory: Path, scope: str, cwd: Path) -> dict[str, Any]:
    """Verify a skill export and build a pending proposal; importing never installs directly."""
    digest = verify_export(directory)
    body = (directory / "SKILL.md").read_text(encoding="utf-8")
    name = directory.name
    root, _ = skill_roots(scope, cwd)
    if (root / name).exists():
        raise HarnessError(f"A {scope} skill named {name} already exists.")
    return _review_proposal(
        name, scope, body, f"Imported from a verified skill export (sha256 {digest[:12]}).", cwd
    )


def export_skill(name: str, scope: str, cwd: Path, destination: Path) -> Path:
    _, metadata, body = inspect_skill(name, scope, cwd)
    if metadata.get("trust_state", metadata.get("status")) != "trusted":
        raise HarnessError(f"Only trusted skills can be exported: {name}")
    governance = metadata.get("governance")
    needs_governance = metadata.get("risk_level") == "sensitive" or (
        isinstance(governance, dict) and governance.get("criticality") in {"high", "critical"}
    )
    if needs_governance or governance is not None:
        problem = _governance_problem(governance)
        if problem:
            raise HarnessError(
                f"Cannot export {name}: {problem}. Run `harness govern {name} --owner ... "
                "--criticality ... --expires YYYY-MM-DD`."
            )
    frontmatter = _FRONTMATTER.match(body)
    description = _DESCRIPTION.search(frontmatter.group(1)) if frontmatter else None
    if not description:
        raise HarnessError(f"Skill description is missing from {name}.")
    body = validate_skill(name, description.group(1).strip().strip("\"'"), body)

    destination = destination.expanduser().absolute()
    if destination.is_symlink():
        raise HarnessError(f"Refusing to export into a symlinked directory: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / name
    if target.exists() or target.is_symlink():
        raise HarnessError(f"Export target already exists: {target}")

    provenance = metadata.get("provenance")
    source_sessions = (
        provenance.get("source_session_ids", []) if isinstance(provenance, dict) else []
    )
    if not isinstance(source_sessions, list):
        source_sessions = []
    version = metadata.get("version", 1)
    if not isinstance(version, int) or version < 1:
        raise HarnessError(f"Invalid version metadata for {name}.")
    manifest = {
        "format_version": 1,
        "skill_name": name,
        "skill_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "skill_version": version,
        "exported_at": datetime.now(UTC).isoformat(),
        "harness_version": __version__,
        "source_session_count": len({value for value in source_sessions if isinstance(value, str)}),
    }
    if isinstance(governance, dict):
        manifest["governance"] = {
            key: governance[key] for key in ("owner", "criticality", "expires") if key in governance
        }
    staging = Path(tempfile.mkdtemp(prefix=f".{name}.export-", dir=destination))
    target_created = False
    try:
        (staging / "SKILL.md").write_text(body, encoding="utf-8", newline="\n")
        (staging / "provenance.json").write_text(
            _sidecar(manifest), encoding="utf-8", newline="\n"
        )
        target.mkdir()
        target_created = True
        os.replace(staging / "SKILL.md", target / "SKILL.md")
        os.replace(staging / "provenance.json", target / "provenance.json")
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        if target_created and target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        raise
    staging.rmdir()
    return target


def verify_export(directory: Path) -> str:
    if directory.is_symlink():
        raise HarnessError(f"Refusing to verify a symlinked export directory: {directory}")
    manifest_path = directory / "provenance.json"
    skill_path = directory / "SKILL.md"
    if manifest_path.is_symlink() or skill_path.is_symlink():
        raise HarnessError(f"Refusing to verify symlinked export files in {directory}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        body = skill_path.read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(f"Could not read skill export {directory}: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("format_version") != 1:
        raise HarnessError(f"Unsupported or invalid export manifest in {manifest_path}")
    name = manifest.get("skill_name")
    if not isinstance(name, str) or name != directory.name:
        raise HarnessError(f"Export manifest skill name does not match {directory.name}.")
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if manifest.get("skill_sha256") != digest:
        raise HarnessError(f"Skill export integrity check failed for {name}.")
    frontmatter = _FRONTMATTER.match(body)
    description = _DESCRIPTION.search(frontmatter.group(1)) if frontmatter else None
    if not description:
        raise HarnessError(f"Skill description is missing from {skill_path}.")
    validate_skill(name, description.group(1).strip().strip("\"'"), body)
    governance = manifest.get("governance")
    if governance is not None:
        problem = _governance_problem(governance)
        if problem:
            raise HarnessError(f"Skill export {name} is not currently approved for sharing: {problem}.")
    return digest


def quarantine_skill(name: str, scope: str, cwd: Path, reason: str) -> Path:
    directory, metadata, _ = inspect_skill(name, scope, cwd)
    _, archive = skill_roots(scope, cwd)
    target = archive.parent / "quarantine" / name
    base = cwd.resolve() if scope == "project" else Path.home().resolve()
    _assert_no_symlink_components(base, target.parent)
    if target.exists() or target.is_symlink():
        raise HarnessError(f"Quarantined skill path already exists: {target}")
    metadata["trust_state"] = "quarantined"
    metadata["status"] = "quarantined"
    metadata["updated_at"] = datetime.now(UTC).isoformat()
    _atomic_write(directory / ".sidecar.json", _sidecar(metadata))
    _append_ledger(
        directory,
        {
            "action": "quarantine",
            "timestamp": metadata["updated_at"],
            "version": metadata.get("version", 1),
            "reason": reason,
            "performed_by": getpass.getuser(),
        },
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        directory.rename(target)
    except OSError as exc:
        raise HarnessError(f"Could not quarantine skill {name}: {exc}") from exc
    return target


def restore_quarantined_skill(name: str, scope: str, cwd: Path) -> Path:
    if not _NAME.fullmatch(name):
        raise HarnessError("Skill name must be a lowercase slug.")
    root, archive = skill_roots(scope, cwd)
    source = archive.parent / "quarantine" / name
    target = root / name
    base = cwd.resolve() if scope == "project" else Path.home().resolve()
    _assert_no_symlink_components(base, source.parent)
    metadata = _managed(source)
    _assert_identity(metadata, name, scope)
    if metadata.get("trust_state", metadata.get("status")) != "quarantined":
        raise HarnessError(f"Skill is not quarantined: {name}")
    if target.exists() or target.is_symlink():
        raise HarnessError(f"Refusing to overwrite existing path: {target}")
    metadata["trust_state"] = "probation"
    metadata["status"] = "probation"
    metadata["updated_at"] = datetime.now(UTC).isoformat()
    _atomic_write(source / ".sidecar.json", _sidecar(metadata))
    _append_ledger(
        source,
        {
            "action": "unquarantine",
            "timestamp": metadata["updated_at"],
            "version": metadata.get("version", 1),
            "performed_by": getpass.getuser(),
            "reason": "manual quarantine restoration",
        },
    )
    root.mkdir(parents=True, exist_ok=True)
    try:
        source.rename(target)
    except OSError as exc:
        raise HarnessError(f"Could not restore quarantined skill {name}: {exc}") from exc
    return target


def rollback_skill(
    name: str,
    scope: str,
    cwd: Path,
    version: int | None = None,
) -> Path:
    directory, metadata, current_body = inspect_skill(name, scope, cwd)
    history = _version_root(scope, cwd, name)
    current_version = metadata.get("version", 1)
    if not isinstance(current_version, int) or current_version < 1:
        raise HarnessError(f"Invalid version metadata for {name}.")
    target_version = version if version is not None else current_version - 1
    if target_version < 1 or target_version >= current_version:
        raise HarnessError("Rollback version must be an existing version older than the current one.")
    version_dir = history / f"{target_version:06d}"
    if version_dir.is_symlink() or not version_dir.is_dir():
        raise HarnessError(f"Skill version not found: {target_version}")
    version_file = version_dir / "SKILL.md"
    version_meta = version_dir / "metadata.json"
    if version_file.is_symlink() or version_meta.is_symlink():
        raise HarnessError("Refusing to restore a symlinked version.")
    try:
        body = version_file.read_text(encoding="utf-8")
        prior = json.loads(version_meta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(f"Could not read version {target_version}: {exc}") from exc
    if not isinstance(prior, dict):
        raise HarnessError(f"Version {target_version} metadata is invalid.")
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if prior.get("sha256") != digest:
        raise HarnessError(f"Version {target_version} failed its integrity check.")
    description_match = _DESCRIPTION.search(_FRONTMATTER.match(body).group(1)) if _FRONTMATTER.match(body) else None
    if not description_match:
        raise HarnessError(f"Version {target_version} has invalid skill frontmatter.")
    validate_skill(name, description_match.group(1).strip().strip("\"'"), body)
    timestamp = datetime.now(UTC).isoformat()
    _save_version(
        scope,
        cwd,
        name,
        current_version,
        current_body,
        metadata,
        f"rollback to version {target_version}",
    )
    _atomic_write(directory / "SKILL.md", body)
    metadata["version"] = current_version + 1
    metadata["trust_state"] = "probation"
    metadata["status"] = "probation"
    metadata["risk_level"] = prior.get("risk_level", "low")
    metadata["updated_at"] = timestamp
    metadata["rollback"] = {"restored_version": target_version, "at": timestamp}
    if metadata["risk_level"] == "sensitive":
        metadata["approval"] = {
            "required": True,
            "approved_by": getpass.getuser(),
            "approved_at": timestamp,
            "reason": "explicit rollback to a previously recorded version",
        }
    _atomic_write(directory / ".sidecar.json", _sidecar(metadata))
    _append_ledger(
        directory,
        {
            "action": "rollback",
            "timestamp": timestamp,
            "version": current_version + 1,
            "restored_version": target_version,
            "reason": f"rollback to version {target_version}",
            "performed_by": getpass.getuser(),
        },
    )
    return directory
