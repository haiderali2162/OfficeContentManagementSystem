"""Whole-file copying of the application's SQLite database.

The settings page's "Database" editor lets the user point the application at
a new SQLite file. By default the switch starts empty (the new file just gets
the application tables created by ``initialize_database``). When the user
asks for it, an **opt-in** full copy of the currently connected SQLite
database is written to the new file first, so the app continues with the same
records it had before the switch.

Design notes:

* The copy is a *whole-file snapshot* using ``sqlite3.Connection.backup``.
  That copies every page of the file in one operation, safely working from a
  live connection (it restarts if the source changes underneath). It is not a
  row-by-row transfer, so no table, trigger, index, or pragma is missed.
* The snapshot **replaces** the target file's content. If the target file
  exists and holds data, that data is gone after the copy succeeds. The
  settings UI states this next to the checkbox; users must opt in.
* Cross-backend copies (SQLite → PostgreSQL, etc.) are deliberately *not*
  supported here. Different engines need different data types, sequences, and
  migration steps; silently guessing a mapping is the kind of data-integrity
  risk this project avoids.
* Failures are raised as :class:`DatabaseCopyError` with a clear, non-technical
  message so the settings page can show the reason inline and the user keep
  the old, still-connected database.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.engine import Engine


class DatabaseCopyError(ValueError):
    """Raised when the SQLite snapshot copy cannot be performed safely."""


def _source_file_path(source_engine: Engine) -> Path:
    """Extract the on-disk path of an engine connected to a SQLite file.

    Shared-memory databases (``:memory:``) have no file to copy, so they are
    rejected: the point of the helper is to produce a portable file.
    """
    database_name = source_engine.url.database
    if not database_name:
        raise DatabaseCopyError(
            "The current database has no file path, so it cannot be copied."
        )
    source_path = Path(database_name)
    if source_path.as_posix() == ":memory:" or not source_path.as_posix():
        raise DatabaseCopyError(
            "The current database is in-memory only and cannot be copied."
        )
    return source_path.resolve()


def _target_file_path(target_path) -> Path:
    """Validate and normalise the destination path for the snapshot."""
    if target_path in (None, ""):
        raise DatabaseCopyError("Choose a SQLite file to copy the data into.")
    target = Path(str(target_path))
    if not target.is_absolute():
        # Relative paths would depend on the launch directory; the settings
        # editor already stores an absolute path, so require one here too.
        raise DatabaseCopyError(
            "The SQLite file path must be absolute (for example "
            "D:/Data/land_office.sqlite3)."
        )
    target = target.resolve()
    parent = target.parent
    if not parent.is_dir():
        raise DatabaseCopyError(
            f"The folder {parent} does not exist, so the file cannot be "
            "created."
        )
    return target


def copy_sqlite_database(source_engine: Engine, target_path) -> dict:
    """Copy the whole current SQLite database into ``target_path``.

    The copy is performed in two phases so an existing target file can never
    be left half-written:

    1. The snapshot is written to a temporary file next to the target and
       verified with SQLite's ``integrity_check``.
    2. Only after a verified copy exists is it swapped onto the target path
       with ``os.replace`` — an atomic operation on the same volume.

    It runs from a worker thread (the settings UI calls it through
    ``asyncio.to_thread``), so blocking on file I/O here is intended.

    Args:
        source_engine: the SQLAlchemy engine currently in use. It must be
            connected to a SQLite *file* (not ``:memory:``).
        target_path: absolute file path of the destination SQLite file.

    Returns:
        A summary dictionary describing the copy, with ``source``, ``target``,
        ``tables`` (amount of user tables found in the source) and ``status``.

    Raises:
        DatabaseCopyError: when the source or target is not a usable SQLite
            file, source and target are the same file, or the finished copy
            fails its integrity check (in which case the target keeps its
            previous content).
        sqlite3.Error: when the file-level backup itself fails.
    """
    source = _source_file_path(source_engine)
    target = _target_file_path(target_path)

    # Copying a file onto itself has no defined result: refuse it outright.
    if source == target:
        raise DatabaseCopyError(
            "The chosen file is the database currently in use. Pick a "
            "different file name or folder to copy the data."
        )

    tables = inspect(source_engine).get_table_names()

    # The source is read through its own new connection to the same file the
    # engine has open. SQLite allows concurrent readers; ``backup`` restarts
    # its page walk if the source changes underneath, so the snapshot stays
    # consistent even while the app is running.
    source_file = source.as_posix()
    raw_source = sqlite3.connect(source_file)
    staging_connection = None
    # A unique sibling name keeps parallel saves from clobbering each other.
    staging_path = target.with_name(
        f"{target.stem}.copy_{os.getpid()}{target.suffix}"
    )
    try:
        # Phase 1: build and verify the snapshot in a temporary file first.
        # The real target is not touched until the copy is proven good, so a
        # disk-full or locked-source failure leaves any existing target data
        # exactly as it was.
        staging_path.unlink(missing_ok=True)
        staging_connection = sqlite3.connect(str(staging_path))
        raw_source.backup(staging_connection)
        staging_connection.commit()
        check = staging_connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()
        if check and check[0] != "ok":
            raise DatabaseCopyError(
                "The copied database failed its integrity check; the "
                "selected file was not changed."
            )
        staging_connection.close()
        staging_connection = None
        # Phase 2: an atomic same-volume replace puts the verified snapshot
        # at the target path. Any previous content there is replaced.
        os.replace(staging_path, target)
    except Exception:
        staging_path.unlink(missing_ok=True)
        raise
    finally:
        if staging_connection is not None:
            staging_connection.close()
        raw_source.close()

    return {
        "source": source_file,
        "target": str(target),
        "tables": len(tables),
        "status": "copied",
    }


def copy_sqlite_file(source_path, target_path) -> dict:
    """Copy an existing SQLite file into a new, integrity-checked file.

    This is the restore counterpart of :func:`copy_sqlite_database`: the
    source is a backup file instead of the live engine. The backup is never
    modified; the destination is written to a staging file first and only
    replaces its target after SQLite's ``integrity_check`` passes.

    Args:
        source_path: absolute path of an existing SQLite backup file.
        target_path: absolute path of the new database file to create.

    Returns:
        A summary dictionary with ``source``, ``target``, ``tables`` and
        ``status``.

    Raises:
        DatabaseCopyError: when the paths are invalid, the source is not a
            healthy SQLite file, or the copy fails verification.
        sqlite3.Error: when the file-level copy itself fails.
    """
    raw_source_path = str(source_path or "").strip()
    if not raw_source_path:
        raise DatabaseCopyError("Choose a backup file to restore.")
    source = Path(raw_source_path)
    if not source.is_absolute():
        raise DatabaseCopyError(
            "The backup file path must be absolute (for example "
            "D:/Backups/land_office_backup_20260101_120000.sqlite3)."
        )
    source = source.resolve()
    if not source.is_file():
        raise DatabaseCopyError(
            f"The backup file does not exist: {source}"
        )
    target = _target_file_path(target_path)
    if source == target:
        raise DatabaseCopyError(
            "The backup file and the restore destination are the same file."
        )

    raw_source = sqlite3.connect(str(source))
    staging_connection = None
    staging_path = target.with_name(
        f"{target.stem}.restore_{os.getpid()}{target.suffix}"
    )
    try:
        check = raw_source.execute("PRAGMA integrity_check").fetchone()
        if not check or check[0] != "ok":
            raise DatabaseCopyError(
                "The selected backup failed its integrity check; no file "
                "was changed."
            )
        tables = raw_source.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]

        staging_path.unlink(missing_ok=True)
        staging_connection = sqlite3.connect(str(staging_path))
        raw_source.backup(staging_connection)
        staging_connection.commit()
        copied_check = staging_connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()
        if not copied_check or copied_check[0] != "ok":
            raise DatabaseCopyError(
                "The restored copy failed its integrity check; the "
                "selected backup was not changed."
            )
        staging_connection.close()
        staging_connection = None
        os.replace(staging_path, target)
    except Exception:
        staging_path.unlink(missing_ok=True)
        raise
    finally:
        if staging_connection is not None:
            staging_connection.close()
        raw_source.close()

    return {
        "source": str(source),
        "target": str(target),
        "tables": tables,
        "status": "copied",
    }
