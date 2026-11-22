"""Client import and saved-data column tools (FBR Clients page).

Developer-request batch (2026-11-20):

* import client information from spreadsheets (.xlsx, .xls, .ods, .csv)
  or pasted portal/sheet tables (a Google Sheet exported or copied in any
  of those shapes), with automatic column mapping and manual override;
* fix mistakes where data was typed into the wrong columns: swap the
  values between two or three columns for saved client records (with a
  preview before anything is written), and swap two fields' values
  inside one import row.

Row creation reuses :func:`services.client_service.create_client`, so
imported rows pass exactly the same validation as manually entered ones
(name, CNIC/NTN, phone, e-mail, amounts) and their default password is
encrypted the same way. Client-defined custom fields are not part of the
spreadsheet import: their definitions are user-created at runtime and
there is no stable column identity to map to (disclosed limitation).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from sqlalchemy import select

from core.database import SessionLocal
from core.models import Client
from services import client_service
from services.client_service import ClientServiceError

# Mappable client fields (field -> spreadsheet label used in the mapping UI).
CLIENT_FIELD_LABELS = {
    "name": "Client name (required)",
    "father_name": "Father / husband name",
    "cnic": "CNIC (00000-0000000-0)",
    "ntn_number": "NTN number",
    "cnic_expiry_date": "CNIC expiry date (YYYY-MM-DD)",
    "phone_number": "Phone number",
    "email": "Email",
    "address": "Address",
    "tehsil": "Tehsil",
    "district": "District",
    "city": "City",
    "province": "Province",
    "payment_amount": "Payment amount",
    "amount_received": "Amount received",
    "current_status": "Status (Active/Inactive/Pending)",
    "referred_by": "Referred by (required)",
    "password": "Password (stored encrypted)",
}

# Header keywords for auto-mapping, in field order of importance.
_CLIENT_FIELD_KEYWORDS = {
    "name": ("client name", "name of client", "applicant", "name"),
    "father_name": ("father", "husband", "f/name", "parent"),
    "cnic": ("cnic", "identity card", "nic no"),
    "ntn_number": ("ntn", "tax number"),
    "cnic_expiry_date": ("expiry", "expire"),
    "phone_number": ("phone", "mobile", "contact", "cell"),
    "email": ("email", "e-mail", "mail"),
    "address": ("address", "street"),
    "tehsil": ("tehsil", "tehseel"),
    "district": ("district", "zila"),
    "city": ("city", "town"),
    "province": ("province", "state"),
    "payment_amount": ("payment", "fee", "amount total", "total amount"),
    "amount_received": ("received", "paid", "advance"),
    "current_status": ("status",),
    "referred_by": ("referr", "reference", "agent"),
    "password": ("password", "pass word", "pwd"),
}

# Text-only columns that may exchange their stored values safely.
SWAPPABLE_CLIENT_FIELDS = {
    "name": "Client name",
    "father_name": "Father / husband name",
    "cnic": "CNIC",
    "ntn_number": "NTN number",
    "phone_number": "Phone number",
    "email": "Email",
    "address": "Address",
    "tehsil": "Tehsil",
    "district": "District",
    "city": "City",
    "province": "Province",
    "current_status": "Status",
    "referred_by": "Referred by",
}

# Row cap for one import run (per-row preview and creation stay responsive).
IMPORT_ROW_CAP = 2000

# Fields the user can mark mandatory or optional in the import dialog.
# The import default is lenient: everything starts optional so rows with
# empty cells import anyway (empty stays empty in the database); ticking
# a field enforces it. The father/husband name can be generated from the
# name split when it is optional and a parentage is given.
REQUIREMENT_DEFAULTS = {
    "cnic": False,
    "ntn_number": False,
    "phone_number": False,
    "email": False,
    "referred_by": False,
    "father_name": False,
    "tehsil": False,
    "district": False,
    "city": False,
    "province": False,
}

# Recognizable placeholder for rows whose name cell is empty.
UNNAMED_PLACEHOLDER = "(unnamed"

# Date cell formats seen in office spreadsheets, most common first:
# DD-MM-YYYY, DD.MM.YYYY, DD/MM/YYYY, DD-MM-YY, and ISO YYYY-MM-DD.
_DATE_SEPARATORS = re.compile(r"[-./\s]+")


def normalise_date_cell(
    raw: Any, label: str,
) -> tuple[str, str]:
    """Normalize a spreadsheet date cell to ISO ``YYYY-MM-DD``.

    Returns ``(iso_value, error_message)`` — an empty ISO value with an
    error means the cell could not be read. Day-first ordering is
    preferred (DD-MM-YYYY is the office convention); ISO dates and
    two-digit years are also accepted.
    """
    text = _clean(raw)
    if not text:
        return "", ""
    # Excel date cells sometimes arrive as datetimes
    # ("2026-01-01 00:00:00") — keep the date part.
    iso_match = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
    if iso_match:
        try:
            return date.fromisoformat(iso_match.group(1)).isoformat(), ""
        except ValueError:
            pass
    parts = [
        part for part in _DATE_SEPARATORS.split(text) if part
    ]
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return "", f"{label} '{text}' is not a recognized date (use DD-MM-YYYY)."
    try:
        if len(parts[0]) == 4:
            year, month, day = (int(part) for part in parts)
        else:
            day, month = int(parts[0]), int(parts[1])
            year = int(parts[2])
            if year < 100:
                year += 2000 if year <= 79 else 1900
        parsed = date(year, month, day)
    except ValueError:
        return "", f"{label} '{text}' is not a valid date."
    return parsed.isoformat(), ""

# Export columns (never includes passwords or PINs).
EXPORT_FIELD_ORDER = (
    "name", "father_name", "cnic", "ntn_number", "cnic_expiry_date",
    "phone_number", "email", "address", "tehsil", "district", "city",
    "province", "payment_amount", "amount_received", "current_status",
    "referred_by",
)


class ClientImportError(ValueError):
    """Raised when a client import cannot be parsed or applied."""


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


def read_client_table(
    *,
    pasted_text: str = "",
    file_bytes: bytes | None = None,
    filename: str = "",
) -> tuple[list[str], list[list[str]]]:
    """Return (headers, rows) from a paste or a spreadsheet file.

    Paste accepts tab-separated (Google Sheets / Excel copy) or
    comma-separated text. Files accept .xlsx, .xls, .ods, and .csv through
    the same workbook reader the DC/FBR rate importer uses.
    """
    if file_bytes:
        from services.dc_rate_service import read_schedule_sheets

        sheets = read_schedule_sheets(file_bytes, filename)
        if not sheets:
            raise ClientImportError("The selected file has no readable sheet.")
        headers = [str(header) for header in sheets[0]["headers"]]
        rows = [
            [_clean(cell) for cell in row] for row in sheets[0]["rows"]
        ]
    else:
        from services.naqsha_service import parse_pasted_table

        headers, rows = parse_pasted_table(pasted_text)
        rows = [[_clean(cell) for cell in row] for row in rows]
    rows = [row for row in rows if any(cell.strip() for cell in row)]
    if not rows:
        raise ClientImportError("No data rows found under the header row.")
    if len(rows) > IMPORT_ROW_CAP:
        raise ClientImportError(
            f"The file holds {len(rows)} data rows; import at most "
            f"{IMPORT_ROW_CAP} rows per run."
        )
    return headers, rows


def suggest_client_column_mapping(headers: list[str]) -> dict[str, int | None]:
    """Auto-map spreadsheet columns to client fields; extras stay ignored."""
    normalized = [
        re.sub(r"\s+", " ", str(header)).strip().casefold()
        for header in headers
    ]
    mapping: dict[str, int | None] = {field: None for field in CLIENT_FIELD_LABELS}
    used: set[int] = set()
    for field, keywords in _CLIENT_FIELD_KEYWORDS.items():
        for keyword in keywords:
            for index, header in enumerate(normalized):
                if keyword in header and index not in used:
                    mapping[field] = index
                    used.add(index)
                    break
            if mapping[field] is not None:
                break
    return mapping


def _cell(row: list[Any], index: int | None) -> str:
    if index is None or index < 0 or index >= len(row):
        return ""
    return _clean(row[index])


def existing_identifiers() -> tuple[set[str], set[str]]:
    """CNIC and NTN numbers already saved (for duplicate reporting)."""
    with SessionLocal() as session:
        cnics = set(
            session.scalars(
                select(Client.cnic).where(Client.cnic.is_not(None))
            ).all()
        )
        ntns = set(
            session.scalars(
                select(Client.ntn_number).where(Client.ntn_number.is_not(None))
            ).all()
        )
    return {str(v).strip() for v in cnics}, {str(v).strip() for v in ntns}


def build_client_import_preview(
    headers: list[str],
    rows: list[list[str]],
    mapping: dict[str, int | None],
    *,
    default_password: str = "",
    required: dict[str, bool] | None = None,
    split_names: bool = False,
) -> dict[str, Any]:
    """Validate every mapped row against the real client rules.

    Passwords are optional per row: a mapped Password column wins; rows
    without one fall back to ``default_password``; a row with neither is
    reported as a problem instead of being created without credentials.
    ``required`` carries the user's mandatory/optional choices (fields
    unchecked there may stay empty); ``split_names`` enables the
    so/do/wo/wid parentage split inside the name cell.

    Returns ``{"rows": [...], "problems": [...], "ready": n}`` where each
    row dict carries the parsed field values, its source row number, and a
    ``problem`` text when the row cannot be imported as-is. Duplicate
    CNIC/NTN values (inside the file or already saved) are reported but
    keep the row importable so the user can decide.
    """
    known_cnic, known_ntn = existing_identifiers()
    if required is None:
        # The import's own default posture: lenient (everything optional)
        # so rows with empty cells import unless the user ticks a field.
        required = dict(REQUIREMENT_DEFAULTS)
    seen_cnic: dict[str, int] = {}
    seen_ntn: dict[str, int] = {}
    preview_rows: list[dict[str, Any]] = []
    for position, row in enumerate(rows, start=2):
        values = {
            field: _cell(row, mapping.get(field)) for field in CLIENT_FIELD_LABELS
        }
        values = apply_name_split(values, enabled=split_names)
        if not values["name"].strip():
            # A recognizable placeholder keeps the row importable when the
            # name cell is empty (developer request 2026-11-21).
            values["name"] = f"{UNNAMED_PLACEHOLDER} — row {position})"
        # Spreadsheet dates arrive as DD-MM-YYYY, DD.MM.YYYY, DD/MM/YYYY,
        # DD-MM-YY, or ISO — normalize to ISO before validation/storage
        # (developer request 2026-11-21).
        expiry_iso, expiry_error = normalise_date_cell(
            values["cnic_expiry_date"], "CNIC expiry date",
        )
        if expiry_error:
            values["cnic_expiry_date"] = ""
        else:
            values["cnic_expiry_date"] = expiry_iso
        problem = expiry_error
        try:
            validated = client_service._validate_client_data(
                name=values["name"],
                cnic=values["cnic"],
                ntn_number=values["ntn_number"],
                cnic_expiry_date=values["cnic_expiry_date"],
                phone_number=values["phone_number"],
                email=values["email"],
                referred_by=values["referred_by"],
                payment_amount=values["payment_amount"] or None,
                amount_received=values["amount_received"] or None,
                required=required,
                father_name=values["father_name"],
                tehsil=values["tehsil"],
                district=values["district"],
                city=values["city"],
                province=values["province"],
            )
            # Show exactly what will be stored (CNIC dash normalization).
            values["cnic"] = validated[1] or ""
            values["ntn_number"] = validated[2] or ""
        except ClientServiceError as exc:
            problem = str(exc)
        cnic = values["cnic"].strip()
        ntn = values["ntn_number"].strip()
        notes: list[str] = []
        if cnic:
            if cnic in known_cnic:
                notes.append(f"CNIC {cnic} is already saved in the database")
            if cnic in seen_cnic:
                notes.append(
                    f"CNIC {cnic} repeats row {seen_cnic[cnic]}"
                )
            else:
                seen_cnic[cnic] = position
        if ntn:
            if ntn in known_ntn:
                notes.append(f"NTN {ntn} is already saved in the database")
            if ntn in seen_ntn:
                notes.append(
                    f"NTN {ntn} repeats row {seen_ntn[ntn]}"
                )
            else:
                seen_ntn[ntn] = position
        if notes:
            problem = problem or "; ".join(notes)
        if not values["password"].strip() and not str(default_password or "").strip():
            # Informational only — the client imports with no credentials
            # stored (the password column stays empty/NULL, never text).
            notes.append("imports without a password (no credentials stored)")
        preview_rows.append({
            "row_number": position,
            "values": values,
            "notes": notes,
            "problem": problem,
        })
    ready = sum(1 for row in preview_rows if not row["problem"])
    return {
        "rows": preview_rows,
        "problems": [
            f"Row {row['row_number']}: {row['problem']}"
            for row in preview_rows if row["problem"]
        ],
        "ready": ready,
    }


def swap_import_row_fields(
    values: dict[str, str], field_names: list[str],
) -> dict[str, str]:
    """Swap/rotate the values of the chosen fields inside one row.

    Values flow first -> second -> ... -> last -> first, so two fields
    exchange and any number of fields rotates. The name and password
    columns can never be swapped.
    """
    fields = [str(name) for name in field_names if str(name) in CLIENT_FIELD_LABELS]
    if len(fields) < 2:
        raise ClientImportError("Pick two or more fields to swap.")
    if len(set(fields)) != len(fields):
        raise ClientImportError("Pick different fields to swap.")
    if "name" in fields:
        raise ClientImportError("The client name column cannot be swapped.")
    if "password" in fields:
        raise ClientImportError("The password column cannot be swapped.")
    rotated = [values[fields[-1]]] + [values[field] for field in fields[:-1]]
    return {**values, **dict(zip(fields, rotated))}


def import_clients(
    rows: list[dict[str, Any]],
    *,
    default_password: str = "",
    required: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Create client records with the standard validation.

    Each row uses its own mapped password when present, otherwise
    ``default_password``; rows with neither import **without credentials**
    (the password column stays empty/NULL — never plain text). Rows with
    ``problem`` texts are skipped unless the caller clears the problem
    first (the UI shows every problem before importing). Returns
    ``{"created", "skipped", "errors": [...]}``.
    """
    created = 0
    skipped = 0
    errors: list[str] = []
    if required is None:
        required = dict(REQUIREMENT_DEFAULTS)
    for row in rows:
        label = f"Row {row.get('row_number', '?')}"
        if row.get("problem"):
            skipped += 1
            errors.append(f"{label}: not imported — {row['problem']}")
            continue
        values = row["values"]
        password = str(values.get("password") or "").strip() or str(
            default_password or ""
        ).strip()
        try:
            client_service.create_client(
                name=values["name"],
                father_name=values["father_name"],
                cnic=values["cnic"],
                ntn_number=values["ntn_number"],
                cnic_expiry_date=values["cnic_expiry_date"],
                phone_number=values["phone_number"],
                email=values["email"],
                address=values["address"],
                tehsil=values["tehsil"],
                district=values["district"],
                city=values["city"],
                province=values["province"],
                payment_amount=values["payment_amount"] or None,
                amount_received=values["amount_received"] or None,
                current_status=values["current_status"],
                referred_by=values["referred_by"],
                password=password,
                field_requirements=required,
                allow_missing_password=True,
            )
        except ClientServiceError as exc:
            # Race with another writer on CNIC/NTN uniqueness surfaces here.
            errors.append(f"{label}: {exc}")
            continue
        created += 1
    return {"created": created, "skipped": skipped, "errors": errors}


