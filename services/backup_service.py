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
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.database import engine
from core.database_copy import (
    DatabaseCopyError,
    copy_sqlite_database,
    copy_sqlite_file,
)
from services.settings_service import get_setting, set_setting

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
    """True when the live engine connects to a SQLite file (backable)."""
    return engine.url.get_backend_name() == "sqlite"


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
        summary = copy_sqlite_database(engine, target)
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
    current_name = str(engine.url.database or "").strip()
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
        copy_sqlite_database(engine, safety_path)
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
    last = str(get_setting(_LAST_RUN_KEY, "") or "")
    try:
        last_run = datetime.fromisoformat(last) if last else None
    except ValueError:
        last_run = None
    now = datetime.now(timezone.utc)
    if last_run is not None:
        if last_run.tzinfo is None:
            last_run = last_run.replace(tzinfo=timezone.utc)
        if now - last_run < timedelta(hours=settings["frequency_hours"]):
            return None
    try:
        return create_backup_now(reason="auto")
    except Exception as exc:  # scheduler must survive failures
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
    last = str(get_setting(_LAST_RUN_KEY, "") or "")
    if not last:
        return ""
    try:
        stamp = datetime.fromisoformat(last)
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone()
        return stamp.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return last
