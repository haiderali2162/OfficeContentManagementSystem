"""Separate archive database and folder for expired ATL challan PDFs.

The archive lives outside the live application database so creating or
cleaning it can never alter existing client records. Only the active
``client_atl_psids`` table is added to the live database by ``create_all()``;
the table in this module uses its own SQLAlchemy ``MetaData`` and is created
only inside ``data/atl_archive/atl_archive.sqlite3``.

Retention is snapshot-based: each archived row stores the retention period
that applied when it was archived. Later Settings changes do not rewrite the
purge date of an older archive entry.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    create_engine,
    delete,
    func,
    select,
)
from sqlalchemy.engine import Engine

from core.config import DATA_DIR
from services.settings_service import SettingsError, reveal_directory


class AtlArchiveError(Exception):
    """Expected archive storage or retention error."""


ARCHIVE_DIR = DATA_DIR / "atl_archive"
ARCHIVE_PDF_DIR = ARCHIVE_DIR / "pdfs"
ARCHIVE_DB_PATH = ARCHIVE_DIR / "atl_archive.sqlite3"

ARCHIVE_METADATA = MetaData()
ARCHIVED_PSIDS = Table(
    "archived_atl_psids",
    ARCHIVE_METADATA,
    Column("id", Integer, primary_key=True),
    # One active record is archived once, even if the daily check runs twice.
    Column("source_id", Integer, nullable=False, unique=True, index=True),
    Column("client_id", Integer, nullable=False, index=True),
    Column("client_name", String(200), default=""),
    Column("financial_year", String(20), nullable=False),
    Column("psid_number", String(100), nullable=False),
    Column("amount", Numeric(14, 2)),
    Column("payment_date", Date),
    Column("notes", Text),
    Column("source_storage_mode", String(20), nullable=False),
    Column("original_filename", String(255)),
    # Empty for a PSID that never had a challan PDF.
    Column("pdf_relative_path", String(500), default="", nullable=False),
    Column("pdf_size_bytes", Integer),
    Column("expiry_date", Date),
    # Retention is copied here so later setting edits cannot shorten an
    # already archived item retroactively.
    Column("retention_days", Integer, default=0, nullable=False),
    Column("archived_at", DateTime(timezone=True), nullable=False),
    Index("ix_archived_atl_psids_archived_at", "archived_at"),
)

_archive_engine: Engine | None = None


def reset_archive_engine() -> None:
    """Dispose the cached archive engine (used by tests and path changes)."""
    global _archive_engine
    if _archive_engine is not None:
        _archive_engine.dispose()
        _archive_engine = None


def ensure_archive() -> Engine:
    """Create the archive folders/database if missing and return its engine."""
    global _archive_engine
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_PDF_DIR.mkdir(parents=True, exist_ok=True)
    if _archive_engine is None:
        _archive_engine = create_engine(
            f"sqlite:///{Path(ARCHIVE_DB_PATH).as_posix()}"
        )
    ARCHIVE_METADATA.create_all(_archive_engine)
    return _archive_engine


def open_archive_folder() -> Path:
    """Create and open the app-managed archive folder in the file manager."""
    ensure_archive()
    try:
        return reveal_directory(str(ARCHIVE_DIR))
    except SettingsError as exc:
        raise AtlArchiveError(str(exc)) from exc


def _safe_filename(value: Any, source_id: int) -> str:
    """Build a collision-resistant, path-safe PDF filename."""
    original = Path(str(value or "challan.pdf")).name
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", original).strip("._")
    if not cleaned:
        cleaned = "challan"
    if not cleaned.casefold().endswith(".pdf"):
        cleaned += ".pdf"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{source_id}_{stamp}_{cleaned[:120]}"


def archive_psid(
    *, metadata: dict[str, Any], pdf_bytes: bytes | None = None
) -> dict[str, Any]:
    """Copy one expired PSID's metadata and PDF into the archive.

    Returns a status dict. ``already_archived`` makes repeated cleanup runs
    harmless. A missing PDF is reported as a warning but the PSID details are
    still archived, because the user's payment reference must not be lost.
    """
    engine = ensure_archive()
    source_id = int(metadata["source_id"])
    now = datetime.now(timezone.utc)
    warnings: list[str] = []

    with engine.begin() as connection:
        existing = connection.execute(
            select(ARCHIVED_PSIDS.c.id).where(
                ARCHIVED_PSIDS.c.source_id == source_id
            )
        ).first()
        if existing is not None:
            return {
                "status": "already_archived",
                "archive_id": int(existing[0]),
                "pdf_relative_path": "",
                "warnings": warnings,
            }

        pdf_relative_path = ""
        if pdf_bytes:
            if not pdf_bytes.startswith(b"%PDF-"):
                raise AtlArchiveError(
                    "The challan file is not a valid PDF and was not archived."
                )
            filename = _safe_filename(
                metadata.get("original_filename"), source_id
            )
            candidate = ARCHIVE_PDF_DIR / filename
            counter = 2
            while candidate.exists():
                candidate = ARCHIVE_PDF_DIR / f"{candidate.stem}_{counter}.pdf"
                counter += 1
            with candidate.open("xb") as target:
                target.write(pdf_bytes)
            pdf_relative_path = candidate.name
        else:
            warnings.append("No challan PDF was attached to this PSID.")

        inserted = connection.execute(
            ARCHIVED_PSIDS.insert().values(
                source_id=source_id,
                client_id=int(metadata["client_id"]),
                client_name=str(metadata.get("client_name") or ""),
                financial_year=str(metadata["financial_year"]),
                psid_number=str(metadata["psid_number"]),
                amount=metadata.get("amount"),
                payment_date=metadata.get("payment_date"),
                notes=metadata.get("notes") or "",
                source_storage_mode=str(metadata.get("storage_mode") or "none"),
                original_filename=str(metadata.get("original_filename") or ""),
                pdf_relative_path=pdf_relative_path,
                pdf_size_bytes=len(pdf_bytes) if pdf_bytes else None,
                expiry_date=metadata.get("expiry_date"),
                retention_days=int(metadata.get("retention_days") or 0),
                archived_at=now,
            )
        )

    return {
        "status": "archived",
        "archive_id": int(inserted.inserted_primary_key[0]),
        "pdf_relative_path": pdf_relative_path,
        "warnings": warnings,
    }


def _resolve_archive_pdf(relative_path: str) -> Path:
    """Resolve an archive PDF only inside the archive PDF directory."""
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise AtlArchiveError("An archive PDF path is outside the archive folder.")
    root = ARCHIVE_PDF_DIR.resolve()
    resolved = (root / candidate).resolve()
    if root != resolved and root not in resolved.parents:
        raise AtlArchiveError("An archive PDF path is outside the archive folder.")
    return resolved


def purge_archived(retention_days: int) -> dict[str, int]:
    """Permanently delete archived rows older than the configured retention.

    ``retention_days <= 0`` means keep forever, so an upgraded database never
    deletes archived work until the user explicitly selects a retention.
    """
    engine = ensure_archive()
    if retention_days <= 0:
        return {"rows_deleted": 0, "pdfs_deleted": 0, "skipped": 0}

    cutoff = datetime.now(timezone.utc) - timedelta(days=int(retention_days))
    rows_deleted = 0
    pdfs_deleted = 0
    skipped = 0
    with engine.begin() as connection:
        rows = connection.execute(
            select(
                ARCHIVED_PSIDS.c.id,
                ARCHIVED_PSIDS.c.pdf_relative_path,
                ARCHIVED_PSIDS.c.retention_days,
            ).where(ARCHIVED_PSIDS.c.archived_at < cutoff)
        ).all()
        for row in rows:
            # Respect the retention snapshot saved with each archived item.
            if int(row.retention_days or 0) <= 0:
                skipped += 1
                continue
            relative = str(row.pdf_relative_path or "")
            if relative:
                try:
                    path = _resolve_archive_pdf(relative)
                    if path.exists():
                        path.unlink()
                        pdfs_deleted += 1
                except (AtlArchiveError, OSError):
                    # Keep the archive row so the user can retry or investigate.
                    skipped += 1
                    continue
            connection.execute(
                delete(ARCHIVED_PSIDS).where(ARCHIVED_PSIDS.c.id == row.id)
            )
            rows_deleted += 1
    return {
        "rows_deleted": rows_deleted,
        "pdfs_deleted": pdfs_deleted,
        "skipped": skipped,
    }


def archive_summary() -> dict[str, int]:
    """Return the number of archived PSID rows for Settings status text."""
    engine = ensure_archive()
    with engine.connect() as connection:
        total = connection.execute(
            select(func.count()).select_from(ARCHIVED_PSIDS)
        ).scalar_one()
    return {"rows": int(total)}


def reveal_archived_pdf(relative_path: str) -> Path:
    """Open one archived PDF with the operating system's PDF handler."""
    path = _resolve_archive_pdf(relative_path)
    if not path.is_file():
        raise AtlArchiveError("The archived challan PDF is missing.")
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif os.name == "posix" and hasattr(os, "uname") and os.uname().sysname == "Darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError as exc:
        raise AtlArchiveError(f"Could not open the archived PDF: {exc}") from exc
    return path
