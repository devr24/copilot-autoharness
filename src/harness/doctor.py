import json
import os
import shutil
from pathlib import Path

from .config import Config


def _copilot_home() -> Path:
    override = os.environ.get("COPILOT_HOME")
    return Path(override) if override else Path.home() / ".copilot"


def _plugin_installed() -> bool:
    try:
        text = (_copilot_home() / "config.json").read_text(encoding="utf-8")
    except OSError:
        return False
    # config.json starts with // comment lines
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
    try:
        plugins = json.loads(body).get("installedPlugins", [])
    except (ValueError, AttributeError):
        return False
    return any(isinstance(p, dict) and p.get("name") == "copilot-harness" and p.get("enabled", True) for p in plugins)


def _repo_hooks_installed(cwd: Path) -> bool:
    directory = cwd / ".github" / "hooks"
    if not directory.is_dir():
        return False
    for path in directory.glob("*.json"):
        try:
            if '"harness"' in path.read_text(encoding="utf-8"):
                return True
        except OSError:
            continue
    return False


def run_checks(config: Config, cwd: Path) -> list[tuple[str, bool, str]]:
    """Return (name, ok, detail) tuples for each installation check."""
    checks: list[tuple[str, bool, str]] = []

    harness = shutil.which("harness")
    # Copilot spawns hook commands without a shell, so Windows shims (.cmd/.bat) do not work.
    if harness and os.name == "nt" and not harness.lower().endswith(".exe"):
        checks.append(("harness executable", False, f"{harness} is not an .exe; hooks cannot spawn it"))
    elif harness:
        checks.append(("harness executable", True, harness))
    else:
        checks.append(("harness executable", False, "not on PATH; run `pip install` for this project"))

    copilot = shutil.which("copilot")
    checks.append(("copilot CLI", bool(copilot), copilot or "not on PATH"))

    plugin = _plugin_installed()
    repo = _repo_hooks_installed(cwd)
    if plugin and repo:
        detail = "plugin AND repo hooks installed; hooks may run twice"
        checks.append(("hooks", False, detail))
    elif plugin or repo:
        checks.append(("hooks", True, "plugin" if plugin else "repo hook manifest"))
    else:
        checks.append(("hooks", False, "install the plugin or copy hooks/hooks.json to .github/hooks/"))

    if config.config_file.exists():
        checks.append(("configuration", True, f"{config.config_file} (mode: {config.mode})"))
    else:
        checks.append(("configuration", False, "run `harness config init` to opt in to auto mode"))
    return checks
