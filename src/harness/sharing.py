"""Scaffold for a shared-skills repository whose pull requests are gated by `harness verify`."""
from pathlib import Path

from .errors import HarnessError

HARNESS_SOURCE = "git+https://github.com/devr24/copilot-autoharness.git"

_WORKFLOW = f"""name: Verify skills

on:
  pull_request:
    paths: ["skills/**"]
  push:
    branches: [main]
    paths: ["skills/**"]

jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
      - name: Install harness
        run: python -m pip install "{HARNESS_SOURCE}"
      - name: Audit skills (hash, expiry, secrets, injection, duplicates)
        run: harness audit skills
"""

_CODEOWNERS = """# Replace with the team that reviews shared skills. Enable "Require review from
# Code Owners" in branch protection so no skill merges without them.
/skills/ @your-org/skill-reviewers
"""

_PR_TEMPLATE = """## Skill change

- Skill name(s):
- Exported with `harness export` from a **trusted** skill: [ ]
- `harness verify` passes locally: [ ]
- Owner, criticality and expiry were set with `harness govern`: [ ]

## Reviewer checklist

- [ ] The guidance is correct, current and not specific to one person's machine
- [ ] No secrets, internal hostnames or personal data in `SKILL.md`
- [ ] Anything touching production, IAM, deployment, secrets or destructive actions has been reviewed by a human with authority over it
- [ ] The skill does not contradict an existing shared skill
- [ ] A shadow run (`harness shadow`) showed no regression, if cases exist
"""

_README = """# Shared skills

Reviewed, hash-verified skills exported from Copilot AutoHarness.

## Contribute

```text
harness govern <skill> --owner <team> --criticality medium --expires 2027-01-01
harness export <skill> --scope project --to <path-to-this-repo>/skills
```

Open a pull request. CI runs `harness verify` on every skill and a code owner must approve.

## Consume

```text
git pull
harness import skills/<skill> --scope project
harness proposals
harness accept <proposal-id>
```

Importing re-verifies the hash and creates a pending proposal; nothing is installed until you accept it.
Accepted skills start in probation like any other.

## Limits

`provenance.json` is an unsigned hash manifest. It detects accidental or unreviewed edits to `SKILL.md`,
not a malicious change that also rewrites the hash, so the pull-request review is the real control.
Branch protection and code owners must be enabled on this repository.
"""

FILES = {
    "README.md": _README,
    "skills/.gitkeep": "",
    ".github/workflows/verify-skills.yml": _WORKFLOW,
    ".github/CODEOWNERS": _CODEOWNERS,
    ".github/PULL_REQUEST_TEMPLATE.md": _PR_TEMPLATE,
}


def scaffold_shared_repo(destination: Path) -> list[Path]:
    destination = destination.expanduser().absolute()
    if destination.is_symlink():
        raise HarnessError(f"Refusing to write into a symlinked directory: {destination}")
    clashes = [destination / name for name in FILES if (destination / name).exists()]
    if clashes:
        raise HarnessError(f"Refusing to overwrite existing file: {clashes[0]}")
    written = []
    for name, content in FILES.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        written.append(target)
    return written
