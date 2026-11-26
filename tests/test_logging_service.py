"""Application logging and crash-report tests on isolated temporary files."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config import APP_VERSION
from core.database import Base
from services import logging_service, settings_service


@pytest.fixture
def logging_database(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'logging_settings.sqlite3').as_posix()}"
    )
    Base.metadata.create_all(engine)
    isolated_sessions = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False
    )
    monkeypatch.setattr(settings_service, "SessionLocal", isolated_sessions)
    monkeypatch.setattr(
        logging_service, "documents_directory", lambda: tmp_path / "Documents"
    )
    try:
        yield tmp_path
    finally:
        logging_service.reset_logging_for_tests()
        engine.dispose()


def test_log_settings_default_to_documents_and_persist_a_folder(
    logging_database, tmp_path
):
    assert logging_service.get_log_settings() == (
        logging_service.DEFAULT_LOG_SETTINGS
    )

    folder = tmp_path / "chosen_logs"
    folder.mkdir()
    saved = logging_service.save_log_settings(
        enabled=True, location=str(folder)
    )
    assert saved == {"enabled": True, "location": str(folder.resolve())}
    assert logging_service.get_log_settings() == saved


def test_log_settings_reject_missing_and_relative_folders(
    logging_database, tmp_path
):
    with pytest.raises(settings_service.SettingsError):
        logging_service.save_log_settings(
            enabled=True, location="relative/logs"
        )
    with pytest.raises(settings_service.SettingsError):
        logging_service.save_log_settings(
            enabled=True, location=str(tmp_path / "missing")
        )
    assert logging_service.get_log_settings()["location"] == ""


def test_configure_logging_writes_redacted_errors_to_documents(
    logging_database
):
    path = logging_service.configure_logging()
    assert path == logging_database / "Documents" / "land_office.log"

    logging_service.log_error(
        "Test failure", RuntimeError("password=secret123 was supplied")
    )
    text = path.read_text(encoding="utf-8")
    assert "Test failure" in text
    assert "password=[redacted]" in text
    assert "secret123" not in text


def test_disabling_logging_removes_the_file_handler(logging_database):
    first = logging_service.configure_logging()
    assert first.is_file()

    logging_service.save_log_settings(enabled=False, location="")
    assert logging_service.configure_logging() is None
    assert not logging_service.get_logger("test").handlers
    assert not logging_service.get_logger("test").parent.handlers or all(
        handler.__class__.__name__ == "NullHandler"
        for handler in logging_service.get_logger("test").parent.handlers
    )


def test_crash_report_includes_version_and_redacts_credentials(
    logging_database
):
    try:
        raise ValueError("token=abc123 failed")
    except ValueError as exc:
        report = logging_service.write_crash_report(exc)

    assert report is not None
    assert report.parent == logging_database / "Documents"
    assert report.name.startswith(logging_service.CRASH_PREFIX)
    text = report.read_text(encoding="utf-8")
    assert APP_VERSION in text
    assert "ValueError" in text
    assert "token=[redacted]" in text
    assert "abc123" not in text


def test_open_log_folder_uses_the_selected_directory(
    logging_database, tmp_path, monkeypatch
):
    folder = tmp_path / "open_logs"
    folder.mkdir()
    logging_service.save_log_settings(enabled=True, location=str(folder))
    opened = []
    monkeypatch.setattr(
        logging_service, "reveal_directory",
        lambda path: opened.append(path) or path,
    )

    result = logging_service.open_log_folder()

    assert opened == [folder.resolve()]
    assert result == folder.resolve()
