"""Reproducible skill bundles and verification of GitHub build-provenance attestations."""
import gzip
import io
import shutil
import subprocess
import tarfile
from pathlib import Path

from .errors import HarnessError


def build_bundle(skills_dir: Path, output: Path) -> Path:
    """Write a deterministic .tar.gz of every skill folder so the same content gives the same hash."""
    if not skills_dir.is_dir() or skills_dir.is_symlink():
        raise HarnessError(f"Not a skills directory: {skills_dir}")
    if output.exists():
        raise HarnessError(f"Bundle already exists: {output}")
    files = sorted(
        (p for p in skills_dir.rglob("*") if p.is_file() and not any(part.startswith(".") for part in p.relative_to(skills_dir).parts)),
        key=lambda p: p.relative_to(skills_dir).as_posix(),
    )
    if not files:
        raise HarnessError(f"No skills found in {skills_dir}")
    for path in files:
        if path.is_symlink():
            raise HarnessError(f"Refusing to bundle symlink: {path}")
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in files:
            data = path.read_bytes()
            info = tarfile.TarInfo(path.relative_to(skills_dir).as_posix())
            info.size, info.mtime, info.mode = len(data), 0, 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(data))
    with output.open("wb") as handle, gzip.GzipFile(fileobj=handle, mode="wb", mtime=0, filename="") as zipped:
        zipped.write(raw.getvalue())
    return output


def verify_attestation(bundle: Path, repo: str, signer_workflow: str | None = None) -> str:
    """Check with `gh attestation verify` that `bundle` was built by `repo` (and optionally one workflow)."""
    if not bundle.is_file() or bundle.is_symlink():
        raise HarnessError(f"Bundle not found: {bundle}")
    if "/" not in repo or repo.count("/") != 1:
        raise HarnessError("Repository must look like owner/name.")
    gh = shutil.which("gh")
    if not gh:
        raise HarnessError("The GitHub CLI (gh) is required to verify attestations but was not found on PATH.")
    command = [gh, "attestation", "verify", str(bundle), "--repo", repo]
    if signer_workflow:
        command += ["--signer-workflow", signer_workflow]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise HarnessError(f"Attestation verification failed for {bundle.name}: {detail[-1] if detail else 'no output'}")
    return f"{repo}" + (f" via {signer_workflow}" if signer_workflow else "")
