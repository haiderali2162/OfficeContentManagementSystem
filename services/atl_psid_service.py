"""Client-scoped ATL surcharge PSID/challan management (v0.2.32).

One client can hold many annual PSIDs. A challan PDF is optional and its
storage choice is saved with the record:

* ``database`` — validated PDF bytes in ``client_atl_psids.pdf_blob``;
* ``managed_file`` — a generated copy under ``data/atl_psids/pdfs``;
* ``linked_path`` — only a link to the user's original file, which is never
  moved or deleted by this application.

The service also performs expiry archiving. Archived metadata and PDF copies
go to the separate database/folder handled by ``atl_psid_archive_service``;
expiry clears only the active PDF and keeps the PSID details visible.
"""

from __future__ import annotations

import os
import subprocess
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import defer

from core.config import DATA_DIR
from core.database import SessionLocal
from core.models import Client, ClientAtlPsid
from services import logging_service
from services.atl_psid_archive_service import archive_psid, purge_archived
from services.settings_service import (
    get_atl_psid_archive_settings,
    record_atl_psid_cleanup,
)


class AtlPsidError(Exception):
    """Expected ATL PSID validation or storage error."""


STORAGE_MODES = ("none", "database", "managed_file", "linked_path")
STORAGE_LABELS = {
    "none": "No PDF",
    "database": "Stored in database",
    "managed_file": "App-managed file",
    "linked_path": "Link to original file",
}
MAX_PDF_BYTES = 25 * 1024 * 1024
MANAGED_PDF_DIR = DATA_DIR / "atl_psids" / "pdfs"
PREVIEW_PDF_DIR = DATA_DIR / "atl_psids" / "preview"

_scheduler_started = False


def _text(value: Any, label: str, *, maximum: int, required: bool = False) -> str:
    cleaned = str(value or "").strip()
    if required and not cleaned:
        raise AtlPsidError(f"{label} is required.")
    if len(cleaned) > maximum:
        raise AtlPsidError(f"{label} must be at most {maximum} characters.")
    return cleaned


def _parse_date(value: Any, label: str) -> date | None:
    if value is None or isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    cleaned = str(value or "").strip()
    if not cleaned:
        return None
    try:
        return date.fromisoformat(cleaned)
    except ValueError as exc:
        raise AtlPsidError(f"{label} must use YYYY-MM-DD format.") from exc


def _parse_amount(value: Any) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise AtlPsidError("ATL amount must be a valid amount.") from exc
    if not amount.is_finite() or amount < 0:
        raise AtlPsidError("ATL amount must be a non-negative amount.")
    try:
        amount = amount.quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise AtlPsidError("ATL amount is too large.") from exc
    if amount > Decimal("999999999999.99"):
        raise AtlPsidError("ATL amount exceeds the supported maximum.")
    return amount


def _parse_mode(value: Any) -> str:
    mode = str(value or "none").strip()
    if mode not in STORAGE_MODES:
        raise AtlPsidError("Choose a supported challan PDF storage option.")
    return mode


def _validate_pdf(data: bytes) -> None:
    if len(data) > MAX_PDF_BYTES:
        raise AtlPsidError(
            f"The challan PDF must be at most {MAX_PDF_BYTES // (1024 * 1024)} MB."
        )
    if not data.startswith(b"%PDF-"):
        raise AtlPsidError("The selected challan file is not a valid PDF.")


def _safe_pdf_name(value: Any) -> str:
    original = Path(str(value or "challan.pdf")).name
    cleaned = "".join(
        char if char.isalnum() or char in "._-" else "_" for char in original
    ).strip("._")
    if not cleaned:
        cleaned = "challan"
    if not cleaned.casefold().endswith(".pdf"):
        cleaned += ".pdf"
    return cleaned[:120]


