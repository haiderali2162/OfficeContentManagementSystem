"""Automatic and manual SQLite backups (Settings page).

Developer-request batch (2026-11-20): auto-backup with on/off, adjustable
frequency, a chosen backup location, a retention count ("keep the newest
N"), and a quick manual backup — all on the Settings page.

Backups are real SQLite snapshots produced by
:func:`core.database_copy.copy_sqlite_database` (staged, integrity-checked,
atomically swapped), named
``land_office_backup_YYYYMMDD_HHMMSS.sqlite3``. Preferences live in the
existing app-setting store, so no schema change is needed. The scheduler
is a small daemon thread (started from ``app.py``) that checks every
minute whether a backup is due; only file-backed SQLite databases can be
backed up (PostgreSQL/MySQL/custom URLs are skipped with a note).

Version history:

* v0.2.26 — the service captured ``engine`` at import time. After Settings
  switched the live database, ``create_backup_now`` could still target the
  old engine, which made the **Back up now** button appear to do nothing or
  fail. The scheduler had the same stale-engine risk.
* v0.2.27 — the current ``core.database.engine`` is resolved at call time;
  due checks also consider the newest real backup file when the saved
  timestamp is absent, and the Settings page can open the backup folder.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine

# Resolve the engine through the module: ``reconfigure_database`` replaces
# ``core.database.engine`` when the user switches databases in Settings.
from core import database as core_database
from core.database_copy import (
    DatabaseCopyError,
    copy_sqlite_database,
    copy_sqlite_file,
)
from services import logging_service
from services.settings_service import get_setting, reveal_directory, set_setting

_SETTINGS_KEY = "backups.settings"
_LAST_RUN_KEY = "backups.last_run_at"
_FILE_PATTERN = "land_office_backup_*.sqlite3"

DEFAULT_SETTINGS = {
    "enabled": False,
    "frequency_hours": 24,
    "location": "",
    "keep_count": 5,
}

_scheduler_started = False
_BACKUP_NAME_RE = re.compile(
    r"^land_office_backup_(\d{8}_\d{6})(?:_\d+)?\.sqlite3$"
)


def _live_engine() -> Engine:
    """Return the engine currently selected by the running application."""
    return core_database.engine


class BackupServiceError(ValueError):
    """Raised when a backup cannot be created or configured."""


def get_backup_settings() -> dict[str, Any]:
    """Return the saved backup preferences with defaults applied."""
    stored = get_setting(_SETTINGS_KEY, None)
    settings = dict(DEFAULT_SETTINGS)
    if isinstance(stored, dict):
        for key in DEFAULT_SETTINGS:
            if key in stored:
                settings[key] = stored[key]
    try:
        settings["frequency_hours"] = max(1, int(settings["frequency_hours"]))
    except (TypeError, ValueError):
        settings["frequency_hours"] = DEFAULT_SETTINGS["frequency_hours"]
    try:
        settings["keep_count"] = max(1, int(settings["keep_count"]))
    except (TypeError, ValueError):
        settings["keep_count"] = DEFAULT_SETTINGS["keep_count"]
    settings["enabled"] = bool(settings["enabled"])
    settings["location"] = str(settings["location"] or "").strip()
    return settings


def save_backup_settings(
    *,
    enabled: bool,
    frequency_hours: int,
    location: str,
    keep_count: int,
) -> dict[str, Any]:
    """Validate and persist the backup preferences."""
    try:
        frequency = int(frequency_hours)
        keep = int(keep_count)
    except (TypeError, ValueError):
        raise BackupServiceError(
            "Backup frequency and retention count must be whole numbers."
        ) from None
    if frequency < 1:
        raise BackupServiceError("Backup frequency must be at least 1 hour.")
    if keep < 1:
        raise BackupServiceError("Keep at least one old backup.")
    location = str(location or "").strip()
    if enabled and not location:
        raise BackupServiceError(
            "Choose a backup folder before enabling automatic backups."
        )
    if enabled:
        folder = Path(location)
        if not folder.is_absolute():
            raise BackupServiceError(
                "The backup folder must be an absolute path, for example "
                "D:\\Backups\\LandOffice."
            )
        if not folder.exists() or not folder.is_dir():
            raise BackupServiceError(
                f"The backup folder does not exist: {location}"
            )
    settings = {
        "enabled": bool(enabled),
        "frequency_hours": frequency,
        "location": location,
        "keep_count": keep,
    }
    set_setting(_SETTINGS_KEY, settings)
    return settings


def is_sqlite_backend() -> bool:
    """True when the currently selected engine connects to a SQLite file."""
    return _live_engine().url.get_backend_name() == "sqlite"


def list_backups(location: str) -> list[dict[str, Any]]:
    """Existing backup files in the location, newest first."""
    folder = Path(str(location or "").strip())
    if not folder.is_dir():
        return []
    files = [
        item for item in folder.glob(_FILE_PATTERN) if item.is_file()
    ]
    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return [
        {
            "path": str(item),
            "name": item.name,
            "size_bytes": item.stat().st_size,
            "modified": datetime.fromtimestamp(
                item.stat().st_mtime
            ).strftime("%Y-%m-%d %H:%M"),
        }
        for item in files
    ]


def _apply_retention(location: str, keep_count: int) -> list[str]:
    """Delete the oldest backups beyond the retention count."""
    backups = list_backups(location)
    removed = []
    for item in backups[keep_count:]:
        try:
            Path(item["path"]).unlink()
            removed.append(item["name"])
        except OSError:
            continue
    return removed


def _parse_backup_name_time(name: str) -> datetime | None:
    """Read the UTC creation time embedded in a backup file name."""
    match = _BACKUP_NAME_RE.match(str(name or ""))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d_%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _last_backup_datetime(location: str | None = None) -> datetime | None:
    """Newest known backup time from settings or the real backup files.

    v0.2.26 only read ``backups.last_run_at``. A backup made by an older
    build (or copied in from another machine) had no timestamp in the
    settings store, so startup backed up immediately even when a recent
    snapshot existed. v0.2.27 uses both sources and returns the newest.
    """
    times: list[datetime] = []
    stored = str(get_setting(_LAST_RUN_KEY, "") or "")
    if stored:
        try:
            stamp = datetime.fromisoformat(stored)
        except ValueError:
            stamp = None
        if stamp is not None:
            times.append(
                stamp if stamp.tzinfo is not None
                else stamp.replace(tzinfo=timezone.utc)
            )
    folder = str(location or get_backup_settings()["location"] or "").strip()
    for item in list_backups(folder):
        stamp = _parse_backup_name_time(item["name"])
        if stamp is not None:
            times.append(stamp)
    return max(times) if times else None


def _format_local_time(stamp: datetime) -> str:
    """Show a UTC-aware timestamp in the user's local time for status text."""
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone()
    return stamp.strftime("%Y-%m-%d %H:%M")


