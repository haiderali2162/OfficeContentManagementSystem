"""Whole-file SQLite database copying onto temporary target files."""

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from core.database_copy import DatabaseCopyError, copy_sqlite_database


def _make_source(path: Path) -> None:
    """Create a small SQLite file with two tables and rows inside each."""
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "CREATE TABLE clients (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT)"
        )
        connection.execute(
            "INSERT INTO clients (id, name) VALUES (1, 'Test Client')"
        )
        connection.execute(
            "INSERT INTO app_settings (key, value) VALUES ('theme', 'light')"
        )
        connection.commit()
    finally:
        connection.close()


def _source_engine(path: Path):
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    _make_source(path)
    return engine


def _rows(target: Path, table: str) -> list[tuple]:
    connection = sqlite3.connect(str(target))
    try:
        return connection.execute(f"SELECT * FROM {table}").fetchall()
    finally:
        connection.close()


def test_copy_creates_target_with_all_tables_and_rows(tmp_path):
    source = _source_engine(tmp_path / "source.sqlite3")
    target = tmp_path / "copy.sqlite3"
    try:
        summary = copy_sqlite_database(source, target)
    finally:
        source.dispose()

    assert target.exists()
    assert summary["status"] == "copied"
    assert summary["target"] == str(target.resolve())
    assert summary["tables"] >= 2
    assert _rows(target, "clients") == [(1, "Test Client")]
    assert _rows(target, "app_settings") == [("theme", "light")]
    # The source keeps working after the copy.
    assert _rows(tmp_path / "source.sqlite3", "clients") == [(1, "Test Client")]


def test_copy_replaces_existing_target_content(tmp_path):
    """A chosen file that already holds data gets fully replaced."""
    source = _source_engine(tmp_path / "source.sqlite3")
    target = tmp_path / "existing.sqlite3"
    existing = sqlite3.connect(str(target))
    try:
        existing.execute("CREATE TABLE junk (id INTEGER PRIMARY KEY)")
        existing.execute("INSERT INTO junk VALUES (99)")
        existing.commit()
    finally:
        existing.close()
    try:
        copy_sqlite_database(source, target)
    finally:
        source.dispose()

    # The junk table is gone; the copied tables are there instead.
    connection = sqlite3.connect(str(target))
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        connection.close()
    assert "junk" not in tables
    assert "clients" in tables
    assert _rows(target, "clients") == [(1, "Test Client")]


def test_copy_refuses_the_currently_used_file(tmp_path):
    source_path = tmp_path / "source.sqlite3"
    source = _source_engine(source_path)
    try:
        with pytest.raises(DatabaseCopyError, match="currently in use"):
            copy_sqlite_database(source, source_path)
        # The refused copy leaves the source untouched.
        assert _rows(source_path, "clients") == [(1, "Test Client")]
    finally:
        source.dispose()


def test_copy_refuses_missing_target_folder(tmp_path):
    source = _source_engine(tmp_path / "source.sqlite3")
    try:
        with pytest.raises(DatabaseCopyError, match="does not exist"):
            copy_sqlite_database(source, tmp_path / "missing" / "copy.sqlite3")
    finally:
        source.dispose()


def test_copy_refuses_relative_and_empty_paths(tmp_path):
    source = _source_engine(tmp_path / "source.sqlite3")
    try:
        with pytest.raises(DatabaseCopyError, match="absolute"):
            copy_sqlite_database(source, "relative/copy.sqlite3")
        with pytest.raises(DatabaseCopyError, match="Choose a SQLite file"):
            copy_sqlite_database(source, "")
    finally:
        source.dispose()


def test_copy_refuses_memory_source_and_keeps_target(tmp_path):
    """An in-memory engine has no file to snapshot."""
    engine = create_engine("sqlite://")
    try:
        target = tmp_path / "copy.sqlite3"
        with pytest.raises(DatabaseCopyError, match="no file path|in-memory"):
            copy_sqlite_database(engine, target)
        assert not target.exists()
    finally:
        engine.dispose()