# Portal-style parentage tokens copied from sheet cells: "usman ali so
# haider ali", "fatima do mushtaq", "khalida wo ahmad", "rashida wid
# majid" — plus the slashed and full "widow of" forms. Order matters:
# the explicit slashed/full forms are checked before the bare words.
_RELATION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bs\s*/\s*o\b", re.IGNORECASE), "s/o"),
    (re.compile(r"\bd\s*/\s*o\b", re.IGNORECASE), "d/o"),
    (re.compile(r"\bw\s*/\s*o\b", re.IGNORECASE), "w/o"),
    (re.compile(r"\bwidow\s+of\b|\bwidow\b|\bwid\b", re.IGNORECASE), "widow of"),
    (re.compile(r"\bso\b", re.IGNORECASE), "s/o"),
    (re.compile(r"\bdo\b", re.IGNORECASE), "d/o"),
    (re.compile(r"\bwo\b", re.IGNORECASE), "w/o"),
]


def split_name_parentage(raw: Any) -> tuple[str, str, str]:
    """Split '[name] so|do|wo|wid|widow of [parent]' into its parts.

    Returns ``(name, relation, parent)``; when no parentage token is found
    (or one side would stay empty), the whole text is returned as the
    name with empty relation/parent so nothing is guessed away.
    """
    text = _clean(raw)
    if not text:
        return "", "", ""
    for pattern, relation in _RELATION_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        name = text[: match.start()].strip(" -:،")
        parent = text[match.end():].strip(" -:،")
        if name and parent:
            return name, relation, parent
    return text, "", ""