def _read_source_pdf(
    *, mode: str, pdf_path: Any, pdf_bytes: bytes | None, filename: Any
) -> dict[str, Any] | None:
    """Validate a newly chosen PDF and describe how it should be stored."""
    if mode == "none":
        if pdf_bytes or pdf_path:
            raise AtlPsidError(
                "Choose a storage option before saving a challan PDF."
            )
        return None

    name = _safe_pdf_name(filename or (pdf_path and Path(str(pdf_path)).name))
    size = 0

    if mode == "linked_path":
        if not pdf_path:
            raise AtlPsidError("Choose the original challan PDF to link.")
        source = Path(str(pdf_path)).expanduser().resolve()
        if not source.is_file():
            raise AtlPsidError("The linked challan PDF no longer exists.")
        size = source.stat().st_size
        if size > MAX_PDF_BYTES:
            raise AtlPsidError(
                f"The linked challan PDF must be at most "
                f"{MAX_PDF_BYTES // (1024 * 1024)} MB."
            )
        with source.open("rb") as handle:
            header = handle.read(5)
        if header != b"%PDF-":
            raise AtlPsidError("The linked challan file is not a valid PDF.")
        return {
            "bytes": None,
            "source_path": str(source),
            "filename": source.name,
            "size": size,
        }

    if pdf_bytes is None:
        if not pdf_path:
            raise AtlPsidError("Choose the challan PDF to save.")
        source = Path(str(pdf_path)).expanduser()
        if not source.is_file():
            raise AtlPsidError("The selected challan PDF no longer exists.")
        pdf_bytes = source.read_bytes()
    _validate_pdf(pdf_bytes)
    return {
        "bytes": pdf_bytes,
        "source_path": None,
        "filename": name,
        "size": len(pdf_bytes),
    }


def _managed_path(relative_name: str) -> Path:
    """Resolve a stored managed filename only inside the managed folder."""
    candidate = Path(relative_name)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise AtlPsidError("A saved challan path is outside the app folder.")
    root = MANAGED_PDF_DIR.resolve()
    resolved = (root / candidate).resolve()
    if root != resolved and root not in resolved.parents:
        raise AtlPsidError("A saved challan path is outside the app folder.")
    return resolved


def _write_managed_pdf(
    *, client_id: int, psid_id: int, data: bytes, filename: str
) -> str:
    MANAGED_PDF_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    candidate_name = f"client_{client_id}_psid_{psid_id}_{stamp}_{filename}"
    candidate = MANAGED_PDF_DIR / candidate_name
    counter = 2
    while candidate.exists():
        candidate = MANAGED_PDF_DIR / f"{candidate_name.rsplit('.', 1)[0]}_{counter}.pdf"
        counter += 1
    with candidate.open("xb") as target:
        target.write(data)
    return candidate.name


def _remove_managed_pdf(relative_name: str | None) -> None:
    if not relative_name:
        return
    try:
        path = _managed_path(relative_name)
    except AtlPsidError:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise AtlPsidError(f"Could not remove the saved challan copy: {exc}") from exc


def _resolve_active_pdf(record: ClientAtlPsid) -> dict[str, Any]:
    """Return the active PDF bytes/path for open, download, or archive."""
    mode = record.storage_mode
    if mode == "none":
        raise AtlPsidError("No challan PDF is attached to this PSID.")
    if mode == "database":
        if not record.pdf_blob:
            raise AtlPsidError("The stored challan PDF is missing from this record.")
        return {
            "bytes": bytes(record.pdf_blob),
            "path": None,
            "filename": _safe_pdf_name(record.original_filename),
        }
    if mode == "managed_file":
        if not record.pdf_relative_path:
            raise AtlPsidError("The app-managed challan PDF is not recorded.")
        path = _managed_path(record.pdf_relative_path)
        if not path.is_file():
            raise AtlPsidError("The app-managed challan PDF is missing.")
        data = path.read_bytes()
        _validate_pdf(data)
        return {
            "bytes": data,
            "path": path,
            "filename": _safe_pdf_name(record.original_filename or path.name),
        }
    if not record.pdf_source_path:
        raise AtlPsidError("The linked challan PDF is not recorded.")
    source = Path(record.pdf_source_path).expanduser().resolve()
    if not source.is_file():
        raise AtlPsidError("The linked challan PDF is missing at its original path.")
    if source.stat().st_size > MAX_PDF_BYTES:
        raise AtlPsidError(
            f"The linked challan PDF exceeds the {MAX_PDF_BYTES // (1024 * 1024)} MB limit."
        )
    data = source.read_bytes()
    _validate_pdf(data)
    return {"bytes": data, "path": source, "filename": source.name}
    raise AtlPsidError("Choose a supported challan PDF storage option.")


