import os
import traceback
from datetime import UTC, datetime

from .config import Config, harness_home

DEBUG_LOG_NAME = "debug.log"
_MAX_BYTES = 1_000_000
_TRUE = {"1", "true", "yes", "on"}


def debug_enabled(config: Config | None) -> bool:
    """COPILOT_HARNESS_DEBUG overrides the config so debugging works even when config.toml is broken."""
    override = os.environ.get("COPILOT_HARNESS_DEBUG")
    if override is not None:
        return override.strip().lower() in _TRUE
    return bool(config and config.debug)


def debug_log(config: Config | None, message: str) -> None:
    """Append one line to debug.log. Never raises: logging must not break a Copilot hook.

    Messages must not contain prompt text, tool arguments or other session content.
    """
    if not debug_enabled(config):
        return
    try:
        home = config.home if config else harness_home()
        home.mkdir(parents=True, exist_ok=True)
        path = home / DEBUG_LOG_NAME
        if path.exists() and path.stat().st_size > _MAX_BYTES:
            path.replace(home / (DEBUG_LOG_NAME + ".1"))
        stamp = datetime.now(UTC).isoformat(timespec="milliseconds")
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"{stamp} pid={os.getpid()} {message}\n")
    except OSError:
        pass


def debug_exception(config: Config | None, where: str, exc: BaseException) -> None:
    if debug_enabled(config):
        trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip()
        debug_log(config, f"ERROR in {where}: {type(exc).__name__}: {exc}\n{trace}")


def tail_debug_log(config: Config, lines: int = 50) -> list[str]:
    try:
        return (config.home / DEBUG_LOG_NAME).read_text(encoding="utf-8").splitlines()[-lines:]
    except OSError:
        return []
