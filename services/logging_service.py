"""Application error logging and automatic crash reports.

Version history:

* v0.2.26 — backend failures were shown in the UI or printed to the console
  with ``traceback.print_exc``. A packaged run could close before the user
  copied that text, and there was no central record of startup or scheduler
  failures.
* v0.2.27 — a rotating ``land_office.log`` is written to the user's Documents
  folder (or a folder chosen in Settings). Unhandled Python and asyncio errors
  also write a separate timestamped crash report. Credential-looking values
  are redacted, and no client names or database contents are logged here.

Preferences are stored in the existing ``app_settings`` table, so this adds
no schema change or migration. Logging is disabled cleanly when the user
turns it off; the Settings page can open the active log folder.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any

from core.config import APP_VERSION
from services.settings_service import (
    SettingsError,
    get_setting,
    normalize_directory_path,
    reveal_directory,
    set_setting,
)

LOG_SETTINGS_KEY = "logs.settings"
DEFAULT_LOG_SETTINGS = {
    "enabled": True,
    # Empty means the user's Documents folder. An absolute folder here
    # overrides it without changing the application's working directory.
    "location": "",
}
LOG_FILENAME = "land_office.log"
CRASH_PREFIX = "land_office_crash_"
_LOGGER_NAME = "land_office"
_MAX_LOG_BYTES = 2_000_000
_BACKUP_COUNT = 5

_configured = False
_hooks_installed = False
_original_excepthook = None

# Only patterns explicitly typed like credentials are redacted. Ordinary
# error text is preserved so the log stays useful for diagnosing failures.
_SECRET_PATTERN = re.compile(
    r"(?i)\b(password|passwd|pin|token|secret|api[_-]?key)\s*[:=]\s*\S+"
)


def get_log_settings() -> dict[str, Any]:
    """Return saved logging preferences with defaults applied."""
    stored = get_setting(LOG_SETTINGS_KEY, None)
    settings = dict(DEFAULT_LOG_SETTINGS)
    if isinstance(stored, dict):
        if "enabled" in stored:
            settings["enabled"] = bool(stored["enabled"])
        if "location" in stored:
            settings["location"] = str(stored["location"] or "").strip()
    try:
        settings["location"] = normalize_directory_path(settings["location"])
    except SettingsError:
        # A moved or deleted folder must not break startup; fall back to
        # Documents instead of silently writing into an unexpected directory.
        settings["location"] = ""
    return settings


def save_log_settings(*, enabled: bool, location: str) -> dict[str, Any]:
    """Validate and persist the logging preferences."""
    normalized = normalize_directory_path(location)
    settings = {"enabled": bool(enabled), "location": normalized}
    set_setting(LOG_SETTINGS_KEY, settings)
    return settings


def documents_directory() -> Path:
    """Resolve the user's Documents folder on Windows, macOS, or Linux."""
    if sys.platform == "win32":
        user_profile = os.environ.get("USERPROFILE")
        base = Path(user_profile) if user_profile else Path.home()
        return base / "Documents"
    if sys.platform == "darwin":
        return Path.home() / "Documents"

    config_file = Path.home() / ".config" / "user-dirs.dirs"
    try:
        for line in config_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("XDG_DOCUMENTS_DIR="):
                configured = line.partition("=")[2].strip().strip('"')
                configured = configured.replace("$HOME", str(Path.home()))
                resolved = Path(configured).expanduser()
                if resolved.is_absolute():
                    return resolved
    except OSError:
        pass
    return Path.home() / "Documents"


def log_directory() -> Path:
    """Return the effective log folder, creating it only when needed."""
    location = get_log_settings()["location"]
    folder = Path(location) if location else documents_directory()
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def log_file_path() -> Path:
    """Path of the rotating application log."""
    return log_directory() / LOG_FILENAME


def get_logger(name: str) -> logging.Logger:
    """Return a logger that writes through the application's file handler."""
    return logging.getLogger(f"{_LOGGER_NAME}.{name}")


