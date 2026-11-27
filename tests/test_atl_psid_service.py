"""ATL PSID/challan service tests on isolated temporary SQLite databases."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.orm import sessionmaker

from core.database import Base
from core.models import Client, ClientAtlPsid
from services import (
    atl_psid_archive_service as archive_service,
    atl_psid_service as atl_service,
    client_service,
    settings_service,
)


PDF_BYTES = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"


@pytest.fixture
def atl_database(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'atl_clients.sqlite3').as_posix()}"
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    sessions = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False
    )
    monkeypatch.setattr(atl_service, "SessionLocal", sessions)
    monkeypatch.setattr(client_service, "SessionLocal", sessions)
    monkeypatch.setattr(settings_service, "SessionLocal", sessions)

    archive_root = tmp_path / "app_archive"
    monkeypatch.setattr(archive_service, "ARCHIVE_DIR", archive_root)
    monkeypatch.setattr(
        archive_service, "ARCHIVE_PDF_DIR", archive_root / "pdfs"
    )
    monkeypatch.setattr(
        archive_service, "ARCHIVE_DB_PATH", archive_root / "atl_archive.sqlite3"
    )
    archive_service.reset_archive_engine()

    managed_root = tmp_path / "app_managed"
    monkeypatch.setattr(atl_service, "MANAGED_PDF_DIR", managed_root / "pdfs")
    monkeypatch.setattr(
        atl_service, "PREVIEW_PDF_DIR", managed_root / "preview"
    )

    with sessions.begin() as session:
        client = Client(name="Test Client", cnic="00000-0000000-0")
        session.add(client)
        session.flush()
        client_id = client.id

    try:
        yield sessions, client_id, archive_root
    finally:
        archive_service.reset_archive_engine()
        engine.dispose()


def _archive_rows():
    engine = archive_service.ensure_archive()
    with engine.connect() as connection:
        return list(
            connection.execute(
                select(archive_service.ARCHIVED_PSIDS)
            ).mappings().all()
        )


def test_database_pdf_records_are_listed_sorted_and_deleted(atl_database):
    sessions, client_id, _archive_root = atl_database
    newer = atl_service.save_psid(
        client_id=client_id,
        financial_year="2026-27",
        psid_number="PSID-2",
        amount="1250.50",
        storage_mode="database",
        pdf_bytes=PDF_BYTES,
        original_filename="challan two.pdf",
    )
    older = atl_service.save_psid(
        client_id=client_id,
        financial_year="2025-26",
        psid_number="PSID-1",
        storage_mode="database",
        pdf_bytes=PDF_BYTES,
    )

    records = atl_service.list_psids(client_id)
    assert [record.id for record in records] == [newer, older]
    assert records[0].amount is not None
    assert str(records[0].amount) == "1250.50"
    with sessions() as session:
        stored = session.get(ClientAtlPsid, newer)
        assert bytes(stored.pdf_blob) == PDF_BYTES

    atl_service.delete_psid(older)
    assert [record.id for record in atl_service.list_psids(client_id)] == [newer]


def test_managed_file_copy_is_saved_downloaded_and_removed(
    atl_database, tmp_path
):
    _sessions, client_id, _archive_root = atl_database
    source = tmp_path / "source challan.pdf"
    source.write_bytes(PDF_BYTES)

    psid_id = atl_service.save_psid(
        client_id=client_id,
        financial_year="2025-26",
        psid_number="PSID-M",
        storage_mode="managed_file",
        pdf_path=str(source),
    )
    record = atl_service.get_psid(psid_id)
    assert record.storage_mode == "managed_file"
    managed_copy = atl_service._managed_path(record.pdf_relative_path)
    assert managed_copy.is_file()
    assert managed_copy.read_bytes() == PDF_BYTES

    destination = tmp_path / "download" / "saved-challan.pdf"
    assert atl_service.save_psid_pdf(psid_id, destination) == destination
    assert destination.read_bytes() == PDF_BYTES

    atl_service.delete_psid(psid_id)
    assert not managed_copy.exists()


def test_linked_storage_never_deletes_the_original(atl_database, tmp_path):
    _sessions, client_id, _archive_root = atl_database
    original = tmp_path / "my own challan.pdf"
    original.write_bytes(PDF_BYTES)

    psid_id = atl_service.save_psid(
        client_id=client_id,
        financial_year="2025-26",
        psid_number="PSID-L",
        storage_mode="linked_path",
        pdf_path=str(original),
    )
    atl_service.delete_psid(psid_id)
    assert original.is_file(), "Deleting a PSID must not touch a linked original."


def test_text_only_edit_of_no_pdf_record_is_allowed(atl_database):
    sessions, client_id, _archive_root = atl_database
    psid_id = atl_service.save_psid(
        client_id=client_id,
        financial_year="2025-26",
        psid_number="PSID-NOPDF",
        notes="before",
    )

    saved_id = atl_service.save_psid(
        client_id=client_id,
        psid_id=psid_id,
        financial_year="2025-26",
        psid_number="PSID-NOPDF",
        notes="after",
        storage_mode="database",
    )
    record = atl_service.get_psid(saved_id)
    assert record.notes == "after"
    assert record.storage_mode == "none"
    with sessions() as session:
        stored = session.get(ClientAtlPsid, saved_id)
        assert stored.pdf_blob is None


def test_invalid_or_missing_pdf_is_rejected(atl_database, tmp_path):
    _sessions, client_id, _archive_root = atl_database
    with pytest.raises(atl_service.AtlPsidError, match="not a valid PDF"):
        atl_service.save_psid(
            client_id=client_id,
            financial_year="2025-26",
            psid_number="PSID-BAD",
            storage_mode="database",
            pdf_bytes=b"not a pdf",
        )
    with pytest.raises(atl_service.AtlPsidError, match="Choose a storage option"):
        atl_service.save_psid(
            client_id=client_id,
            financial_year="2025-26",
            psid_number="PSID-NONE",
            pdf_bytes=PDF_BYTES,
        )
    with pytest.raises(atl_service.AtlPsidError, match="YYYY-MM-DD"):
        atl_service.save_psid(
            client_id=client_id,
            financial_year="2025-26",
            psid_number="PSID-DATE",
            expiry_date="31-12-2026",
        )


def test_expiry_uses_record_date_over_default_and_is_idempotent(atl_database):
    _sessions, client_id, archive_root = atl_database
    settings_service.set_atl_psid_archive_settings({
        "enabled": True,
        "default_expiry_days": 365,
        "retention_days": 10,
    })
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    psid_id = atl_service.save_psid(
        client_id=client_id,
        financial_year="2025-26",
        psid_number="PSID-EXPIRED",
        amount="500",
        storage_mode="database",
        pdf_bytes=PDF_BYTES,
        expiry_date=yesterday,
    )

    result = atl_service.run_cleanup(force=True)
    assert result["status"] == "completed"
    assert result["archived"] == 1

    record = atl_service.get_psid(psid_id)
    assert record.psid_number == "PSID-EXPIRED"
    assert record.storage_mode == "none"
    assert record.expiry_processed_at is not None
    assert len(_archive_rows()) == 1
    archived_pdf = archive_root / "pdfs" / _archive_rows()[0]["pdf_relative_path"]
    assert archived_pdf.read_bytes() == PDF_BYTES

    repeat = atl_service.run_cleanup(force=True)
    assert repeat["archived"] == 0
    assert len(_archive_rows()) == 1


def test_default_age_and_retention_purge_are_configurable(
    atl_database, tmp_path
):
    sessions, client_id, archive_root = atl_database
    settings_service.set_atl_psid_archive_settings({
        "enabled": True,
        "default_expiry_days": 1,
        "retention_days": 1,
    })
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF_BYTES)
    psid_id = atl_service.save_psid(
        client_id=client_id,
        financial_year="2024-25",
        psid_number="PSID-OLD",
        storage_mode="managed_file",
        pdf_path=str(source),
    )
    old = datetime.now(timezone.utc) - timedelta(days=10)
    with sessions.begin() as session:
        session.execute(
            update(ClientAtlPsid)
            .where(ClientAtlPsid.id == psid_id)
            .values(created_at=old)
        )

    first = atl_service.run_cleanup(force=True)
    assert first["archived"] == 1
    rows = _archive_rows()
    assert len(rows) == 1
    archived_pdf = archive_root / "pdfs" / rows[0]["pdf_relative_path"]
    assert archived_pdf.is_file()

    engine = archive_service.ensure_archive()
    with engine.begin() as connection:
        connection.execute(
            update(archive_service.ARCHIVED_PSIDS).values(archived_at=old)
        )
    second = atl_service.run_cleanup(force=True)
    assert second["purged_rows"] == 1
    assert second["purged_pdfs"] == 1
    assert not archived_pdf.exists()
    assert not _archive_rows()


def test_linked_expiry_copies_pdf_but_preserves_original(atl_database, tmp_path):
    _sessions, client_id, archive_root = atl_database
    settings_service.set_atl_psid_archive_settings({
        "enabled": True,
        "default_expiry_days": 0,
        "retention_days": 0,
    })
    original = tmp_path / "keep this challan.pdf"
    original.write_bytes(PDF_BYTES)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    psid_id = atl_service.save_psid(
        client_id=client_id,
        financial_year="2025-26",
        psid_number="PSID-LINK-EXPIRY",
        storage_mode="linked_path",
        pdf_path=str(original),
        expiry_date=yesterday,
    )

    assert atl_service.run_cleanup(force=True)["archived"] == 1
    assert original.is_file()
    record = atl_service.get_psid(psid_id)
    assert record.storage_mode == "none"
    rows = _archive_rows()
    assert len(rows) == 1
    assert (archive_root / "pdfs" / rows[0]["pdf_relative_path"]).is_file()


def test_cleanup_is_skipped_until_enabled(atl_database):
    _sessions, client_id, _archive_root = atl_database
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    atl_service.save_psid(
        client_id=client_id,
        financial_year="2025-26",
        psid_number="PSID-DISABLED",
        storage_mode="database",
        pdf_bytes=PDF_BYTES,
        expiry_date=yesterday,
    )
    result = atl_service.run_cleanup()
    assert result["status"] == "disabled"
    assert not _archive_rows()


def test_scheduler_due_check_runs_once_per_day(atl_database):
    _sessions, client_id, _archive_root = atl_database
    settings_service.set_atl_psid_archive_settings({"enabled": True})
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    atl_service.save_psid(
        client_id=client_id,
        financial_year="2025-26",
        psid_number="PSID-SCHEDULE",
        storage_mode="database",
        pdf_bytes=PDF_BYTES,
        expiry_date=yesterday,
    )
    first = atl_service.run_cleanup_if_due()
    assert first is not None and first["archived"] == 1
    assert atl_service.run_cleanup_if_due() is None


def test_client_deletion_removes_managed_copy_but_preserves_linked_file(
    atl_database, tmp_path
):
    _sessions, client_id, archive_root = atl_database
    source = tmp_path / "linked.pdf"
    source.write_bytes(PDF_BYTES)
    managed_id = atl_service.save_psid(
        client_id=client_id,
        financial_year="2025-26",
        psid_number="PSID-MANAGED",
        storage_mode="managed_file",
        pdf_bytes=PDF_BYTES,
    )
    atl_service.save_psid(
        client_id=client_id,
        financial_year="2025-26",
        psid_number="PSID-LINK",
        storage_mode="linked_path",
        pdf_path=str(source),
    )
    managed_copy = atl_service._managed_path(
        atl_service.get_psid(managed_id).pdf_relative_path
    )

    client_service.delete_client(client_id)

    assert not managed_copy.exists()
    assert source.is_file()
    assert atl_service.list_psids(client_id) == []
    assert archive_root.exists() is False
