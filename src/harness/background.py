import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from .config import Config
from .curator import maybe_consolidate
from .debug import debug_exception, debug_log
from .errors import HarnessError
from .reflection import auto_learn, count_tool_events, session_is_ready
from .storage import Storage

_LOG_NAME = "reflection.log"
_FAILURE_COOLDOWN_SECONDS = 600


def _key(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:80]


def _paths(config: Config, session_id: str) -> tuple[Path, Path]:
    directory = config.home / "locks"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{_key(session_id)}.lock", directory / f"{_key(session_id)}.failed"


def _lock_is_live(path: Path, config: Config) -> bool:
    try:
        return time.time() - path.stat().st_mtime <= config.reflect_timeout + 60
    except FileNotFoundError:
        return False


@contextmanager
def _session_lock(config: Config, session_id: str) -> Iterator[bool]:
    path, _ = _paths(config, session_id)
    for _attempt in range(2):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _lock_is_live(path, config):
                yield False
                return
            path.unlink(missing_ok=True)
            continue
        os.close(descriptor)
        try:
            yield True
        finally:
            path.unlink(missing_ok=True)
        return
    yield False


def log_reflection(config: Config, message: str) -> None:
    config.home.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    with (config.home / _LOG_NAME).open("a", encoding="utf-8") as stream:
        stream.write(f"{stamp} {message}\n")


def last_reflection_log(config: Config) -> str | None:
    try:
        lines = (config.home / _LOG_NAME).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    return lines[-1] if lines else None


def reflect_session(
    storage: Storage,
    config: Config,
    cwd: Path,
    session_id: str,
    force: bool = False,
) -> str | None:
    """Run one guarded reflection and return a user-facing outcome, or None if nothing ran."""
    _, failed = _paths(config, session_id)
    started = time.monotonic()
    debug_log(config, f"reflect_session start session={session_id} force={force}")
    with _session_lock(config, session_id) as acquired:
        if not acquired:
            debug_log(config, f"reflect_session skipped: another reflection holds the lock session={session_id}")
            return None
        try:
            proposal, path = auto_learn(storage, config, cwd, session_id, force=force)
        except HarnessError as exc:
            failed.touch()
            debug_exception(config, "reflect_session", exc)
            raise
        outcome = proposal["action"] if proposal else "none"
        debug_log(config, f"reflect_session done in {time.monotonic() - started:.1f}s outcome={outcome}")
        failed.unlink(missing_ok=True)
        try:
            for item in maybe_consolidate(storage, config, cwd):
                merged = item["proposal"]
                log_reflection(
                    config,
                    f"{'Merged' if item['applied'] else 'Merge proposed (needs review)'} "
                    f"{', '.join(merged['absorbs'])} into {merged['name']} "
                    f"[proposal: {merged['id']}]",
                )
        except HarnessError as exc:
            log_reflection(config, f"consolidation failed: {exc}")
    if proposal is None:
        return None
    if path:
        return f"Learned {proposal['name']} from session {session_id}: {path}"
    return (
        f"Candidate {proposal['name']} requires human review "
        f"(risk: {proposal['risk_level']}; proposal: {proposal['id']})"
    )


def _mid_session_decision(
    storage: Storage, config: Config, cwd: Path, session_id: str
) -> tuple[bool, str]:
    if config.mode != "auto":
        return False, f"mode is {config.mode}, not auto"
    if not config.reflect_during_session:
        return False, "[reflection].during_session is false"
    if not config.reflect_command:
        return False, "no reflector command configured"
    path, failed = _paths(config, session_id)
    if _lock_is_live(path, config):
        return False, "a reflection is already running"
    try:
        if time.time() - failed.stat().st_mtime < _FAILURE_COOLDOWN_SECONDS:
            return False, "cooling down after a failed reflection"
    except FileNotFoundError:
        pass
    marked_id, _ = storage.reflection_mark(session_id, str(cwd))
    events = storage.session_events(session_id, str(cwd), after_id=marked_id)
    if not session_is_ready(events, config):
        return False, (
            f"below threshold ({count_tool_events(events)}/{config.tool_calls_before_learn} new tool events)"
        )
    return True, "threshold reached"


def should_reflect_mid_session(
    storage: Storage, config: Config, cwd: Path, session_id: str
) -> bool:
    ready, reason = _mid_session_decision(storage, config, cwd, session_id)
    debug_log(config, f"mid-session reflection {'WILL start' if ready else 'not started'}: {reason}")
    return ready


def spawn_background_reflection(cwd: Path, session_id: str, config: Config | None = None) -> None:
    command = [sys.executable, "-m", "harness", "reflect-session", session_id, "--cwd", str(cwd)]
    options: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        options["creationflags"] = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(command, **options)
    debug_log(config, f"spawned background reflection child_pid={process.pid} session={session_id}")