def create_backup_now(*, reason: str = "manual") -> dict[str, Any]:
    """Create one verified snapshot and apply the retention rule."""
    if not is_sqlite_backend():
        raise BackupServiceError(
            "Automatic backups work with the SQLite database. The app is "
            "currently connected to a different database type; use that "
            "database system's own backup tooling."
        )
    settings = get_backup_settings()
    location = settings["location"]
    if not location:
        raise BackupServiceError(
            "Choose a backup folder in Settings first (even for a manual "
            "backup)."
        )
    folder = Path(location)
    if not folder.exists() or not folder.is_dir():
        raise BackupServiceError(
            f"The backup folder does not exist: {location} — create it or "
            "pick it with Browse folder in Settings."
        )
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    target = folder / f"land_office_backup_{stamp}.sqlite3"
    try:
        # v0.2.27: read the live engine here instead of the import-time copy.
        # A database switched in Settings must be the one backed up.
        summary = copy_sqlite_database(_live_engine(), target)
    except DatabaseCopyError as exc:
        raise BackupServiceError(str(exc)) from exc
    removed = _apply_retention(location, settings["keep_count"])
    set_setting(_LAST_RUN_KEY, datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ))
    return {
        "reason": reason,
        "target": summary.get("target", str(target)),
        "tables": summary.get("tables"),
        "removed_old": removed,
    }


def restore_backup(backup_path: str) -> dict[str, Any]:
    """Restore a backup into a new SQLite file without overwriting the live DB.

    A verified snapshot of the currently connected database is written first
    (``*_before_restore_*``), then the selected backup is copied into a new
    timestamped database (``*_restored_*``). The caller switches the app to
    ``restored_path`` and persists that choice; this service deliberately
    never touches the live file or the saved connection config.

    Args:
        backup_path: absolute path of one of the files returned by
            :func:`list_backups`.

    Returns:
        A summary with ``backup``, ``safety_copy``, ``restored_path`` and
        ``tables``.

    Raises:
        BackupServiceError: for non-SQLite backends, invalid paths, or a
            failed copy. The current database remains untouched.
    """
    if not is_sqlite_backend():
        raise BackupServiceError(
            "Restore is available only while the SQLite database is in use. "
            "Other database systems need their own restore tooling."
        )
    current_name = str(_live_engine().url.database or "").strip()
    if not current_name:
        raise BackupServiceError(
            "The current database has no file path, so it cannot be restored."
        )
    current_path = Path(current_name).resolve()
    if not current_path.is_file():
        raise BackupServiceError(
            f"The current database file does not exist: {current_path}"
        )

    selected = Path(str(backup_path or "").strip())
    if not selected.is_absolute():
        raise BackupServiceError(
            "The backup file path must be absolute. Choose a backup from the "
            "list in Settings."
        )
    selected = selected.resolve()
    if not selected.is_file():
        raise BackupServiceError(
            f"The backup file does not exist: {selected}"
        )
    if selected == current_path:
        raise BackupServiceError(
            "That is the current database, not a backup. Choose a different "
            "file."
        )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safety_path = current_path.with_name(
        f"{current_path.stem}_before_restore_{stamp}{current_path.suffix}"
    )
    restored_path = current_path.with_name(
        f"{current_path.stem}_restored_{stamp}{current_path.suffix}"
    )
    try:
        copy_sqlite_database(_live_engine(), safety_path)
        summary = copy_sqlite_file(selected, restored_path)
    except (DatabaseCopyError, sqlite3.Error) as exc:
        raise BackupServiceError(str(exc)) from exc
    return {
        "backup": str(selected),
        "safety_copy": str(safety_path),
        "restored_path": str(restored_path),
        "tables": summary.get("tables"),
    }