def _reveal_file(path: Path) -> Path:
    """Open a local PDF with the operating system's associated handler."""
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif os.name == "posix" and hasattr(os, "uname") and os.uname().sysname == "Darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError as exc:
        raise AtlPsidError(f"Could not open the challan PDF: {exc}") from exc
    return path


def list_psids(client_id: int) -> list[ClientAtlPsid]:
    """Return one client's PSIDs, newest financial year first (without PDFs)."""
    with SessionLocal() as session:
        statement = (
            select(ClientAtlPsid)
            .options(defer(ClientAtlPsid.pdf_blob))
            .where(ClientAtlPsid.client_id == client_id)
            .order_by(ClientAtlPsid.financial_year.desc(), ClientAtlPsid.id.desc())
        )
        return list(session.scalars(statement).all())


def get_psid(psid_id: int) -> ClientAtlPsid | None:
    """Return one PSID without loading its PDF bytes."""
    with SessionLocal() as session:
        return session.scalar(
            select(ClientAtlPsid)
            .options(defer(ClientAtlPsid.pdf_blob))
            .where(ClientAtlPsid.id == psid_id)
        )


def save_psid(
    *,
    client_id: int,
    financial_year: Any,
    psid_number: Any,
    amount: Any = None,
    payment_date: Any = None,
    notes: Any = "",
    expiry_date: Any = None,
    storage_mode: Any = "none",
    pdf_path: Any = None,
    pdf_bytes: bytes | None = None,
    original_filename: Any = None,
    clear_pdf: bool = False,
    psid_id: int | None = None,
) -> int:
    """Create or update one client PSID and its optional challan PDF."""
    year = _text(financial_year, "Financial year", maximum=20, required=True)
    psid = _text(psid_number, "PSID number", maximum=100, required=True)
    money = _parse_amount(amount)
    paid_on = _parse_date(payment_date, "Payment/challan date")
    expires_on = _parse_date(expiry_date, "Expiry date")
    note_text = _text(notes, "Notes", maximum=2000)
    mode = _parse_mode(storage_mode)

    old_managed_to_remove: str | None = None
    new_managed_created: str | None = None
    saved_id = 0

    try:
        with SessionLocal.begin() as session:
            if session.get(Client, client_id) is None:
                raise AtlPsidError("Client record not found.")
            record = session.get(ClientAtlPsid, psid_id) if psid_id else None
            if psid_id is not None and (
                record is None or record.client_id != client_id
            ):
                raise AtlPsidError(
                    "This PSID does not belong to the selected client."
                )

            incoming_pdf = None
            if pdf_bytes is not None or pdf_path:
                incoming_pdf = _read_source_pdf(
                    mode=mode,
                    pdf_path=pdf_path,
                    pdf_bytes=pdf_bytes,
                    filename=original_filename,
                )
            elif clear_pdf:
                mode = "none"

            old_managed = (
                record.pdf_relative_path
                if record is not None
                and record.storage_mode == "managed_file"
                else None
            )

            if record is None:
                # Required fields must be present before the first flush: the
                # table has NOT NULL columns and managed-file naming needs id.
                record = ClientAtlPsid(
                    client_id=client_id,
                    financial_year=year,
                    psid_number=psid,
                    amount=money,
                    payment_date=paid_on,
                    notes=note_text or None,
                    expiry_date=expires_on,
                )
                session.add(record)
            else:
                record.financial_year = year
                record.psid_number = psid
                record.amount = money
                record.payment_date = paid_on
                record.notes = note_text or None
                record.expiry_date = expires_on
            session.flush()

            if incoming_pdf is not None:
                # Keep the old file until the database commit succeeds.
                old_managed_to_remove = old_managed
                record.storage_mode = mode
                record.original_filename = incoming_pdf["filename"]
                record.pdf_size_bytes = int(incoming_pdf["size"])
                record.pdf_blob = None
                record.pdf_relative_path = None
                record.pdf_source_path = None
                if mode == "database":
                    record.pdf_blob = incoming_pdf["bytes"]
                elif mode == "managed_file":
                    new_managed_created = _write_managed_pdf(
                        client_id=client_id,
                        psid_id=record.id,
                        data=incoming_pdf["bytes"],
                        filename=incoming_pdf["filename"],
                    )
                    record.pdf_relative_path = new_managed_created
                else:
                    record.pdf_source_path = incoming_pdf["source_path"]
            elif clear_pdf:
                old_managed_to_remove = old_managed
                record.storage_mode = "none"
                record.original_filename = None
                record.pdf_blob = None
                record.pdf_relative_path = None
                record.pdf_source_path = None
                record.pdf_size_bytes = None
            elif record.id and psid_id is not None:
                # Editing only the text fields preserves the existing choice.
                record.storage_mode = record.storage_mode or "none"

            saved_id = int(record.id)
    except Exception:
        if new_managed_created:
            _remove_managed_pdf(new_managed_created)
        raise

    if old_managed_to_remove:
        _remove_managed_pdf(old_managed_to_remove)
    return saved_id