def apply_name_split(
    values: dict[str, str], *, enabled: bool,
) -> dict[str, str]:
    """Optionally split the name cell's parentage into the father field.

    Only when enabled and a token is present. An already-mapped,
    non-empty father value always wins over the split result.
    """
    if not enabled:
        return values
    name, relation, parent = split_name_parentage(values.get("name"))
    if not relation:
        return values
    return {
        **values,
        "name": name,
        "father_name": values.get("father_name") or parent,
    }


def export_clients(
    records: list[Any],
    file_format: str,
) -> bytes:
    """Serialize clients to CSV/XLSX/ODS (passwords are never exported).

    ``records`` are Client model objects or any objects with the standard
    client attributes.
    """
    from services.dc_rate_service import _export_simple_table

    headers = [CLIENT_FIELD_LABELS[field] for field in EXPORT_FIELD_ORDER]
    rows = [
        [str(getattr(client, field) or "") for field in EXPORT_FIELD_ORDER]
        for client in records
    ]
    return _export_simple_table(
        headers, rows, file_format, "FBR Clients",
    )


def preview_saved_column_swap(
    field_names: list[str],
    *,
    client_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Preview which saved clients change when their columns swap/rotate.

    Two fields exchange values; three fields rotate (first -> second ->
    third -> first). Only clients where the involved values actually
    differ are listed; nothing is written here.
    """
    fields = [str(name) for name in field_names]
    if len(fields) not in (2, 3) or len(set(fields)) != len(fields):
        raise ClientImportError("Pick exactly two or three different columns.")
    unknown = [name for name in fields if name not in SWAPPABLE_CLIENT_FIELDS]
    if unknown:
        raise ClientImportError(
            "Only plain text columns can be swapped: "
            + ", ".join(sorted(SWAPPABLE_CLIENT_FIELDS))
        )
    statement = select(Client).order_by(Client.name, Client.id)
    if client_ids:
        statement = statement.where(Client.id.in_([int(v) for v in client_ids]))
    with SessionLocal() as session:
        clients = session.scalars(statement).all()
        affected = []
        for client in clients:
            values = [getattr(client, name) for name in fields]
            texts = [str(value or "").strip() for value in values]
            if len(set(texts)) == 1:
                continue  # swapping identical values changes nothing
            # Values flow first -> second -> third -> first: new first is
            # the old last value (a two-field rotate is the plain swap).
            rotated = texts[-1:] + texts[:-1]
            affected.append({
                "client_id": client.id,
                "client_name": client.name,
                "changes": [
                    {
                        "field": name,
                        "label": SWAPPABLE_CLIENT_FIELDS[name],
                        "before": before,
                        "after": after,
                    }
                    for name, before, after in zip(fields, texts, rotated)
                ],
            })
    return {
        "fields": fields,
        "affected": affected,
        "count": len(affected),
    }


def apply_saved_column_swap(
    field_names: list[str],
    *,
    client_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Apply the swap/rotation previewed by :func:`preview_saved_column_swap`.

    Values rotate per client (two fields exchange; three rotate), inside
    one transaction. Returns ``{"updated", "unchanged"}``.
    """
    preview = preview_saved_column_swap(field_names, client_ids=client_ids)
    if not preview["affected"]:
        return {"updated": 0, "unchanged": 0}
    fields = preview["fields"]
    updated_ids = [item["client_id"] for item in preview["affected"]]
    with SessionLocal.begin() as session:
        clients = session.scalars(
            select(Client).where(Client.id.in_(updated_ids))
        ).all()
        for client in clients:
            texts = [str(getattr(client, name) or "").strip() for name in fields]
            rotated = texts[-1:] + texts[:-1]
            for name, value in zip(fields, rotated):
                setattr(client, name, value or None)
    return {"updated": len(updated_ids), "unchanged": 0}
