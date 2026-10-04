import os
import shlex
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import HarnessError


@dataclass(frozen=True)
class Config:
    home: Path
    mode: str = "off"
    tool_calls_before_learn: int = 40
    reflect_during_session: bool = True
    reflect_command: tuple[str, ...] = ()
    reflect_timeout: int = 180
    project_skill_capacity: int = 50
    personal_skill_capacity: int = 20
    inject_index: bool = True
    index_desc_chars: int = 100
    index_max_lines: int = 30
    consolidate_enabled: bool = True
    consolidate_every: int = 250
    consolidate_min_skills: int = 3
    debug: bool = False

    @property
    def database(self) -> Path:
        return self.home / "state.sqlite3"

    @property
    def config_file(self) -> Path:
        return self.home / "config.toml"


def harness_home() -> Path:
    override = os.environ.get("COPILOT_HARNESS_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "copilot-harness"
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "copilot-harness"


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    result = data.get(name, {})
    if not isinstance(result, dict):
        raise HarnessError(f"Configuration section [{name}] must be a table.")
    return result


def load_config(home: Path | None = None) -> Config:
    root = home or harness_home()
    file = root / "config.toml"
    if not file.exists():
        return Config(home=root)
    try:
        with file.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise HarnessError(f"Could not read configuration {file}: {exc}") from exc

    reflection = _table(data, "reflection")
    skills = _table(data, "skills")
    project = _table(skills, "project")
    personal = _table(skills, "personal")
    mode = data.get("mode", "auto")
    command = reflection.get("command", [])
    if isinstance(command, str):
        command = shlex.split(command, posix=sys.platform != "win32")
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        raise HarnessError("[reflection].command must be a string or an array of strings.")
    values = {
        "tool_calls_before_learn": reflection.get("tool_calls", 40),
        "reflect_timeout": reflection.get("timeout_seconds", 180),
        "project_skill_capacity": project.get("capacity", 50),
        "personal_skill_capacity": personal.get("capacity", 20),
    }
    for key, value in values.items():
        if not isinstance(value, int) or value < 1:
            raise HarnessError(f"Configuration value {key} must be a positive integer.")
    index = _table(data, "index")
    consolidation = _table(data, "consolidation")
    extras = {
        "index_desc_chars": index.get("description_chars", 100),
        "index_max_lines": index.get("max_skills", 30),
        "consolidate_every": consolidation.get("every_tool_calls", 250),
        "consolidate_min_skills": consolidation.get("min_skills", 3),
    }
    for key, value in extras.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise HarnessError(f"Configuration value {key} must be a positive integer.")
    inject_index = index.get("session_start", True)
    consolidate_enabled = consolidation.get("enabled", True)
    if not isinstance(inject_index, bool) or not isinstance(consolidate_enabled, bool):
        raise HarnessError("[index].session_start and [consolidation].enabled must be true or false.")
    debug = _table(data, "logging").get("debug", False)
    if not isinstance(debug, bool):
        raise HarnessError("[logging].debug must be true or false.")
    during_session = reflection.get("during_session", True)
    if not isinstance(during_session, bool):
        raise HarnessError("[reflection].during_session must be true or false.")
    if mode not in {"off", "review", "auto"}:
        raise HarnessError("mode must be one of: off, review, auto.")
    return Config(
        home=root,
        mode=mode,
        tool_calls_before_learn=values["tool_calls_before_learn"],
        reflect_during_session=during_session,
        reflect_command=tuple(command),
        reflect_timeout=values["reflect_timeout"],
        project_skill_capacity=values["project_skill_capacity"],
        personal_skill_capacity=values["personal_skill_capacity"],
        inject_index=inject_index,
        index_desc_chars=extras["index_desc_chars"],
        index_max_lines=extras["index_max_lines"],
        consolidate_enabled=consolidate_enabled,
        consolidate_every=extras["consolidate_every"],
        consolidate_min_skills=extras["consolidate_min_skills"],
        debug=debug,
    )


def default_config_text() -> str:
    if sys.platform == "win32":
        command = (
            'command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", '
            '"$utf8 = New-Object System.Text.UTF8Encoding $false; [Console]::InputEncoding = $utf8; '
            '$OutputEncoding = $utf8; [Console]::OutputEncoding = $utf8; '
            '$input | copilot --silent --no-color --no-ask-user --disable-builtin-mcps --available-tools=harness_no_tools --excluded-tools=bash,powershell,view,create,edit,glob,grep,web_fetch,task"]'
        )
    else:
        command = (
            'command = ["sh", "-c", '
            '"copilot --silent --no-color --no-ask-user --disable-builtin-mcps --available-tools=harness_no_tools --excluded-tools=bash,powershell,view,create,edit,glob,grep,web_fetch,task"]'
        )
    return f'''# Session evidence is sent to GitHub Copilot for reflection after the threshold is met.
# The reflector runs with every built-in tool excluded and built-in MCP servers disabled.
mode = "auto"

[reflection]
tool_calls = 40
during_session = true
timeout_seconds = 180
{command}

[skills.project]
capacity = 50

[skills.personal]
capacity = 20

[index]
session_start = true
description_chars = 100
max_skills = 30

[consolidation]
enabled = true
every_tool_calls = 250
min_skills = 3

# Set debug = true (or env COPILOT_HARNESS_DEBUG=1) to write hook and reflection traces to debug.log.
# Logs contain event names, ids and decisions, never prompt or tool content.
[logging]
debug = false
'''