def delete_psid(psid_id: int) -> None:
    """Permanently delete one active PSID; linked originals are untouched."""
    managed_path: str | None = None
    with SessionLocal.begin() as session:
        record = session.get(ClientAtlPsid, psid_id)
        if record is None:
            raise AtlPsidError("This PSID no longer exists.")
        if record.storage_mode == "managed_file":
            managed_path = record.pdf_relative_path
        session.delete(record)

    if managed_path:
        _remove_managed_pdf(managed_path)


def managed_psid_paths_for_client(client_id: int) -> list[str]:
    """Return app-managed PSID copies that belong to one client."""
    with SessionLocal() as session:
        rows = session.execute(
            select(ClientAtlPsid.pdf_relative_path).where(
                ClientAtlPsid.client_id == client_id,
                ClientAtlPsid.storage_mode == "managed_file",
            )
        ).all()
    return [row[0] for row in rows if row[0]]


def remove_managed_psid_paths(paths: list[str]) -> None:
    """Remove app-managed copies after their database rows are gone.

    Archive rows and linked original files are never touched.
    """
    for relative_name in paths:
        _remove_managed_pdf(relative_name)


def open_psid_pdf(psid_id: int) -> Path:
    """Open the active challan PDF in the operating system's PDF viewer."""
    with SessionLocal() as session:
        record = session.get(ClientAtlPsid, psid_id)
        if record is None:
            raise AtlPsidError("This PSID no longer exists.")
        pdf = _resolve_active_pdf(record)
        if pdf["path"] is not None:
            return _reveal_file(Path(pdf["path"]))

    PREVIEW_PDF_DIR.mkdir(parents=True, exist_ok=True)
    preview = PREVIEW_PDF_DIR / f"psid_{psid_id}.pdf"
    preview.write_bytes(pdf["bytes"])
    return _reveal_file(preview)


def save_psid_pdf(psid_id: int, destination: Any) -> Path:
    """Write one active challan PDF to a user-chosen destination."""
    target = Path(str(destination or "")).expanduser()
    if not str(target).strip():
        raise AtlPsidError("Choose where to save the challan PDF.")
    with SessionLocal() as session:
        record = session.get(ClientAtlPsid, psid_id)
        if record is None:
            raise AtlPsidError("This PSID no longer exists.")
        pdf = _resolve_active_pdf(record)
    if target.is_dir():
        target = target / pdf["filename"]
    if target.suffix.casefold() != ".pdf":
        target = target.with_suffix(".pdf")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(pdf["bytes"])
    return target


def _due_reason(
    record: ClientAtlPsid, settings: dict[str, Any], today: date
) -> str:
    """Return why a record is due, or an empty string when it is not."""
    if record.expiry_date is not None:
        return "the saved expiry date" if record.expiry_date <= today else ""
    default_days = int(settings.get("default_expiry_days") or 0)
    if default_days > 0:
        created = record.created_at.date() if record.created_at else today
        if created + timedelta(days=default_days) <= today:
            return "the default expiry age"
    return ""