def _remove_handlers() -> None:
    """Detach and close this application's file handlers (used by Save)."""
    global _configured
    logger = logging.getLogger(_LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except OSError:
            pass
    _configured = False


def configure_logging() -> Path | None:
    """Install the rotating file handler once and return the log path.

    Returns ``None`` when logging is disabled. Re-applying settings closes the
    old handler first, so changing the folder never leaves two logs running.
    """
    global _configured
    settings = get_log_settings()
    if not settings["enabled"]:
        _remove_handlers()
        return None
    if _configured:
        return log_file_path()

    target = log_file_path()
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = logging.handlers.RotatingFileHandler(
        target, maxBytes=_MAX_LOG_BYTES, backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    ))
    logger.addHandler(handler)
    _configured = True
    logger.info("Application log started, version %s", APP_VERSION)
    return target


def reconfigure_logging() -> Path | None:
    """Apply saved logging settings immediately after a Settings change."""
    _remove_handlers()
    return configure_logging()


def _redact(text: str) -> str:
    """Remove credential-looking values from a diagnostic message."""
    return _SECRET_PATTERN.sub(r"\1=[redacted]", str(text))


def write_crash_report(
    error: BaseException,
    traceback_text: str | None = None,
) -> Path | None:
    """Write a separate crash file when an unhandled error reaches the app."""
    if not get_log_settings()["enabled"]:
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = log_directory() / f"{CRASH_PREFIX}{stamp}.log"
    detail = traceback_text or "".join(traceback.format_exception(
        type(error), error, error.__traceback__
    ))
    body = [
        "Land Office Manager crash report",
        f"Time: {datetime.now().isoformat(timespec='seconds')}",
        f"Version: {APP_VERSION}",
        f"Error: {type(error).__name__}: {_redact(error)}",
        "",
        "Traceback:",
        _redact(detail).rstrip(),
        "",
    ]
    try:
        path.write_text("\n".join(body), encoding="utf-8")
    except OSError:
        # Logging must never turn a crash into a second exception.
        return None
    return path


def log_error(event: str, error: BaseException) -> None:
    """Record a handled backend failure with a redacted traceback."""
    detail = "".join(traceback.format_exception(
        type(error), error, error.__traceback__
    ))
    # Do not pass ``exc_info`` to logging: the raw traceback would bypass
    # redaction and could repeat a credential that appeared in an error text.
    get_logger("error").error(
        "%s: %s: %s\n%s", event, type(error).__name__, _redact(error),
        _redact(detail).rstrip(),
    )


def log_unhandled_exception(
    exc_type: type[BaseException],
    error: BaseException,
    tb: TracebackType | None,
) -> None:
    """Log an uncaught exception, then write its crash report."""
    if not get_log_settings()["enabled"]:
        return
    try:
        detail = "".join(traceback.format_exception(exc_type, error, tb))
        get_logger("crash").critical(
            "Unhandled exception: %s: %s\n%s",
            exc_type.__name__, _redact(error), _redact(detail).rstrip(),
        )
        write_crash_report(
            error, "".join(traceback.format_exception(exc_type, error, tb))
        )
    except Exception:  # noqa: BLE001 - must not replace the original crash
        pass


def log_async_exception(context: dict[str, Any]) -> None:
    """Log an asyncio loop error that is not an expected shutdown event."""
    if not get_log_settings()["enabled"]:
        return
    error = context.get("exception")
    if isinstance(error, BaseException):
        log_unhandled_exception(type(error), error, error.__traceback__)
        return
    message = _redact(context.get("message") or "Asynchronous task failed")
    try:
        get_logger("crash").critical("Unhandled asyncio error: %s", message)
    except Exception:  # noqa: BLE001 - must not replace the original error
        pass


def install_exception_hooks() -> None:
    """Install the process-wide uncaught exception logger once."""
    global _hooks_installed, _original_excepthook
    if _hooks_installed:
        return
    _original_excepthook = sys.excepthook

    def logged_excepthook(exc_type, error, tb):
        log_unhandled_exception(exc_type, error, tb)
        if _original_excepthook is not None:
            _original_excepthook(exc_type, error, tb)

    sys.excepthook = logged_excepthook
    _hooks_installed = True


def open_log_folder() -> Path:
    """Open the active log folder in the operating system's file manager."""
    try:
        return reveal_directory(log_directory())
    except SettingsError:
        raise


def reset_logging_for_tests() -> None:
    """Reset module state so isolated tests can reconfigure temp folders."""
    global _hooks_installed, _original_excepthook
    _remove_handlers()
    if _hooks_installed and _original_excepthook is not None:
        sys.excepthook = _original_excepthook
    _hooks_installed = False
    _original_excepthook = None
