"""Organisation policy (deny list, owner allowlist, required governance) and SSH-key signatures for exports."""
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import HarnessError

NAMESPACE = "harness-skill"
SIGNATURE_NAME = "provenance.json.sig"
POLICY_NAMES = ("harness-policy.json", ".github/harness-policy.json")
_LEVELS = ("low", "medium", "high", "critical")


@dataclass(frozen=True)
class Policy:
    path: Path
    deny_names: frozenset[str] = frozenset()
    deny_sha256: frozenset[str] = frozenset()
    allowed_owners: frozenset[str] = frozenset()
    max_criticality: str | None = None
    require_governance: bool = False
    require_signature: bool = False
    allowed_signers: Path | None = None
    trusted_repo: str | None = None
    trusted_workflow: str | None = None


def _strings(raw: dict, key: str) -> frozenset[str]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise HarnessError(f"Policy field {key} must be a list of strings.")
    return frozenset(value)


def find_policy(*starts: Path) -> Path | None:
    for start in starts:
        for name in POLICY_NAMES:
            candidate = start / name
            if candidate.is_file():
                return candidate
    return None


def load_policy(path: Path) -> Policy:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise HarnessError(f"Could not read policy {path}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("format_version") != 1:
        raise HarnessError(f"Unsupported or invalid policy file: {path}")
    maximum = raw.get("max_criticality")
    if maximum is not None and maximum not in _LEVELS:
        raise HarnessError(f"Policy max_criticality must be one of {', '.join(_LEVELS)}.")
    signers = raw.get("allowed_signers")
    signers_path = None
    if signers is not None:
        if not isinstance(signers, str):
            raise HarnessError("Policy allowed_signers must be a file path string.")
        signers_path = (path.parent / signers).resolve()
    if raw.get("require_signature") and signers_path is None:
        raise HarnessError("Policy require_signature needs allowed_signers.")
    for key in ("trusted_repo", "trusted_workflow"):
        if raw.get(key) is not None and not isinstance(raw[key], str):
            raise HarnessError(f"Policy {key} must be a string.")
    return Policy(
        path=path,
        trusted_repo=raw.get("trusted_repo"),
        trusted_workflow=raw.get("trusted_workflow"),
        deny_names=_strings(raw, "deny_names"),
        deny_sha256=frozenset(item.lower() for item in _strings(raw, "deny_sha256")),
        allowed_owners=_strings(raw, "allowed_owners"),
        max_criticality=maximum,
        require_governance=bool(raw.get("require_governance", False)),
        require_signature=bool(raw.get("require_signature", False)),
        allowed_signers=signers_path,
    )


def _ssh_keygen() -> str:
    found = shutil.which("ssh-keygen")
    if not found:
        raise HarnessError("ssh-keygen is required for signatures but was not found on PATH.")
    return found


def sign_export(directory: Path, key: Path) -> Path:
    manifest = directory / "provenance.json"
    if directory.is_symlink() or manifest.is_symlink() or not manifest.is_file():
        raise HarnessError(f"Not a valid skill export: {directory}")
    signature = directory / SIGNATURE_NAME
    if signature.exists():
        raise HarnessError(f"Already signed: {signature}")
    result = subprocess.run(
        [_ssh_keygen(), "-Y", "sign", "-f", str(key), "-n", NAMESPACE, str(manifest)],
        capture_output=True, text=True, check=False,
    )
    produced = directory / "provenance.json.sig"
    if result.returncode != 0 or not produced.is_file():
        raise HarnessError(f"Signing failed: {(result.stderr or result.stdout).strip()}")
    return produced


def verify_signature(directory: Path, allowed_signers: Path) -> str:
    """Return the allowed-signers identity that signed provenance.json, or raise."""
    manifest = directory / "provenance.json"
    signature = directory / SIGNATURE_NAME
    if not signature.is_file() or signature.is_symlink():
        raise HarnessError("missing signature")
    if not allowed_signers.is_file():
        raise HarnessError(f"allowed_signers file not found: {allowed_signers}")
    data = manifest.read_bytes()
    tool = _ssh_keygen()
    found = subprocess.run(
        [tool, "-Y", "find-principals", "-f", str(allowed_signers), "-s", str(signature)],
        capture_output=True, text=True, check=False,
    )
    principals = [line.strip() for line in found.stdout.splitlines() if line.strip()]
    for principal in principals:
        checked = subprocess.run(
            [tool, "-Y", "verify", "-f", str(allowed_signers), "-I", principal,
             "-n", NAMESPACE, "-s", str(signature)],
            input=data, capture_output=True, check=False,
        )
        if checked.returncode == 0:
            return principal
    raise HarnessError("signature is not from an allowed signer")


def check_policy(directory: Path, policy: Policy) -> list[str]:
    """Return policy violations for one verified export directory (empty means allowed)."""
    manifest = json.loads((directory / "provenance.json").read_text(encoding="utf-8"))
    name = manifest.get("skill_name", directory.name)
    problems = []
    if name in policy.deny_names:
        problems.append(f"skill name {name} is on the deny list")
    if str(manifest.get("skill_sha256", "")).lower() in policy.deny_sha256:
        problems.append("skill content hash is on the deny list")
    governance = manifest.get("governance")
    if policy.require_governance and not isinstance(governance, dict):
        problems.append("policy requires governance metadata")
    if isinstance(governance, dict):
        if policy.allowed_owners and governance.get("owner") not in policy.allowed_owners:
            problems.append(f"owner {governance.get('owner')!r} is not in allowed_owners")
        level = governance.get("criticality")
        if policy.max_criticality and level in _LEVELS and _LEVELS.index(level) > _LEVELS.index(policy.max_criticality):
            problems.append(f"criticality {level} exceeds policy maximum {policy.max_criticality}")
    if policy.require_signature or (policy.allowed_signers and (directory / SIGNATURE_NAME).exists()):
        try:
            verify_signature(directory, policy.allowed_signers)
        except HarnessError as exc:
            problems.append(str(exc))
    return problems


def enforce_policy(directory: Path, policy: Policy) -> None:
    problems = check_policy(directory, policy)
    if problems:
        raise HarnessError(f"Policy {policy.path.name} blocks {directory.name}: " + "; ".join(problems))