def run_cleanup(*, force: bool = False) -> dict[str, Any]:
    """Archive due PSID PDFs, then purge archive rows past their retention.

    ``force=True`` is used by the Settings button; automatic calls respect
    the saved enable switch. The function is idempotent: processed records
    are skipped and the archive service rejects duplicate source IDs.
    """
    settings = get_atl_psid_archive_settings()
    if not force and not settings["enabled"]:
        return {"status": "disabled", "archived": 0, "warnings": []}

    now = datetime.now(timezone.utc)
    today = date.today()
    archived = 0
    warnings: list[str] = []
    managed_paths_to_remove: list[str] = []

    with SessionLocal.begin() as session:
        records = session.scalars(
            select(ClientAtlPsid)
            .options(defer(ClientAtlPsid.pdf_blob))
            .where(ClientAtlPsid.expiry_processed_at.is_(None))
        ).all()
        for record in records:
            reason = _due_reason(record, settings, today)
            if not reason:
                continue
            client_name = session.scalar(
                select(Client.name).where(Client.id == record.client_id)
            )
            pdf_bytes: bytes | None = None
            attached = bool(record.storage_mode != "none")
            if attached:
                try:
                    pdf_bytes = _resolve_active_pdf(record)["bytes"]
                except AtlPsidError as exc:
                    warnings.append(f"PSID {record.psid_number}: {exc}")
                    continue

            try:
                result = archive_psid(
                    metadata={
                        "source_id": record.id,
                        "client_id": record.client_id,
                        "client_name": client_name or "",
                        "financial_year": record.financial_year,
                        "psid_number": record.psid_number,
                        "amount": record.amount,
                        "payment_date": record.payment_date,
                        "notes": record.notes or "",
                        "storage_mode": record.storage_mode,
                        "original_filename": record.original_filename or "",
                        "expiry_date": record.expiry_date,
                        "retention_days": int(settings["retention_days"]),
                    },
                    pdf_bytes=pdf_bytes,
                )
            except Exception as exc:
                warnings.append(f"PSID {record.psid_number}: {exc}")
                continue

            if record.storage_mode == "managed_file" and record.pdf_relative_path:
                managed_paths_to_remove.append(record.pdf_relative_path)
            record.storage_mode = "none"
            record.original_filename = None
            record.pdf_blob = None
            record.pdf_relative_path = None
            record.pdf_source_path = None
            record.pdf_size_bytes = None
            record.expiry_processed_at = now
            archived += 1
            for message in result.get("warnings", []):
                warnings.append(f"PSID {record.psid_number}: {message}")

    # The archive copy already exists; remove active managed files only after
    # the live database has committed the cleared PDF fields.
    for relative_name in managed_paths_to_remove:
        try:
            _remove_managed_pdf(relative_name)
        except AtlPsidError as exc:
            warnings.append(str(exc))

    purge = purge_archived(int(settings["retention_days"]))
    return {
        "status": "completed",
        "archived": archived,
        "purged_rows": purge["rows_deleted"],
        "purged_pdfs": purge["pdfs_deleted"],
        "skipped": purge["skipped"],
        "warnings": warnings,
    }


def _parse_last_cleanup(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def run_cleanup_if_due() -> dict[str, Any] | None:
    """Run the daily cleanup when enabled and the last run is over a day old."""
    settings = get_atl_psid_archive_settings()
    if not settings["enabled"]:
        return None
    now = datetime.now(timezone.utc)
    last = _parse_last_cleanup(settings.get("last_cleanup_at"))
    if last is not None and now - last < timedelta(hours=24):
        return None
    try:
        result = run_cleanup(force=True)
    except Exception as exc:  # scheduler must survive failures
        logging_service.log_error("ATL PSID cleanup failed", exc)
        result = {"status": "error", "error": str(exc)}
    record_atl_psid_cleanup(now.isoformat())
    return result


def start_atl_psid_cleanup_scheduler() -> None:
    """Start the idempotent once-per-minute due-check daemon thread."""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    def loop() -> None:
        while True:
            try:
                run_cleanup_if_due()
            except Exception:
                # run_cleanup_if_due already logs its own expected failures.
                pass
            threading.Event().wait(60)

    thread = threading.Thread(
        target=loop, name="atl-psid-cleanup-scheduler", daemon=True
    )
    thread.start()