def run_backup_if_due() -> dict[str, Any] | None:
    """Create an automatic backup when the frequency interval has passed.

    Returns the backup summary, ``None`` when disabled/not due/not a
    SQLite database. Never raises: the scheduler logs failures into the
    returned status dict instead of crashing the background thread.
    """
    settings = get_backup_settings()
    if not settings["enabled"] or not is_sqlite_backend():
        return None
    # v0.2.27: include real backup files in the due check so a snapshot made
    # by an older build still counts as the last backup after an upgrade.
    last_run = _last_backup_datetime(settings["location"])
    now = datetime.now(timezone.utc)
    if last_run is not None and now - last_run < timedelta(
        hours=settings["frequency_hours"]
    ):
        return None
    try:
        return create_backup_now(reason="auto")
    except Exception as exc:  # scheduler must survive failures
        # v0.2.27: keep the failure visible after the desktop window closes.
        logging_service.log_error("Automatic backup failed", exc)
        return {"reason": "auto", "error": str(exc)}


def start_backup_scheduler() -> None:
    """Start the once-per-minute due-check daemon thread (idempotent)."""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    def loop():
        while True:
            try:
                run_backup_if_due()
            except Exception:
                pass
            threading.Event().wait(60)

    thread = threading.Thread(target=loop, name="backup-scheduler", daemon=True)
    thread.start()


def get_last_backup_time() -> str:
    """Human-readable last automatic/manual backup time (blank if never)."""
    last = _last_backup_datetime()
    return _format_local_time(last) if last else ""


def backup_status(location: str | None = None) -> dict[str, Any]:
    """Describe the last backup, elapsed time, and whether startup is due.

    The Settings page uses this instead of recalculating the schedule in an
    event handler. ``due`` is true only when automatic backups are enabled,
    the active engine is SQLite, and the frequency interval has passed (or
    no backup can be found yet).
    """
    settings = get_backup_settings()
    folder = str(location or settings["location"] or "").strip()
    last = _last_backup_datetime(folder)
    now = datetime.now(timezone.utc)
    hours_since = (
        round((now - last).total_seconds() / 3600, 1) if last else None
    )
    due = bool(
        settings["enabled"]
        and is_sqlite_backend()
        and (
            last is None
            or now - last >= timedelta(hours=settings["frequency_hours"])
        )
    )
    next_due = (
        last + timedelta(hours=settings["frequency_hours"])
        if last and settings["enabled"] else None
    )
    if not settings["enabled"]:
        message = "Automatic backups are disabled."
    elif not is_sqlite_backend():
        message = "Automatic backups require the SQLite database backend."
    elif last is None:
        message = "No backup found; the next startup check will create one."
    elif due:
        message = (
            f"Last backup {_format_local_time(last)} ({hours_since} hours ago); "
            "a backup is due."
        )
    else:
        message = (
            f"Last backup {_format_local_time(last)} ({hours_since} hours ago); "
            f"next at {_format_local_time(next_due)}."
        )
    return {
        "enabled": settings["enabled"],
        "location": folder,
        "last_backup": _format_local_time(last) if last else "",
        "hours_since": hours_since,
        "due": due,
        "next_backup": _format_local_time(next_due) if next_due else "",
        "message": message,
    }


def open_backup_folder(location: str | None = None) -> Path:
    """Open the configured backup folder in the operating system's file manager."""
    folder = str(location or get_backup_settings()["location"] or "").strip()
    if not folder:
        raise BackupServiceError(
            "Choose a backup folder in Settings first."
        )
    try:
        path = reveal_directory(folder)
    except ValueError as exc:
        raise BackupServiceError(str(exc)) from exc
    return path
