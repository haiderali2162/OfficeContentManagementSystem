"""Validation and persistence for DC valuation rates and Excel imports."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO
import csv
import os
import sys
import re
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from sqlalchemy import func, or_, select
from sqlalchemy.orm import joinedload

from core.area_engine import AreaError, LandArea
from core.database import SessionLocal
from core.models import (
    DC_RATE_UNITS,
    DCValuationRate,
    KhewatReference,
    PropertyKhewat,
    PropertyLocation,
)


class DCRateServiceError(ValueError):
    """Raised when a DC-rate operation fails validation or conflicts."""


def normalize_rhz_year(value: str | int) -> str:
    """Normalize RHZ year variants to the stored ``YYYY-YY`` format."""
    raw = _text(value)
    if not raw:
        raise DCRateServiceError("Select an RHZ year.")
    match = re.fullmatch(r"(\d{4})\s*[-/]\s*(\d{2}|\d{4})", raw)
    if not match:
        raise DCRateServiceError("Enter an RHZ year like 2019-20.")
    start = int(match.group(1))
    end_text = match.group(2)
    end = int(end_text) if len(end_text) == 4 else (start // 100) * 100 + int(end_text)
    if len(end_text) == 2 and end < start:
        end += 100
    if start < 1800 or start > 2198 or end != start + 1:
        raise DCRateServiceError("RHZ year must cover two consecutive years, such as 2019-20.")
    return f"{start:04d}-{end % 100:02d}"


def normalize_khewat_size(value: str | None) -> str | None:
    """Validate and canonicalize optional Kanal-Marla-Feet size text."""
    raw = _text(value)
    if not raw:
        return None
    try:
        area = LandArea.parse(raw)
    except AreaError as exc:
        raise DCRateServiceError(
            "Khewat size must use K-M-F format, for example 12-4-0."
        ) from exc
    del area  # Parsing validates whole, non-negative components without imposing a unit conversion.
    parts = [int(part.strip()) for part in raw.split("-")]
    return "-".join(str(part) for part in parts)


LOCATION_FIELDS = (
    "name",
    "serial_no",
    "province",
    "district",
    "tehsil",
    "land_type",
    "committee_town",
    "revenue_circle",
    "property_area",
    "qanoongoi",
    "patwar_circle",
    "mauza",
    "hadbast_number",
    "square_no",
    "kila_no",
    "khasra_no",
    "locality",
    "notes",
)
LOCATION_IDENTITY_FIELDS = tuple(
    field for field in LOCATION_FIELDS if field != "notes"
)
RATE_HEADERS = {
    "location": "location_name",
    "location_name": "location_name",
    "province": "province",
    "district": "district",
    "tehsil": "tehsil",
    "rural_or_urban": "land_type",
    "land_type": "land_type",
    "committee": "committee_town",
    "town": "committee_town",
    "committee_town": "committee_town",
    "revenue_circle": "revenue_circle",
    "property_area": "property_area",
    "qanoongoi": "qanoongoi",
    "qanungoi": "qanoongoi",
    "patwar_circle": "patwar_circle",
    "mauza": "mauza",
    "hadbast": "hadbast_number",
    "hadbast_number": "hadbast_number",
    "square_no": "square_no",
    "square_number": "square_no",
    "kila_no": "kila_no",
    "killa_no": "kila_no",
    "khasra_no": "khasra_no",
    "khasra_number": "khasra_no",
    "locality": "locality",
    "location_description": "locality",
    "property_type": "property_type",
    "classification": "classification",
    "land_classification": "classification",
    "floor": "floor",
    "schedule_type": "schedule_type",
    "schedule": "schedule_type",
    "dc_schedule": "rate_value",
    "dc_rate": "rate_value",
    "fbr_schedule": "rate_value",
    "schedule_reference": "source_reference",
    "rate": "rate_value",
    "rate_value": "rate_value",
    "structure_rate": "structure_rate",
    "per_marla_valuation": "per_marla_value",
    "per_marla_value": "per_marla_value",
    "value_of_property_per_square_ft_in_rs": "rate_value",
    "value_of_property_per_square_foot_in_rs": "rate_value",
    "value_per_square_ft": "rate_value",
    "square_no_location": "square_no",
    "location_square_no": "square_no",
    "serial_no": "serial_no",
    "rate_unit": "rate_unit",
    "unit": "rate_unit",
    "effective_from": "effective_from",
    "published_year": "published_year",
    "publish_year": "published_year",
    "schedule_year": "published_year",
    "year": "published_year",
    "source": "source_reference",
    "source_reference": "source_reference",
}
REQUIRED_RATE_COLUMNS = {
    "location_name",
    "property_type",
    "rate_value",
    "rate_unit",
}


def _text(value: Any) -> str | None:
    """Trim optional text, mapping empty values to None."""
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _apply_qanoongoi_filter(statement, qanoongoi: str):
    """Match a Qanoongoi filter case-insensitively against trimmed DB text."""
    term = _text(qanoongoi)
    if term:
        statement = statement.where(
            func.lower(func.trim(PropertyLocation.qanoongoi)).like(
                f"%{term.casefold()}%"
            )
        )
    return statement


def _reference_values(value: Any) -> list[str]:
    """Expand square/Kila/Khasra lists and inclusive numeric ranges to tokens."""
    normalized = _text(value)
    if not normalized:
        return []
    tokens: list[str] = []
    for segment in re.split(r"[,;]", normalized):
        segment = segment.strip()
        if not segment:
            continue
        interval = re.fullmatch(r"(\d+)\s*-\s*(\d+)", segment)
        if interval:
            # Published schedules sometimes write the larger endpoint first;
            # treat either order as the same inclusive range.
            start, end = sorted(int(part) for part in interval.groups())
            if end - start > 5000:
                raise DCRateServiceError("A square, Kila, or Khasra range cannot exceed 5,001 values.")
            tokens.extend(str(number) for number in range(start, end + 1))
        elif segment.isdigit():
            # Canonical integer tokens allow a user to search 2 and match an
            # imported range containing 2 without matching 12 or 20.
            tokens.append(str(int(segment)))
        else:
            tokens.append(segment)
    return list(dict.fromkeys(tokens))


def _normalize_reference(value: Any) -> str | None:
    tokens = _reference_values(value)
    return ", ".join(tokens) if tokens else None


def _normalize_rate_unit(value: Any) -> str:
    """Normalize common schedule wording to one of the stored explicit units."""
    raw = _text(value)
    if not raw:
        return ""
    # Ignore parenthetical notes such as a stated marla convention, plus
    # currency/rate words that do not change the area unit.
    normalized = re.sub(r"\([^)]*\)", " ", raw.casefold())
    tokens = re.findall(r"[a-z]+", normalized)
    tokens = [token for token in tokens if token not in {
        "per", "rate", "value", "valuation", "rs", "rupee", "rupees", "in", "of", "the",
    }]
    token_set = set(tokens)
    if "square" in token_set or "sq" in token_set:
        if token_set.intersection({"foot", "feet", "ft"}):
            return "per_square_foot"
        if token_set.intersection({"yard", "yards", "yd"}):
            return "per_square_yard"
        if token_set.intersection({"meter", "meters", "metre", "metres", "m"}):
            return "per_square_meter"
    if token_set.intersection({"marla", "marlas"}):
        return "per_marla"
    if token_set.intersection({"kanal", "kanals"}):
        return "per_kanal"
    if token_set.intersection({"kila", "kilas", "killa", "killas"}):
        return "per_kila"
    if token_set.intersection({"acre", "acres"}):
        return "per_acre"
    if token_set.intersection({"plot", "plots"}):
        return "per_plot"
    compact = re.sub(r"[^a-z0-9]+", "_", raw.casefold()).strip("_")
    aliases = {
        "sqft": "per_square_foot", "sq_ft": "per_square_foot",
        "square_feet": "per_square_foot", "square_foot": "per_square_foot",
        "sqyd": "per_square_yard", "sq_yd": "per_square_yard",
        "square_yards": "per_square_yard", "square_yard": "per_square_yard",
        "sqm": "per_square_meter", "sq_m": "per_square_meter",
        "square_meters": "per_square_meter", "square_meter": "per_square_meter",
        "marla": "per_marla", "kanal": "per_kanal", "kila": "per_kila",
        "killa": "per_kila", "acre": "per_acre", "plot": "per_plot",
    }
    return aliases.get(compact, compact)


def _import_fix_suggestion(message: str, kind: str) -> str:
    """Return a concise action the reviewer can take for an import issue."""
    lower = message.casefold()
    if "rate unit" in lower or "supported rate unit" in lower:
        return "Use a recognized unit such as 'per kanal', 'per marla', 'per acre', or 'per square foot'; the importer accepts common wording variants."
    if "range" in lower:
        return "Use an ascending range or split an exceptionally large range into smaller ranges."
    if "blank" in lower or "required" in lower:
        return "Fill the required source cell or map the correct spreadsheet column. Optional fields may remain blank."
    if "conflict" in lower or "duplicate" in lower or kind.startswith("duplicate"):
        return "Review the existing location, class, unit, and effective period; correct the source row if this is a distinct rate."
    if "decimal" in lower or "amount" in lower or "numeric" in lower:
        return "Enter a non-negative numeric value without currency text or thousands separators."
    return "Check the mapped columns and correct this source row, then preview the file again."


def _date(value: Any, label: str, *, required: bool = False) -> date | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise DCRateServiceError(f"{label} is required.")
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        raise DCRateServiceError(f"{label} must use YYYY-MM-DD format.") from None


def _location_data(values: dict[str, Any]) -> dict[str, str | None]:
    name = _text(values.get("name", values.get("location_name")))
    if not name:
        raise DCRateServiceError("Location name is required.")
    location = {field: _text(values.get(field)) for field in LOCATION_FIELDS}
    location["name"] = name
    # The structured location fields are scalar database columns. Store ranges
    # as a canonical comma-separated list of individual identifiers.
    for field in ("square_no", "kila_no", "khasra_no"):
        location[field] = _normalize_reference(values.get(field))
    return location


def _location_key(values: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        (values.get(field) or "").strip().casefold()
        for field in LOCATION_IDENTITY_FIELDS
    )


def _rate_data(values: dict[str, Any]) -> dict[str, Any]:
    property_type = _text(values.get("property_type") or values.get("classification"))
    if not property_type:
        raise DCRateServiceError("Property type is required.")

    try:
        amount = Decimal(str(values.get("rate_value", "")).strip())
    except (InvalidOperation, ValueError, TypeError):
        raise DCRateServiceError("Rate must be a valid decimal number.") from None
    if not amount.is_finite() or amount < 0:
        raise DCRateServiceError("Rate must be a finite, non-negative amount.")

    def optional_amount(key: str, label: str) -> Decimal | None:
        raw = values.get(key)
        if raw in (None, ""):
            return None
        try:
            result = Decimal(str(raw).strip())
        except (InvalidOperation, ValueError, TypeError):
            raise DCRateServiceError(f"{label} must be a valid decimal number.") from None
        if not result.is_finite() or result < 0:
            raise DCRateServiceError(f"{label} must be a finite, non-negative amount.")
        return result

    structure_rate = optional_amount("structure_rate", "Structure rate")
    per_marla_value = optional_amount("per_marla_value", "Per marla valuation")

    raw_unit = _text(values.get("rate_unit")) or ""
    unit = _normalize_rate_unit(raw_unit)
    if unit not in DC_RATE_UNITS:
        raise DCRateServiceError(
            f"Rate unit '{raw_unit or '(blank)'}' is not recognized. Choose a supported unit: "
            + ", ".join(DC_RATE_UNITS) + "."
        )

    raw_year = values.get("published_year")
    try:
        year = int(str(raw_year).strip()) if raw_year not in (None, "") else None
    except (TypeError, ValueError):
        raise DCRateServiceError("Published year must be a four-digit year.") from None
    if year is not None and not 1900 <= year <= 2200:
        raise DCRateServiceError("Published year must be between 1900 and 2200.")
    start = _date(values.get("effective_from"), "Effective from")
    if year is not None:
        start = date(year, 1, 1)
    if start is None:
        # effective_from is retained as a required legacy storage column. A
        # missing schedule year remains NULL so later valuation can flag it.
        start = date.today()
    elif year is None:
        year = start.year
    end = _date(values.get("effective_to"), "Effective to")
    source_date = _date(values.get("source_date"), "Source date")
    if end and end < start:
        raise DCRateServiceError("Effective to cannot be before Effective from.")

    status = (_text(values.get("verification_status")) or "unverified").lower()
    schedule_type = (_text(values.get("schedule_type")) or "dc").lower()
    if schedule_type not in {"dc", "fbr"}:
        raise DCRateServiceError("Schedule must be DC or FBR.")
    if schedule_type == "fbr":
        # FBR schedule imports use a per-square-foot amount. The optional
        # per-marla amount can be retained as published or derived using the
        # specific 272 sq ft convention requested for this import template.
        unit = "per_square_foot"
        if per_marla_value is None:
            per_marla_value = amount * Decimal(272)
    if status not in {"unverified", "verified"}:
        raise DCRateServiceError("Verification status must be Unverified or Verified.")
    source = _text(values.get("source_reference"))
    verified_by = _text(values.get("verified_by"))
    if status == "verified" and (not source or not verified_by):
        raise DCRateServiceError(
            "A verified rate requires a source reference and the verifier's name."
        )

    return {
        "schedule_type": schedule_type,
        "property_type": property_type,
        "classification": _text(values.get("classification")),
        "floor": _text(values.get("floor")),
        "rate_value": amount,
        "rate_unit": unit,
        "structure_rate": structure_rate,
        "per_marla_value": per_marla_value,
        "effective_from": start,
        "published_year": year,
        "effective_to": end,
        "source_reference": source,
        "source_date": source_date,
        "verification_status": status,
        "verified_at": datetime.now(timezone.utc) if status == "verified" else None,
        "verified_by": verified_by if status == "verified" else None,
    }


def _rate_identity(rate: dict[str, Any], location_key: tuple[str, ...]) -> tuple[Any, ...]:
    return (
        rate.get("schedule_type", "dc"),
        location_key,
        rate["property_type"].casefold(),
        (rate.get("classification") or "").casefold(),
        (rate.get("floor") or "").casefold(),
        rate["rate_unit"],
        rate["effective_from"],
        rate["effective_to"],
    )


def _periods_overlap(
    first_start: date,
    first_end: date | None,
    second_start: date,
    second_end: date | None,
) -> bool:
    """Treat an absent end date as an open-ended effective period."""
    return (first_end is None or second_start <= first_end) and (
        second_end is None or first_start <= second_end
    )


def _record_dict(rate: DCValuationRate) -> dict[str, Any]:
    location = rate.location
    return {
        "id": rate.id,
        "location_id": rate.location_id,
        "location_name": location.name if location else "Unassigned",
        "location_notes": location.notes if location else None,
        "location_created_at": location.created_at if location else None,
        "location_updated_at": location.updated_at if location else None,
        "province": location.province if location else None,
        "district": location.district if location else None,
        "tehsil": location.tehsil if location else None,
        "land_type": location.land_type if location else None,
        "serial_no": location.serial_no if location else None,
        "committee_town": location.committee_town if location else None,
        "revenue_circle": location.revenue_circle if location else None,
        "property_area": location.property_area if location else None,
        "qanoongoi": location.qanoongoi if location else None,
        "patwar_circle": location.patwar_circle if location else None,
        "mauza": location.mauza if location else None,
        "hadbast_number": location.hadbast_number if location else None,
        "square_no": location.square_no if location else None,
        "kila_no": location.kila_no if location else None,
        "khasra_no": location.khasra_no if location else None,
        "locality": location.locality if location else None,
        "property_type": rate.property_type,
        "schedule_type": rate.schedule_type,
        "classification": rate.classification,
        "floor": rate.floor,
        "rate_value": rate.rate_value,
        "rate_unit": rate.rate_unit,
        "structure_rate": rate.structure_rate,
        "per_marla_value": rate.per_marla_value,
        "effective_from": rate.effective_from,
        "published_year": rate.published_year,
        "effective_to": rate.effective_to,
        "source_reference": rate.source_reference,
        "source_date": rate.source_date,
        "verification_status": rate.verification_status,
        "verified_at": rate.verified_at,
        "verified_by": rate.verified_by,
        "created_at": rate.created_at,
        "updated_at": rate.updated_at,
    }


def _duplicate_value(value: Any) -> Any:
    """Normalize text for duplicate identity while preserving typed values."""
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    return value


def _duplicate_key(rate: DCValuationRate) -> tuple[Any, ...]:
    """Build the explicit rural key or strict full-row key requested by the UI."""
    location = rate.location
    if rate.schedule_type == "dc" and location and (location.land_type or "").casefold() == "rural":
        # Rural DC schedules identify a row within its Mauza by cadastral
        # numbers, location, classification, and unit. Administrative context
        # avoids colliding with similarly named Mauzas in other districts.
        location_key = tuple(_duplicate_value(getattr(location, field)) for field in (
            "province", "district", "tehsil", "qanoongoi", "patwar_circle",
            "mauza", "square_no", "kila_no", "khasra_no", "name", "locality",
        ))
        return (
            "rural_dc", location_key,
            _duplicate_value(rate.classification or rate.property_type),
            _duplicate_value(rate.rate_unit),
        )

    # Urban DC and FBR rows are duplicates only when all source values match.
    # Surrogate IDs and audit timestamps differ on repeated inserts, so exclude
    # those while retaining every substantive schedule and location column.
    rate_values = tuple(
        (column.name, _duplicate_value(getattr(rate, column.name)))
        for column in DCValuationRate.__table__.columns
        if column.name not in {"id", "location_id", "created_at", "updated_at"}
    )
    location_values = tuple(
        (column.name, _duplicate_value(getattr(location, column.name)) if location else None)
        for column in PropertyLocation.__table__.columns
        if column.name not in {"id", "created_at", "updated_at"}
    )
    return "strict", rate_values, location_values


def _duplicate_rate_groups(session) -> list[list[DCValuationRate]]:
    rows = session.scalars(
        select(DCValuationRate)
        .options(joinedload(DCValuationRate.location))
        .order_by(DCValuationRate.id)
    ).unique().all()
    grouped: dict[tuple[Any, ...], list[DCValuationRate]] = {}
    for rate in rows:
        grouped.setdefault(_duplicate_key(rate), []).append(rate)
    return [group for group in grouped.values() if len(group) > 1]


def find_duplicate_dc_rates() -> list[dict[str, Any]]:
    """List duplicate rows that would be removed, preserving the first row."""
    with SessionLocal() as session:
        duplicate_rows: list[dict[str, Any]] = []
        for group in _duplicate_rate_groups(session):
            survivor_id = group[0].id
            for duplicate in group[1:]:
                record = _record_dict(duplicate)
                record["duplicate_of"] = survivor_id
                duplicate_rows.append(record)
        return duplicate_rows


def _documents_directory() -> Path:
    """Resolve the user's Documents folder on Windows, macOS, or Linux."""
    if sys.platform == "win32":
        user_profile = os.environ.get("USERPROFILE")
        base = Path(user_profile) if user_profile else Path.home()
        return base / "Documents"
    if sys.platform == "darwin":
        return Path.home() / "Documents"

    # Linux desktop environments may move Documents via XDG user directories.
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


def remove_duplicate_dc_rates_with_report(file_format: str) -> tuple[int, Path | None]:
    """Export duplicate row values to Documents, then remove the extra rows.

    If report generation fails, no database rows are deleted. The lowest ID in
    each duplicate group is retained, and SQLAlchemy's SET NULL policy keeps
    historical estimate snapshots valid when duplicate rate links are removed.
    """
    if file_format not in {"csv", "xlsx", "ods"}:
        raise DCRateServiceError("Choose CSV, Excel, or ODS for the duplicate report.")

    report_path: Path | None = None
    try:
        with SessionLocal.begin() as session:
            groups = _duplicate_rate_groups(session)
            duplicates = [
                (survivor, duplicate)
                for group in groups
                for survivor, duplicate in ((group[0], row) for row in group[1:])
            ]
            if not duplicates:
                return 0, None

            report_records = []
            for survivor, duplicate in duplicates:
                record = _record_dict(duplicate)
                record["duplicate_of"] = survivor.id
                report_records.append(record)
            payload = export_schedule_records(report_records, file_format)

            documents_dir = _documents_directory()
            documents_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            report_path = documents_dir / f"dc_rate_duplicates_removed_{timestamp}.{file_format}"
            # Exclusive creation prevents overwriting a prior audit report.
            with report_path.open("xb") as report_file:
                report_file.write(payload)

            for _survivor, duplicate in duplicates:
                session.delete(duplicate)
        return len(duplicates), report_path
    except Exception:
        # A report is created before deletion so the removed values are never
        # lost. If the DB transaction fails, remove only the report we created.
        if report_path is not None:
            try:
                report_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


SCHEDULE_EXPORT_COLUMNS = (
    ("id", "Record ID"),
    ("duplicate_of", "Duplicate of record ID"),
    ("location_id", "Location ID"),
    ("schedule_type", "Schedule"),
    ("serial_no", "Serial no."),
    ("province", "Province"),
    ("district", "District"),
    ("tehsil", "Tehsil"),
    ("land_type", "Rural or urban"),
    ("committee_town", "Town / city"),
    ("revenue_circle", "Revenue circle"),
    ("qanoongoi", "Qanoongoi"),
    ("patwar_circle", "Patwar circle"),
    ("mauza", "Mauza"),
    ("square_no", "Square no"),
    ("kila_no", "Kila no"),
    ("khasra_no", "Khasra no"),
    ("floor", "Floor"),
    ("property_area", "Property area"),
    ("location_name", "Location"),
    ("locality", "Location detail"),
    ("hadbast_number", "Hadbast number"),
    ("classification", "Land classification"),
    ("rate_value", "Schedule rate"),
    ("rate_unit", "Unit"),
    ("structure_rate", "Structure rate"),
    ("per_marla_value", "Per marla valuation"),
    ("effective_from", "Effective from"),
    ("effective_to", "Effective to"),
    ("published_year", "Published year"),
    ("source_reference", "Schedule reference"),
    ("source_date", "Source date"),
    ("verification_status", "Verification status"),
    ("verified_at", "Verified at"),
    ("verified_by", "Verified by"),
    ("location_notes", "Location notes"),
    ("location_created_at", "Location created at"),
    ("location_updated_at", "Location updated at"),
    ("created_at", "Rate created at"),
    ("updated_at", "Rate updated at"),
)


def export_schedule_records(
    records: list[dict[str, Any]], file_format: str, fields: list[str] | None = None
) -> bytes:
    """Serialize the selected schedule dimensions to CSV, XLSX, or ODS bytes."""
    columns_by_key = dict(SCHEDULE_EXPORT_COLUMNS)
    selected = fields or [key for key, _label in SCHEDULE_EXPORT_COLUMNS]
    unknown = set(selected) - columns_by_key.keys()
    if unknown:
        raise DCRateServiceError("Unsupported export field(s): " + ", ".join(sorted(unknown)))
    labels = [columns_by_key[key] for key in selected]

    def cell(record: dict[str, Any], key: str) -> str:
        value = record.get(key)
        if value is None:
            return ""
        return value.isoformat() if hasattr(value, "isoformat") else str(value)

    rows = [[cell(record, key) for key in selected] for record in records]
    output = BytesIO()
    if file_format == "csv":
        text_output = StringIO(newline="")
        writer = csv.writer(text_output)
        writer.writerow(labels)
        writer.writerows(rows)
        return text_output.getvalue().encode("utf-8-sig")
    if file_format == "xlsx":
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Valuation Schedules"
        sheet.append(labels)
        for row in rows:
            sheet.append(row)
        workbook.save(output)
        workbook.close()
        return output.getvalue()
    if file_format == "ods":
        try:
            from odf.opendocument import OpenDocumentSpreadsheet
            from odf.table import Table, TableCell, TableRow
            from odf.text import P
            document = OpenDocumentSpreadsheet()
            sheet = Table(name="Valuation Schedules")
            for values in [labels, *rows]:
                row = TableRow()
                for value in values:
                    cell_control = TableCell()
                    if value:
                        cell_control.addElement(P(text=value))
                    row.addElement(cell_control)
                sheet.addElement(row)
            document.spreadsheet.addElement(sheet)
            document.write(output)
            return output.getvalue()
        except Exception as exc:
            raise DCRateServiceError(f"Could not create ODS export: {exc}") from exc
    raise DCRateServiceError("Choose CSV, XLSX, or ODS for export.")


def list_locations() -> list[dict[str, Any]]:
    with SessionLocal() as session:
        locations = session.scalars(
            select(PropertyLocation).order_by(
                PropertyLocation.district,
                PropertyLocation.tehsil,
                PropertyLocation.name,
                PropertyLocation.id,
            )
        ).all()
        return [
            {
                "id": location.id,
                **{field: getattr(location, field) for field in LOCATION_FIELDS},
            }
            for location in locations
        ]


def list_mauza_names(
    *, search: str = "", qanoongoi: str = "", limit: int = 50
) -> list[str]:
    """Return distinct Mauza values that have at least one DC or FBR rate."""
    statement = (
        select(PropertyLocation.mauza)
        .join(DCValuationRate, DCValuationRate.location_id == PropertyLocation.id)
        .where(PropertyLocation.mauza.is_not(None), PropertyLocation.mauza != "")
        .distinct()
        .order_by(PropertyLocation.mauza)
    )
    if search.strip():
        statement = statement.where(PropertyLocation.mauza.ilike(f"%{search.strip()}%"))
    statement = _apply_qanoongoi_filter(statement, qanoongoi)
    statement = statement.limit(max(1, limit))
    with SessionLocal() as session:
        return list(session.scalars(statement).all())


def summarize_location_hierarchy() -> list[dict[str, Any]]:
    """Summarize Tehsils and Qanoongoi per district with Mauza counts.

    Blank administrative values are returned as empty strings so the UI can
    label them as unrecorded without hiding their Mauza rows.
    """
    district_key = func.lower(func.trim(func.coalesce(PropertyLocation.district, "")))
    tehsil_key = func.lower(func.trim(func.coalesce(PropertyLocation.tehsil, "")))
    qanoongoi_key = func.lower(func.trim(func.coalesce(PropertyLocation.qanoongoi, "")))
    mauza_key = func.lower(func.trim(PropertyLocation.mauza))
    statement = (
        select(
            district_key.label("district_key"),
            func.min(func.trim(PropertyLocation.district)).label("district"),
            tehsil_key.label("tehsil_key"),
            func.min(func.trim(PropertyLocation.tehsil)).label("tehsil"),
            qanoongoi_key.label("qanoongoi_key"),
            func.min(func.trim(PropertyLocation.qanoongoi)).label("qanoongoi"),
            func.count(func.distinct(mauza_key)).label("mauza_count"),
        )
        .where(
            PropertyLocation.mauza.is_not(None),
            func.trim(PropertyLocation.mauza) != "",
        )
        .group_by(district_key, tehsil_key, qanoongoi_key)
    )
    with SessionLocal() as session:
        rows = session.execute(statement).all()

    tehsils_by_district: dict[str, set[str]] = {}
    qanoongoi_by_tehsil: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        if row.tehsil_key:
            tehsils_by_district.setdefault(row.district_key, set()).add(
                row.tehsil_key
            )
        if row.qanoongoi_key:
            qanoongoi_by_tehsil.setdefault(
                (row.district_key, row.tehsil_key), set()
            ).add(row.qanoongoi_key)

    summary = [
        {
            "district": row.district or "",
            "tehsil": row.tehsil or "",
            "qanoongoi": row.qanoongoi or "",
            "mauza_count": int(row.mauza_count),
            "tehsil_count": len(tehsils_by_district.get(row.district_key, set())),
            "qanoongoi_count": len(
                qanoongoi_by_tehsil.get((row.district_key, row.tehsil_key), set())
            ),
        }
        for row in rows
    ]
    summary.sort(
        key=lambda item: (
            item["district"].casefold(),
            item["tehsil"].casefold(),
            item["qanoongoi"].casefold(),
        )
    )
    return summary


MAUZA_CONTEXT_FIELDS = (
    "province", "district", "tehsil", "qanoongoi", "patwar_circle",
    "mauza_name", "hadbast_number",
)
_MAUZA_LOCATION_COLUMNS = {
    "province": PropertyLocation.province,
    "district": PropertyLocation.district,
    "tehsil": PropertyLocation.tehsil,
    "qanoongoi": PropertyLocation.qanoongoi,
    "patwar_circle": PropertyLocation.patwar_circle,
    "mauza_name": PropertyLocation.mauza,
    "hadbast_number": PropertyLocation.hadbast_number,
}


def _apply_mauza_context(statement, context: dict[str, Any]):
    """Restrict queries to the selected Mauza and its administrative identity."""
    for name, column in _MAUZA_LOCATION_COLUMNS.items():
        value = _text(context.get(name))
        if name == "mauza_name" and not value:
            raise DCRateServiceError("Select a Mauza before searching cadastral references.")
        statement = statement.where(column == value if value else column.is_(None))
    return statement


def list_mauza_contexts(*, search: str = "", limit: int = 500) -> list[dict[str, str | None]]:
    """List Mauza identities found in schedules or existing Khewat records."""
    columns = [_MAUZA_LOCATION_COLUMNS[field] for field in MAUZA_CONTEXT_FIELDS]
    statement = (
        select(*columns)
        .join(DCValuationRate, DCValuationRate.location_id == PropertyLocation.id)
        .where(PropertyLocation.mauza.is_not(None), PropertyLocation.mauza != "")
        .distinct()
    )
    query = search.strip()
    if query:
        statement = statement.where(PropertyLocation.mauza.ilike(f"%{query}%"))
    statement = statement.order_by(PropertyLocation.mauza, PropertyLocation.district,
                                  PropertyLocation.tehsil).limit(max(1, limit))
    with SessionLocal() as session:
        rows = session.execute(statement).all()
        khewat_statement = select(
            *(getattr(PropertyKhewat, field) for field in MAUZA_CONTEXT_FIELDS)
        ).where(PropertyKhewat.mauza_name.is_not(None), PropertyKhewat.mauza_name != "")
        if query:
            khewat_statement = khewat_statement.where(
                PropertyKhewat.mauza_name.ilike(f"%{query}%")
            )
        khewat_rows = session.execute(khewat_statement).all()
    contexts = {
        tuple(value or "" for value in row)
        for row in [*rows, *khewat_rows]
    }
    ordered = sorted(contexts, key=lambda row: tuple(str(value).casefold() for value in row))
    return [dict(zip(MAUZA_CONTEXT_FIELDS, row)) for row in ordered[:max(1, limit)]]


def list_saved_khewat_mauzas(*, search: str = "", limit: int = 200) -> list[str]:
    """Return saved Khewat Mauzas for the searchable Khewat filter."""
    statement = select(PropertyKhewat.mauza_name).where(
        PropertyKhewat.mauza_name.is_not(None), PropertyKhewat.mauza_name != ""
    ).distinct()
    if search.strip():
        statement = statement.where(PropertyKhewat.mauza_name.ilike(f"%{search.strip()}%"))
    statement = statement.order_by(PropertyKhewat.mauza_name).limit(max(1, limit))
    with SessionLocal() as session:
        return list(session.scalars(statement).all())


def list_saved_khewat_numbers(*, mauza: str = "", search: str = "", limit: int = 200) -> list[str]:
    """Return searchable Khewat numbers, optionally scoped to a Mauza."""
    statement = select(PropertyKhewat.khewat_number).distinct()
    if mauza.strip():
        statement = statement.where(PropertyKhewat.mauza_name.ilike(f"%{mauza.strip()}%"))
    if search.strip():
        statement = statement.where(PropertyKhewat.khewat_number.ilike(f"%{search.strip()}%"))
    statement = statement.order_by(PropertyKhewat.khewat_number).limit(max(1, limit))
    with SessionLocal() as session:
        return list(session.scalars(statement).all())


def list_mauza_cadastral_values(
    context: dict[str, Any], field_name: str, *, search: str = "", limit: int = 1000,
) -> list[str]:
    """List distinct expanded square or Kila references for one Mauza."""
    if field_name not in {"square_no", "kila_no"}:
        raise DCRateServiceError("Choose Square no. or Kila no. for this selection.")
    field = getattr(PropertyLocation, field_name)
    statement = (
        select(field)
        .join(DCValuationRate, DCValuationRate.location_id == PropertyLocation.id)
        .where(field.is_not(None), field != "")
    )
    statement = _apply_mauza_context(statement, context).distinct()
    with SessionLocal() as session:
        stored_values = session.scalars(statement).all()
    query = search.strip().casefold()
    tokens = {
        token
        for stored_value in stored_values
        for token in _reference_values(stored_value)
        if not query or query in token.casefold()
    }
    return sorted(
        tokens,
        key=lambda token: (not token.isdigit(), int(token) if token.isdigit() else token.casefold()),
    )[:max(1, limit)]


def summarize_mauza_cadastral_selection(
    *, context: dict[str, Any], square_numbers: list[str], kila_numbers: list[str],
    schedule_type: str | None = None, rate_unit: str | None = None,
    published_year: int | None = None,
) -> dict[str, Any]:
    """Summarize rates intersecting selected square/Kila values in one Mauza."""
    selected_squares = set(_reference_values(",".join(square_numbers)))
    selected_kilas = set(_reference_values(",".join(kila_numbers)))
    if not selected_squares and not selected_kilas:
        raise DCRateServiceError("Select at least one square or Kila before requesting a summary.")

    statement = select(DCValuationRate).options(joinedload(DCValuationRate.location))
    statement = statement.join(DCValuationRate.location)
    statement = _apply_mauza_context(statement, context)
    if schedule_type in {"dc", "fbr"}:
        statement = statement.where(DCValuationRate.schedule_type == schedule_type)
    if rate_unit:
        statement = statement.where(DCValuationRate.rate_unit == rate_unit)
    if published_year is not None:
        statement = statement.where(DCValuationRate.published_year == published_year)

    with SessionLocal() as session:
        rates = session.scalars(statement).unique().all()

    matches: list[DCValuationRate] = []
    matched_squares: set[str] = set()
    matched_kilas: set[str] = set()
    for rate in rates:
        location = rate.location
        rate_squares = set(_reference_values(location.square_no)) if location else set()
        rate_kilas = set(_reference_values(location.kila_no)) if location else set()
        square_match = not selected_squares or bool(selected_squares & rate_squares)
        kila_match = not selected_kilas or bool(selected_kilas & rate_kilas)
        if square_match and kila_match:
            matches.append(rate)
            matched_squares.update(selected_squares & rate_squares)
            matched_kilas.update(selected_kilas & rate_kilas)

    def ordered_extremes(values: set[str]) -> tuple[str | None, str | None]:
        ordered = sorted(
            values,
            key=lambda token: (not token.isdigit(), int(token) if token.isdigit() else token.casefold()),
        )
        return (ordered[0], ordered[-1]) if ordered else (None, None)

    lowest_square, highest_square = ordered_extremes(matched_squares)
    lowest_kila, highest_kila = ordered_extremes(matched_kilas)
    lowest_rate = min(matches, key=lambda rate: (rate.rate_value, rate.id), default=None)
    highest_rate = max(matches, key=lambda rate: (rate.rate_value, -rate.id), default=None)
    return {
        "count": len(matches),
        "lowest_square": lowest_square,
        "highest_square": highest_square,
        "lowest_kila": lowest_kila,
        "highest_kila": highest_kila,
        "lowest_rate": lowest_rate.rate_value if lowest_rate else None,
        "highest_rate": highest_rate.rate_value if highest_rate else None,
    }


def _khewat_record(item: PropertyKhewat) -> dict[str, Any]:
    """Convert a Khewat and its child references into a UI/service record."""
    natural_key = lambda value: (not value.isdigit(), int(value) if value.isdigit() else value.casefold())
    return {
        "id": item.id,
        "province": item.province,
        "district": item.district,
        "tehsil": item.tehsil,
        "qanoongoi": item.qanoongoi,
        "patwar_circle": item.patwar_circle,
        "mauza_name": item.mauza_name,
        "hadbast_number": item.hadbast_number,
        "khewat_number": item.khewat_number,
        "rhz_year": item.rhz_year,
        "khewat_size": item.khewat_size,
        "squares": sorted(
            (ref.reference_value for ref in item.references if ref.reference_type == "square"),
            key=natural_key,
        ),
        "kilas": sorted(
            (ref.reference_value for ref in item.references if ref.reference_type == "kila"),
            key=natural_key,
        ),
    }


def list_rhz_years(context: dict[str, Any] | None = None) -> list[str]:
    """Build searchable canonical RHZ year options, including saved years."""
    current_year = date.today().year
    options = {f"{year}-{(year + 1) % 100:02d}" for year in range(1900, current_year + 3)}
    statement = select(PropertyKhewat.rhz_year).where(PropertyKhewat.rhz_year.is_not(None))
    if context:
        criteria = {field: (_text(context.get(field)) or "") for field in MAUZA_CONTEXT_FIELDS}
        statement = statement.where(*(
            getattr(PropertyKhewat, field) == value for field, value in criteria.items()
        ))
    with SessionLocal() as session:
        stored_years = session.scalars(statement.distinct()).all()
    for value in stored_years:
        try:
            options.add(normalize_rhz_year(value))
        except DCRateServiceError:
            continue
    return sorted(options, reverse=True)


def latest_rhz_year(context: dict[str, Any]) -> str | None:
    """Return the highest RHZ year already saved in this Mauza context."""
    years = [item["rhz_year"] for item in list_khewats_for_mauza(context) if item.get("rhz_year")]
    normalized = []
    for year in years:
        try:
            normalized.append(normalize_rhz_year(year))
        except DCRateServiceError:
            continue
    return max(normalized, key=lambda item: int(item[:4]), default=None)


def list_khewats_for_mauza(
    context: dict[str, Any], *, rhz_year: str | None = None,
) -> list[dict[str, Any]]:
    """Return saved Khewats for an exact Mauza context and optional RHZ year."""
    criteria = {field: (_text(context.get(field)) or "") for field in MAUZA_CONTEXT_FIELDS}
    statement = (
        select(PropertyKhewat)
        .options(joinedload(PropertyKhewat.references))
        .where(*(getattr(PropertyKhewat, field) == value for field, value in criteria.items()))
    )
    if rhz_year:
        statement = statement.where(PropertyKhewat.rhz_year == normalize_rhz_year(rhz_year))
    statement = statement.order_by(PropertyKhewat.rhz_year.desc(), PropertyKhewat.khewat_number)
    with SessionLocal() as session:
        return [_khewat_record(item) for item in session.scalars(statement).unique().all()]


def search_khewats(
    *, mauza: str = "", district: str = "", tehsil: str = "",
    khewat_number: str = "", rhz_year: str | None = None,
) -> list[dict[str, Any]]:
    """Search saved Khewats using the filters shown on the rates page."""
    statement = select(PropertyKhewat).options(joinedload(PropertyKhewat.references))
    for field, value in (("mauza_name", mauza), ("district", district), ("tehsil", tehsil),
                         ("khewat_number", khewat_number)):
        if value.strip():
            statement = statement.where(getattr(PropertyKhewat, field).ilike(f"%{value.strip()}%"))
    if rhz_year:
        statement = statement.where(PropertyKhewat.rhz_year == normalize_rhz_year(rhz_year))
    statement = statement.order_by(
        PropertyKhewat.mauza_name, PropertyKhewat.rhz_year.desc(), PropertyKhewat.khewat_number
    )
    with SessionLocal() as session:
        return [_khewat_record(item) for item in session.scalars(statement).unique().all()]


def get_khewat_details(khewat_id: int) -> dict[str, Any] | None:
    with SessionLocal() as session:
        item = session.scalar(
            select(PropertyKhewat)
            .options(joinedload(PropertyKhewat.references))
            .where(PropertyKhewat.id == khewat_id)
        )
        return _khewat_record(item) if item else None


KHEWAT_SUMMARY_COLUMNS = (
    ("serial_no", "Sr no"), ("mauza", "Mauza"), ("square_no", "Square no"),
    ("kila_no", "Kila no"), ("khasra_no", "Khasra no"),
    ("unit", "Unit"), ("schedule", "Schedule"),
)

AREA_UNIT_DISPLAY_NAMES = {
    "per_square_foot": "Square foot", "per_square_yard": "Square yard",
    "per_square_meter": "Square meter", "per_marla": "Marla",
    "per_kanal": "Kanal", "per_acre": "Acre", "per_kila": "Kila",
    "per_plot": "Plot",
}


def list_khewat_summary(khewat_id: int) -> list[dict[str, Any]]:
    """Build a schedule-aware, one-row-per-cadastral-reference Khewat summary."""
    record = get_khewat_details(khewat_id)
    if not record:
        return []
    return summarize_mauza_references(
        context={field: record.get(field) for field in MAUZA_CONTEXT_FIELDS},
        square_numbers=record["squares"], kila_numbers=record["kilas"],
    )


def summarize_mauza_references(
    *, context: dict[str, Any], square_numbers: list[str], kila_numbers: list[str],
) -> list[dict[str, Any]]:
    """Build the exportable summary rows for selected cadastral references."""
    selected_squares = list(dict.fromkeys(_reference_values(",".join(square_numbers))))
    selected_kilas = list(dict.fromkeys(_reference_values(",".join(kila_numbers))))
    statement = select(DCValuationRate).options(joinedload(DCValuationRate.location)).join(
        DCValuationRate.location
    )
    statement = _apply_mauza_context(statement, context)
    with SessionLocal() as session:
        rates = session.scalars(statement).unique().all()
    rows: list[dict[str, Any]] = []
    refs = [("square", value) for value in selected_squares]
    refs.extend(("kila", value) for value in selected_kilas)
    for kind, value in refs:
        field_name = "square_no" if kind == "square" else "kila_no"
        matching = []
        for rate in rates:
            location = rate.location
            if value in _reference_values(getattr(location, field_name, None)):
                matching.append(rate)
        if not matching:
            rows.append({
                "mauza": context.get("mauza_name") or "", "square_no": value if kind == "square" else "",
                "kila_no": value if kind == "kila" else "", "khasra_no": "", "unit": "", "schedule": "",
            })
        for rate in matching:
            location = rate.location
            rows.append({
                "mauza": context.get("mauza_name") or "",
                "square_no": value if kind == "square" else "",
                "kila_no": value if kind == "kila" else "",
                "khasra_no": location.khasra_no or "",
                "unit": AREA_UNIT_DISPLAY_NAMES.get(rate.rate_unit, rate.rate_unit or ""),
                # Keep the schedule source visible while showing its actual
                # valuation; a bare "DC" or "FBR" is not useful as a value.
                "schedule": (
                    f"{(rate.schedule_type or '').upper()} valuation: Rs. "
                    f"{format(rate.rate_value.normalize(), 'f')}"
                ),
            })
    for index, row in enumerate(rows, start=1):
        row["serial_no"] = index
    return rows


def export_khewat_summary(rows: list[dict[str, Any]], file_format: str) -> bytes:
    """Export Khewat summary rows as PDF, CSV, XLSX, or ODS."""
    normalized = file_format.strip().lower().lstrip(".")
    headers = [label for _key, label in KHEWAT_SUMMARY_COLUMNS]
    values = [[str(row.get(key, "") or "") for key, _label in KHEWAT_SUMMARY_COLUMNS]
              for row in rows]
    output = BytesIO()
    if normalized == "csv":
        stream = StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerows(values)
        return stream.getvalue().encode("utf-8-sig")
    if normalized == "xlsx":
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Khewat Summary"
        sheet.append(headers)
        for row in values:
            sheet.append(row)
        workbook.save(output)
        workbook.close()
        return output.getvalue()
    if normalized == "ods":
        try:
            from odf.opendocument import OpenDocumentSpreadsheet
            from odf.table import Table, TableCell, TableRow
            from odf.text import P
            document = OpenDocumentSpreadsheet()
            sheet = Table(name="Khewat Summary")
            for values_row in [headers, *values]:
                table_row = TableRow()
                for value in values_row:
                    cell = TableCell()
                    if value:
                        cell.addElement(P(text=value))
                    table_row.addElement(cell)
                sheet.addElement(table_row)
            document.spreadsheet.addElement(sheet)
            document.write(output)
            return output.getvalue()
        except Exception as exc:
            raise DCRateServiceError(f"Could not create ODS export: {exc}") from exc
    if normalized == "pdf":
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import landscape, A4
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle
        document = SimpleDocTemplate(output, pagesize=landscape(A4), title="Khewat Summary",
                                     rightMargin=10 * mm, leftMargin=10 * mm)
        table = Table([headers, *values], repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E78")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), .35, colors.HexColor("#CBD5E1")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F1F5F9")]),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        document.build([table])
        return output.getvalue()
    raise DCRateServiceError("Choose PDF, CSV, XLSX, or ODS for Khewat export.")


def export_khewat_import_template(file_format: str) -> bytes:
    """Create a blank, headered Khewat import sheet in a supported format."""
    columns = [
        ("mauza", "Mauza"), ("district", "District"), ("tehsil", "Tehsil"),
        ("khewat_number", "Khewat number"), ("rhz_year", "RHZ year"),
        ("square_no", "Square no"), ("kila_no", "Kila no"),
    ]
    headers = [label for _, label in columns]
    return _export_simple_table(headers, [], file_format, "Khewat Import")


def _export_simple_table(headers: list[str], rows: list[list[str]], file_format: str, title: str) -> bytes:
    """Serialize header and rows for Khewat templates and summary exports."""
    normalized = file_format.strip().lower().lstrip(".")
    output = BytesIO()
    if normalized == "csv":
        stream = StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerows(rows)
        return stream.getvalue().encode("utf-8-sig")
    if normalized == "xlsx":
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = title[:31]
        sheet.append(headers)
        for row in rows:
            sheet.append(row)
        workbook.save(output)
        workbook.close()
        return output.getvalue()
    if normalized == "ods":
        try:
            from odf.opendocument import OpenDocumentSpreadsheet
            from odf.table import Table, TableCell, TableRow
            from odf.text import P
            document = OpenDocumentSpreadsheet()
            sheet = Table(name=title[:31])
            for values in [headers, *rows]:
                table_row = TableRow()
                for value in values:
                    cell = TableCell()
                    if value:
                        cell.addElement(P(text=str(value)))
                    table_row.addElement(cell)
                sheet.addElement(table_row)
            document.spreadsheet.addElement(sheet)
            document.write(output)
            return output.getvalue()
        except Exception as exc:
            raise DCRateServiceError(f"Could not create ODS file: {exc}") from exc
    raise DCRateServiceError("Choose CSV, XLSX, or ODS.")


def preview_khewat_import(
    headers: list[str], rows: list[list[Any]], mapping: dict[str, int | None],
) -> dict[str, Any]:
    """Validate mapped Khewat spreadsheet rows without writing to the database."""
    valid: list[dict[str, str]] = []
    issues: list[dict[str, Any]] = []
    if mapping.get("mauza") is None or mapping.get("khewat_number") is None:
        raise DCRateServiceError("Map both Mauza and Khewat number columns.")
    if mapping.get("square_no") is None and mapping.get("kila_no") is None:
        raise DCRateServiceError("Map at least one Square no or Kila no column.")
    for row_index, row in enumerate(rows, start=2):
        def field(name: str) -> str:
            column = mapping.get(name)
            if column is None or column >= len(row):
                return ""
            return _text(row[column])
        record = {name: field(name) for name in (
            "mauza", "district", "tehsil", "khewat_number", "rhz_year", "square_no", "kila_no"
        )}
        problems = []
        if not record["mauza"]:
            problems.append("Mauza is required")
        if not record["khewat_number"]:
            problems.append("Khewat number is required")
        if not record["square_no"] and not record["kila_no"]:
            problems.append("Square no or Kila no is required")
        if record["rhz_year"]:
            try:
                record["rhz_year"] = normalize_rhz_year(record["rhz_year"])
            except DCRateServiceError as exc:
                problems.append(str(exc))
        if problems:
            issues.append({"row": row_index, "reason": "; ".join(problems)})
        else:
            record["_source_row"] = str(row_index)
            valid.append(record)
    return {"valid_records": valid, "issues": issues, "rows_read": len(rows)}


def import_khewat_assignments(assignments: list[dict[str, Any]]) -> dict[str, int]:
    """Validate and import a complete grouped spreadsheet atomically."""
    if not assignments:
        raise DCRateServiceError("The import contains no Khewat assignments.")
    normalized = []
    for item in assignments:
        values = _validated_khewat_values(
            context=item["context"], khewat_number=item["khewat_number"],
            rhz_year=item["rhz_year"], khewat_size=None,
            square_numbers=item.get("square_numbers", []),
            kila_numbers=item.get("kila_numbers", []),
        )
        normalized.append((values, item["context"]))
    created = 0
    added = 0
    with SessionLocal.begin() as session:
        # Validate all references before changing any record, so bad input never
        # leaves a partially imported workbook.
        for values, context in normalized:
            context_values, _number, _year, _size, squares, kilas = values
            _validate_khewat_references(session, context_values, squares, kilas)
        for values, _context in normalized:
            context_values, number, year, _size, squares, kilas = values
            khewat = session.scalar(select(PropertyKhewat).where(
                *(getattr(PropertyKhewat, field) == value for field, value in context_values.items()),
                PropertyKhewat.khewat_number == number,
                PropertyKhewat.rhz_year == year,
            ))
            if khewat is None:
                khewat = PropertyKhewat(**context_values, khewat_number=number, rhz_year=year)
                session.add(khewat)
                session.flush()
                created += 1
            existing = {
                (ref.reference_type, ref.reference_value)
                for ref in session.scalars(select(KhewatReference).where(
                    KhewatReference.khewat_id == khewat.id
                )).all()
            }
            for reference_type, reference_value in (
                [("square", value) for value in squares] + [("kila", value) for value in kilas]
            ):
                if (reference_type, reference_value) in existing:
                    continue
                session.add(KhewatReference(
                    khewat_id=khewat.id, reference_type=reference_type,
                    reference_value=reference_value,
                ))
                added += 1
    return {"created": created, "references_added": added, "assignments": len(normalized)}


def find_khewat_year_conflicts(
    *, context: dict[str, Any], rhz_year: str, exclude_id: int | None = None,
) -> list[dict[str, Any]]:
    """Find records in the same Mauza and RHZ year before a new save."""
    criteria = {field: (_text(context.get(field)) or "") for field in MAUZA_CONTEXT_FIELDS}
    statement = (
        select(PropertyKhewat)
        .options(joinedload(PropertyKhewat.references))
        .where(
            *(getattr(PropertyKhewat, field) == value for field, value in criteria.items()),
            PropertyKhewat.rhz_year == normalize_rhz_year(rhz_year),
        )
    )
    if exclude_id is not None:
        statement = statement.where(PropertyKhewat.id != exclude_id)
    with SessionLocal() as session:
        return [_khewat_record(item) for item in session.scalars(statement).unique().all()]


def _validated_khewat_values(
    *, context: dict[str, Any], khewat_number: str,
    rhz_year: str, khewat_size: str | None,
    square_numbers: list[str], kila_numbers: list[str], require_references: bool = True,
) -> tuple[dict[str, str], str, str, str | None, list[str], list[str]]:
    if not _text(context.get("mauza_name")):
        raise DCRateServiceError("Select the Mauza that owns this Khewat.")
    number = _text(khewat_number)
    if not number:
        raise DCRateServiceError("Enter a Khewat number.")
    if len(number) > 100:
        raise DCRateServiceError("Khewat number cannot exceed 100 characters.")
    year = normalize_rhz_year(rhz_year)
    size = normalize_khewat_size(khewat_size)
    squares = list(dict.fromkeys(_reference_values(",".join(square_numbers))))
    kilas = list(dict.fromkeys(_reference_values(",".join(kila_numbers))))
    if require_references and not squares and not kilas:
        raise DCRateServiceError("Select at least one square or Kila to add to the Khewat.")
    context_values = {field: (_text(context.get(field)) or "") for field in MAUZA_CONTEXT_FIELDS}
    return context_values, number, year, size, squares, kilas


def _validate_khewat_references(
    session, context, squares, kilas, *, allowed_existing: dict[str, set[str]] | None = None,
) -> None:
    allowed_existing = allowed_existing or {"square_no": set(), "kila_no": set()}
    for field_name, values in (("square_no", squares), ("kila_no", kilas)):
        known_values = set(_mauza_cadastral_values_in_session(session, context, field_name))
        unknown = set(values) - known_values - allowed_existing.get(field_name, set())
        if unknown:
            raise DCRateServiceError(
                f"These {field_name.replace('_no', '').title()} values are not listed for the selected Mauza: "
                + ", ".join(sorted(unknown))
            )


def assign_cadastral_references_to_khewat(
    *, context: dict[str, Any], khewat_number: str,
    rhz_year: str, khewat_size: str | None = None,
    square_numbers: list[str], kila_numbers: list[str],
) -> dict[str, Any]:
    """Create or extend a Khewat record identified by Mauza, number, and RHZ year."""
    context_values, number, year, size, squares, kilas = _validated_khewat_values(
        context=context, khewat_number=khewat_number, rhz_year=rhz_year,
        khewat_size=khewat_size, square_numbers=square_numbers, kila_numbers=kila_numbers,
    )
    with SessionLocal.begin() as session:
        _validate_khewat_references(session, context, squares, kilas)
        khewat = session.scalar(select(PropertyKhewat).where(
            *(getattr(PropertyKhewat, field) == value for field, value in context_values.items()),
            PropertyKhewat.khewat_number == number,
            PropertyKhewat.rhz_year == year,
        ))
        if khewat is None:
            khewat = PropertyKhewat(**context_values, khewat_number=number, rhz_year=year)
            session.add(khewat)
            session.flush()
        if size is not None:
            khewat.khewat_size = size
        existing = {
            (reference.reference_type, reference.reference_value)
            for reference in session.scalars(
                select(KhewatReference).where(KhewatReference.khewat_id == khewat.id)
            ).all()
        }
        requested = [("square", value) for value in squares] + [("kila", value) for value in kilas]
        added = 0
        for reference_type, reference_value in requested:
            if (reference_type, reference_value) in existing:
                continue
            session.add(KhewatReference(
                khewat_id=khewat.id,
                reference_type=reference_type,
                reference_value=reference_value,
            ))
            added += 1
    return {
        "khewat_number": number, "rhz_year": year, "khewat_size": size,
        "added": added, "existing": len(requested) - added,
        "squares": squares, "kilas": kilas,
    }


def update_khewat_record(
    *, khewat_id: int, khewat_number: str, rhz_year: str, khewat_size: str | None,
    square_numbers: list[str], kila_numbers: list[str],
) -> dict[str, Any]:
    """Edit a saved Khewat and replace its selected Square/Kila references."""
    with SessionLocal.begin() as session:
        existing = session.get(PropertyKhewat, khewat_id)
        if existing is None:
            raise DCRateServiceError("This Khewat no longer exists. Refresh the search results.")
        context = {field: getattr(existing, field) for field in MAUZA_CONTEXT_FIELDS}
        context_values, number, year, size, squares, kilas = _validated_khewat_values(
            context=context, khewat_number=khewat_number, rhz_year=rhz_year,
            khewat_size=khewat_size, square_numbers=square_numbers, kila_numbers=kila_numbers,
            require_references=False,
        )
        conflict = session.scalar(select(PropertyKhewat.id).where(
            *(getattr(PropertyKhewat, field) == value for field, value in context_values.items()),
            PropertyKhewat.khewat_number == number,
            PropertyKhewat.rhz_year == year,
            PropertyKhewat.id != khewat_id,
        ))
        if conflict is not None:
            raise DCRateServiceError("A Khewat with this number and RHZ year already exists in this Mauza.")
        current_refs = session.scalars(
            select(KhewatReference).where(KhewatReference.khewat_id == khewat_id)
        ).all()
        _validate_khewat_references(
            session, context, squares, kilas,
            allowed_existing={
                "square_no": {ref.reference_value for ref in current_refs if ref.reference_type == "square"},
                "kila_no": {ref.reference_value for ref in current_refs if ref.reference_type == "kila"},
            },
        )
        existing.khewat_number = number
        existing.rhz_year = year
        existing.khewat_size = size
        desired = {("square", value) for value in squares} | {("kila", value) for value in kilas}
        current = {(ref.reference_type, ref.reference_value): ref for ref in current_refs}
        removed = 0
        for key, ref in current.items():
            if key not in desired:
                session.delete(ref)
                removed += 1
        added = 0
        for ref_type, value in desired - set(current):
            session.add(KhewatReference(
                khewat_id=khewat_id, reference_type=ref_type, reference_value=value,
            ))
            added += 1
    return {"id": khewat_id, "khewat_number": number, "rhz_year": year,
            "khewat_size": size, "added": added, "removed": removed}


def _mauza_cadastral_values_in_session(session, context: dict[str, Any], field_name: str) -> list[str]:
    field = getattr(PropertyLocation, field_name)
    statement = (
        select(field)
        .join(DCValuationRate, DCValuationRate.location_id == PropertyLocation.id)
        .where(field.is_not(None), field != "")
    )
    stored_values = session.scalars(_apply_mauza_context(statement, context).distinct()).all()
    return list(dict.fromkeys(
        token for stored_value in stored_values for token in _reference_values(stored_value)
    ))


def list_cadastral_values(
    field_name: str, *, search: str = "", limit: int = 50,
) -> list[str]:
    """Return matching Square, Kila, or Khasra tokens present in saved schedules."""
    fields = {
        "square_no": PropertyLocation.square_no,
        "kila_no": PropertyLocation.kila_no,
        "khasra_no": PropertyLocation.khasra_no,
    }
    field = fields.get(field_name)
    if field is None:
        raise DCRateServiceError("Choose Square no., Kila no., or Khasra no. for suggestions.")
    statement = (
        select(field)
        .join(DCValuationRate, DCValuationRate.location_id == PropertyLocation.id)
        .where(field.is_not(None), field != "")
        .distinct()
    )
    query = search.strip()
    if query:
        statement = statement.where(field.ilike(f"%{query}%"))
    with SessionLocal() as session:
        stored_values = session.scalars(statement).all()
    tokens = {
        token
        for stored_value in stored_values
        for token in _reference_values(stored_value)
        if not query or token.casefold().startswith(query.casefold())
    }
    return sorted(tokens, key=lambda token: (not token.isdigit(), int(token) if token.isdigit() else token.casefold()))[:max(1, limit)]


def summarize_dc_rates(
    *, mauza: str = "", qanoongoi: str = "", rate_unit: str | None = None,
    schedule_type: str | None = None, land_type: str | None = None,
) -> dict[str, Any]:
    """Aggregate rate extremes for the selected location filters."""
    statement = (
        select(DCValuationRate.id)
        .join(PropertyLocation, DCValuationRate.location_id == PropertyLocation.id)
    )
    if mauza.strip():
        statement = statement.where(PropertyLocation.mauza.ilike(f"%{mauza.strip()}%"))
    statement = _apply_qanoongoi_filter(statement, qanoongoi)
    if rate_unit:
        statement = statement.where(DCValuationRate.rate_unit == rate_unit)
    if schedule_type in {"dc", "fbr"}:
        statement = statement.where(DCValuationRate.schedule_type == schedule_type)
    if land_type in {"rural", "urban"}:
        statement = statement.where(PropertyLocation.land_type == land_type)
    aggregate = select(
        func.count(DCValuationRate.id),
        func.min(DCValuationRate.rate_value),
        func.max(DCValuationRate.rate_value),
    ).where(DCValuationRate.id.in_(statement))
    with SessionLocal() as session:
        count, lowest, highest = session.execute(aggregate).one()
        if not count:
            return {
                "count": 0, "lowest": None, "highest": None,
                "lowest_square": None, "highest_square": None,
                "lowest_kila": None, "highest_kila": None,
            }
        cadastral_rows = session.execute(
            select(PropertyLocation.square_no, PropertyLocation.kila_no)
            .join(DCValuationRate, DCValuationRate.location_id == PropertyLocation.id)
            .where(DCValuationRate.id.in_(statement))
        ).all()
        lowest_record = session.scalar(
            select(DCValuationRate)
            .where(DCValuationRate.id.in_(statement))
            .order_by(DCValuationRate.rate_value.asc(), DCValuationRate.id.asc())
            .limit(1)
        )
        highest_record = session.scalar(
            select(DCValuationRate)
            .where(DCValuationRate.id.in_(statement))
            .order_by(DCValuationRate.rate_value.desc(), DCValuationRate.id.asc())
            .limit(1)
        )
    def cadastral_extremes(values: set[str]) -> tuple[str | None, str | None]:
        ordered = sorted(
            values,
            key=lambda token: (not token.isdigit(), int(token) if token.isdigit() else token.casefold()),
        )
        return (ordered[0], ordered[-1]) if ordered else (None, None)

    square_values: set[str] = set()
    kila_values: set[str] = set()
    for square_value, kila_value in cadastral_rows:
        try:
            square_values.update(_reference_values(square_value))
            kila_values.update(_reference_values(kila_value))
        except DCRateServiceError:
            # An invalid legacy range should not hide the saved rate summary.
            continue
    lowest_square, highest_square = cadastral_extremes(square_values)
    lowest_kila, highest_kila = cadastral_extremes(kila_values)
    return {
        "count": count, "lowest": lowest, "highest": highest,
        "lowest_square": lowest_square, "highest_square": highest_square,
        "lowest_kila": lowest_kila, "highest_kila": highest_kila,
        "lowest_unit": lowest_record.rate_unit,
        "lowest_schedule": lowest_record.schedule_type,
        "highest_unit": highest_record.rate_unit,
        "highest_schedule": highest_record.schedule_type,
    }


def create_location(**values: Any) -> tuple[int, bool]:
    """Return (location ID, created); exact duplicates reuse the location."""
    normalized = _location_data(values)
    with SessionLocal.begin() as session:
        for existing in session.scalars(select(PropertyLocation)).all():
            existing_data = {field: getattr(existing, field) for field in LOCATION_FIELDS}
            if _location_key(existing_data) == _location_key(normalized):
                return existing.id, False
        location = PropertyLocation(**normalized)
        session.add(location)
        session.flush()
        return location.id, True


def _duplicate_rate(
    session,
    rate_data: dict[str, Any],
    location_id: int,
    *,
    exclude_id: int | None = None,
) -> DCValuationRate | None:
    statement = select(DCValuationRate).where(
        DCValuationRate.location_id == location_id,
        DCValuationRate.rate_unit == rate_data["rate_unit"],
    )
    if exclude_id is not None:
        statement = statement.where(DCValuationRate.id != exclude_id)
    for existing in session.scalars(statement).all():
        if (
            existing.schedule_type == rate_data.get("schedule_type", "dc")
            and existing.property_type.casefold() == rate_data["property_type"].casefold()
            and (existing.classification or "").casefold()
            == (rate_data.get("classification") or "").casefold()
            and (existing.floor or "").casefold()
            == (rate_data.get("floor") or "").casefold()
            and _periods_overlap(
                existing.effective_from,
                existing.effective_to,
                rate_data["effective_from"],
                rate_data["effective_to"],
            )
        ):
            return existing
    return None


def create_dc_rate(*, location_id: int, **values: Any) -> int:
    rate_data = _rate_data(values)
    with SessionLocal.begin() as session:
        if session.get(PropertyLocation, location_id) is None:
            raise DCRateServiceError("Select a valid property location.")
        duplicate = _duplicate_rate(session, rate_data, location_id)
        if duplicate:
            raise DCRateServiceError(
                f"This rate conflicts with existing record #{duplicate.id}; "
                "existing rates are never overwritten."
            )
        record = DCValuationRate(location_id=location_id, **rate_data)
        session.add(record)
        session.flush()
        return record.id


def update_dc_rate(*, rate_id: int, location_id: int, **values: Any) -> None:
    rate_data = _rate_data(values)
    with SessionLocal.begin() as session:
        record = session.get(DCValuationRate, rate_id)
        if record is None:
            raise DCRateServiceError("DC rate record not found.")
        if session.get(PropertyLocation, location_id) is None:
            raise DCRateServiceError("Select a valid property location.")
        duplicate = _duplicate_rate(
            session, rate_data, location_id, exclude_id=rate_id
        )
        if duplicate:
            raise DCRateServiceError(
                f"This rate conflicts with existing record #{duplicate.id}; "
                "existing rates are never overwritten."
            )
        record.location_id = location_id
        for field, value in rate_data.items():
            setattr(record, field, value)


def get_dc_rate(rate_id: int) -> dict[str, Any] | None:
    with SessionLocal() as session:
        record = session.scalar(
            select(DCValuationRate)
            .options(joinedload(DCValuationRate.location))
            .where(DCValuationRate.id == rate_id)
        )
        return _record_dict(record) if record else None


def list_dc_rates(
    *,
    search: str = "",
    schedule_type: str | None = None,
    location_id: int | None = None,
    rate_unit: str | None = None,
    verification_status: str | None = None,
    effective_on: date | None = None,
    published_year: int | None = None,
    mauza: str = "",
    qanoongoi: str = "",
    square_no: str = "",
    kila_no: str = "",
    khasra_no: str = "",
    land_type: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    statement = select(DCValuationRate).options(joinedload(DCValuationRate.location))
    search = search.strip()
    if (mauza.strip() or qanoongoi.strip() or square_no.strip() or kila_no.strip()
            or khasra_no.strip() or land_type in {"rural", "urban"}
            or (search and not search.isdigit())):
        statement = statement.join(DCValuationRate.location)
    if schedule_type in {"dc", "fbr"}:
        statement = statement.where(DCValuationRate.schedule_type == schedule_type)
    if location_id is not None:
        statement = statement.where(DCValuationRate.location_id == location_id)
    if land_type in {"rural", "urban"}:
        statement = statement.where(PropertyLocation.land_type == land_type)
    if rate_unit:
        statement = statement.where(DCValuationRate.rate_unit == rate_unit)
    if verification_status in {"unverified", "verified"}:
        statement = statement.where(
            DCValuationRate.verification_status == verification_status
        )
    if effective_on:
        statement = statement.where(
            DCValuationRate.effective_from <= effective_on,
            or_(DCValuationRate.effective_to.is_(None), DCValuationRate.effective_to >= effective_on),
        )
    if published_year is not None:
        statement = statement.where(DCValuationRate.published_year == published_year)
    if mauza.strip():
        statement = statement.where(
            PropertyLocation.mauza.ilike(f"%{mauza.strip()}%")
        )
    statement = _apply_qanoongoi_filter(statement, qanoongoi)
    # Cadastral fields are stored as token lists; query candidates in SQLite,
    # then validate whole tokens below so (for example) 2 does not match 12.
    for field_name, term in (
        ("square_no", square_no), ("kila_no", kila_no), ("khasra_no", khasra_no)
    ):
        if term.strip():
            field = getattr(PropertyLocation, field_name)
            statement = statement.where(field.ilike(f"%{term.strip()}%"))
    if search and not search.isdigit():
        searchable_rate_fields = (
            DCValuationRate.property_type, DCValuationRate.classification,
            DCValuationRate.floor, DCValuationRate.schedule_type,
            DCValuationRate.source_reference,
        )
        searchable_location_fields = (
            PropertyLocation.name, PropertyLocation.district, PropertyLocation.tehsil,
            PropertyLocation.land_type, PropertyLocation.committee_town,
            PropertyLocation.revenue_circle, PropertyLocation.property_area,
            PropertyLocation.qanoongoi, PropertyLocation.patwar_circle,
            PropertyLocation.mauza, PropertyLocation.hadbast_number,
            PropertyLocation.serial_no, PropertyLocation.locality,
        )
        statement = statement.where(or_(
            *(field.ilike(f"%{search}%") for field in searchable_rate_fields),
            *(field.ilike(f"%{search}%") for field in searchable_location_fields),
            PropertyLocation.square_no.ilike(f"%{search}%"),
            PropertyLocation.kila_no.ilike(f"%{search}%"),
            PropertyLocation.khasra_no.ilike(f"%{search}%"),
        ))
    statement = statement.order_by(
        DCValuationRate.effective_from.desc(), DCValuationRate.id.desc()
    )
    # Whole-token reference validation and legacy range expansion happen in
    # Python below, so paginate those filtered results only after validation.
    has_reference_filter = any((square_no.strip(), kila_no.strip(), khasra_no.strip()))
    sql_pagination = limit is not None and not has_reference_filter
    apply_sql_pagination = sql_pagination and (not search or not search.isdigit())
    with SessionLocal() as session:
        if apply_sql_pagination:
            statement = statement.limit(max(1, limit)).offset(max(0, offset))
        rows = session.scalars(statement).unique().all()
        if not search and not has_reference_filter:
            return [_record_dict(row) for row in rows]

        search_key = search.casefold()
        reference_fields = ("square_no", "kila_no", "khasra_no")
        matches: list[dict[str, Any]] = []
        for row in rows:
            record = _record_dict(row)
            try:
                square_match = not square_no.strip() or bool(set(_reference_values(square_no)).intersection(
                    _reference_values(record.get("square_no"))
                ))
                kila_match = not kila_no.strip() or bool(set(_reference_values(kila_no)).intersection(
                    _reference_values(record.get("kila_no"))
                ))
                khasra_match = not khasra_no.strip() or bool(set(_reference_values(khasra_no)).intersection(
                    _reference_values(record.get("khasra_no"))
                ))
            except DCRateServiceError:
                square_match = kila_match = khasra_match = False
            if not square_match or not kila_match or not khasra_match:
                continue
            # Ordinary fields retain the existing case-insensitive substring
            # search. Cadastral identifiers match complete expanded tokens,
            # including values represented as ranges in older database rows.
            text_values = [
                record.get(field) for field in (
                    "property_type", "classification", "floor", "schedule_type",
                    "source_reference", "location_name", "district", "tehsil",
                    "land_type", "committee_town", "revenue_circle", "property_area",
                    "qanoongoi", "patwar_circle", "mauza", "hadbast_number", "serial_no",
                    "locality",
                )
            ]
            if search_key and any(search_key in str(value).casefold() for value in text_values if value):
                matches.append(record)
                continue
            try:
                requested_tokens = set(_reference_values(search))
                found = any(
                    requested_tokens.intersection(_reference_values(record.get(field)))
                    for field in reference_fields
                )
            except DCRateServiceError:
                found = False
            if not search_key or found:
                matches.append(record)
        if apply_sql_pagination:
            return matches
        start = max(0, offset)
        return matches[start : start + limit] if limit is not None else matches


def _header_key(value: Any) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().casefold())
    return normalized.strip("_")


def read_schedule_spreadsheet(file_bytes: bytes, filename: str) -> tuple[list[str], list[list[Any]]]:
    """Read tabular values from CSV, XLSX or ODS without changing source data."""
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension == "csv":
        try:
            lines = file_bytes.decode("utf-8-sig").splitlines()
            rows = list(csv.reader(lines))
        except (UnicodeDecodeError, csv.Error) as exc:
            raise DCRateServiceError(f"Could not read CSV file: {exc}") from exc
    elif extension == "xlsx":
        try:
            workbook = load_workbook(BytesIO(file_bytes), read_only=True, data_only=True)
            rows = list(workbook.active.iter_rows(values_only=True))
            workbook.close()
        except Exception as exc:
            raise DCRateServiceError(f"Could not open Excel workbook: {exc}") from exc
    elif extension == "ods":
        try:
            from odf.opendocument import load as load_ods
            from odf.table import Table, TableCell, TableRow
            from odf.text import P
            from odf import teletype
            document = load_ods(BytesIO(file_bytes))
            sheets = document.spreadsheet.getElementsByType(Table)
            if not sheets:
                raise DCRateServiceError("The ODS file has no worksheet.")
            rows = []
            for row in sheets[0].getElementsByType(TableRow):
                values = []
                for cell in row.getElementsByType(TableCell):
                    repeat = int(cell.getAttribute("numbercolumnsrepeated") or 1)
                    raw = cell.getAttribute("datevalue") or cell.getAttribute("value")
                    if raw is None:
                        raw = teletype.extractText(cell)
                    values.extend([raw] * min(repeat, 256))
                rows.append(values)
        except DCRateServiceError:
            raise
        except Exception as exc:
            raise DCRateServiceError(f"Could not open ODS workbook: {exc}") from exc
    else:
        raise DCRateServiceError("Choose a CSV, XLSX, or ODS spreadsheet.")
    rows = [list(row) for row in rows if any(value is not None and str(value).strip() for value in row)]
    if not rows:
        raise DCRateServiceError("The selected spreadsheet is empty.")
    headers = [str(value or "").strip() for value in rows[0]]
    return headers, rows[1:]


def suggested_column_mapping(headers: list[str]) -> dict[str, int]:
    """Suggest mappings from familiar schedule headers; user confirms them."""
    mapping = {}
    for index, header in enumerate(headers):
        target = RATE_HEADERS.get(_header_key(header))
        if _header_key(header) == "location":
            target = "locality"
        if target and target not in mapping:
            mapping[target] = index
    # The current official schedule exports usually expose Mauza and Location,
    # but not a separate display-name column for the database location record.
    if "location_name" not in mapping:
        if "mauza" in mapping:
            mapping["location_name"] = mapping["mauza"]
        elif "locality" in mapping:
            mapping["location_name"] = mapping["locality"]
    return mapping


def preview_mapped_schedule(
    headers: list[str], rows: list[list[Any]], mapping: dict[str, int], schedule_type: str
) -> dict[str, Any]:
    """Normalize a rural/urban DC or FBR profile and validate it without writes."""
    profiles = {"rural_dc": "rural", "urban_dc": "urban", "fbr": "urban"}
    if schedule_type not in profiles:
        raise DCRateServiceError("Select Rural DC, Urban DC, or FBR schedule.")
    land_type = profiles[schedule_type]
    normalized_schedule = "fbr" if schedule_type == "fbr" else "dc"
    mapping = dict(mapping)
    # Schedule profile controls these fields; source-sheet columns with the
    # same names cannot override the explicit radio selection.
    mapping.pop("schedule_type", None)
    mapping.pop("land_type", None)
    # In many published schedules the only class column is Land Classification.
    # It also supplies the broad property type when a separate column is absent.
    if "property_type" not in mapping and "classification" in mapping:
        mapping["property_type"] = mapping["classification"]
    if "location_name" not in mapping:
        if "mauza" in mapping:
            mapping["location_name"] = mapping["mauza"]
        elif "locality" in mapping:
            mapping["location_name"] = mapping["locality"]
    required_by_profile = {
        "rural_dc": {"tehsil", "qanoongoi", "mauza", "classification", "locality", "rate_value", "rate_unit"},
        "urban_dc": {"tehsil", "committee_town", "revenue_circle", "property_area", "classification", "locality", "rate_value", "rate_unit", "structure_rate"},
        "fbr": {"revenue_circle", "property_area", "classification", "locality", "rate_value"},
    }
    missing = required_by_profile[schedule_type] - mapping.keys()
    if schedule_type in {"rural_dc", "fbr"} and not {"square_no", "kila_no", "khasra_no"}.intersection(mapping):
        missing.add("at least one of square_no, kila_no, or khasra_no")
    if missing:
        raise DCRateServiceError("Map required columns for this schedule: " + ", ".join(sorted(missing)))
    if "location_name" not in mapping:
        if "mauza" in mapping:
            mapping["location_name"] = mapping["mauza"]
        elif "locality" in mapping:
            mapping["location_name"] = mapping["locality"]
    if "rate_unit" not in mapping:
        # The FBR template defines this amount as value per square foot.
        mapping["rate_unit"] = -1
    # Report blank cells in required profile fields by original spreadsheet
    # row number. Optional fields remain nullable and can be omitted.
    row_issues = []
    valid_rows = []
    required_targets = required_by_profile[schedule_type]
    for row_number, row in enumerate(rows, start=2):
        empty = [
            field for field in required_targets
            if field in mapping
            and (mapping[field] == -1 or mapping[field] >= len(row) or _text(row[mapping[field]]) is None)
        ]
        if schedule_type in {"rural_dc", "fbr"}:
            def has_value(field: str) -> bool:
                index = mapping.get(field)
                return index is not None and index >= 0 and index < len(row) and _text(row[index]) is not None

            # A Khasra-based record may legitimately omit square and Kila.
            # Rural rows otherwise need both; FBR rows need a square number.
            if has_value("khasra_no"):
                empty = [field for field in empty if field not in {"square_no", "kila_no"}]
            elif schedule_type == "rural_dc" and (not has_value("square_no") or not has_value("kila_no")):
                empty.extend(field for field in ("square_no", "kila_no") if not has_value(field) and field not in empty)
            elif schedule_type == "fbr" and not has_value("square_no"):
                empty.append("square_no")
        if empty:
            row_issues.append({
                "row": row_number,
                "kind": "invalid",
                "message": "Required value(s) are blank: " + ", ".join(sorted(empty)),
                "source_values": ["" if value is None else str(value) for value in row],
                "fix": "Fill the missing required value or select the correct spreadsheet column mapping.",
            })
        else:
            valid_rows.append(row)
    output = BytesIO()
    workbook = Workbook()
    sheet = workbook.active
    fields = list(mapping) + ["schedule_type", "land_type"]
    sheet.append(fields)
    for row in valid_rows:
        sheet.append([
            *[
                "per_square_foot" if index == -1 else row[index] if index < len(row) else None
                for index in mapping.values()
            ],
            normalized_schedule,
            land_type,
        ])
    workbook.save(output)
    workbook.close()
    preview = preview_dc_rate_workbook(output.getvalue())
    preview["issues"] = sorted([*row_issues, *preview["issues"]], key=lambda issue: issue["row"])
    preview["rows_read"] = len(rows)
    return preview


def preview_dc_rate_workbook(file_bytes: bytes) -> dict[str, Any]:
    """Validate an XLSX sheet and report invalid/duplicate rows without writes."""
    try:
        workbook = load_workbook(BytesIO(file_bytes), read_only=True, data_only=True)
    except Exception as exc:
        raise DCRateServiceError(f"Could not open this Excel workbook: {exc}") from exc
    try:
        sheet = workbook.active
        iterator = sheet.iter_rows(values_only=True)
        try:
            header_row = next(iterator)
        except StopIteration:
            raise DCRateServiceError("The selected worksheet is empty.") from None

        columns: dict[str, int] = {}
        duplicate_headers = []
        for index, value in enumerate(header_row):
            raw_header = _header_key(value)
            if not raw_header:
                continue
            target = RATE_HEADERS.get(raw_header)
            if target:
                if target in columns:
                    duplicate_headers.append(target)
                columns[target] = index
        if duplicate_headers:
            raise DCRateServiceError(
                "The worksheet contains duplicate columns: "
                + ", ".join(sorted(set(duplicate_headers)))
            )
        missing = REQUIRED_RATE_COLUMNS - columns.keys()
        if "classification" in columns:
            missing.discard("property_type")
        if missing:
            raise DCRateServiceError(
                "Missing required columns: " + ", ".join(sorted(missing))
            )

        candidates: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []
        seen: dict[tuple[Any, ...], list[tuple[date, date | None, int]]] = {}
        rows_read = 0
        for excel_row, values in enumerate(iterator, start=2):
            if not any(value is not None and str(value).strip() for value in values):
                continue
            rows_read += 1
            raw = {
                field: values[index] if index < len(values) else None
                for field, index in columns.items()
            }
            try:
                location = _location_data({
                    "name": raw.get("location_name"),
                    "serial_no": raw.get("serial_no"),
                    "province": raw.get("province"),
                    "district": raw.get("district"),
                    "tehsil": raw.get("tehsil"),
                    "land_type": raw.get("land_type"),
                    "committee_town": raw.get("committee_town"),
                    "revenue_circle": raw.get("revenue_circle"),
                    "property_area": raw.get("property_area"),
                    "qanoongoi": raw.get("qanoongoi"),
                    "patwar_circle": raw.get("patwar_circle"),
                    "mauza": raw.get("mauza"),
                    "hadbast_number": raw.get("hadbast_number"),
                    "square_no": raw.get("square_no"),
                    "kila_no": raw.get("kila_no"),
                    "khasra_no": raw.get("khasra_no"),
                    "locality": raw.get("locality"),
                })
                rate = _rate_data(raw)
                identity = _rate_identity(rate, _location_key(location))
                scope = identity[:6]
                conflict_row = next(
                    (
                        prior_row
                        for prior_start, prior_end, prior_row in seen.get(scope, [])
                        if _periods_overlap(
                            prior_start,
                            prior_end,
                            rate["effective_from"],
                            rate["effective_to"],
                        )
                    ),
                    None,
                )
                if conflict_row is not None:
                    issues.append({
                        "row": excel_row,
                        "kind": "duplicate in workbook",
                        "message": f"Conflicting effective period with row {conflict_row} in this workbook.",
                        "source_values": ["" if value is None else str(value) for value in values],
                    })
                    continue
                seen.setdefault(scope, []).append(
                    (rate["effective_from"], rate["effective_to"], excel_row)
                )
                candidates.append({
                    "row": excel_row,
                    "location": location,
                    "source_values": ["" if value is None else str(value) for value in values],
                    **rate,
                })
            except DCRateServiceError as exc:
                issues.append({
                    "row": excel_row,
                    "kind": "invalid",
                    "message": str(exc),
                    "source_values": ["" if value is None else str(value) for value in values],
                })

        with SessionLocal() as session:
            existing_keys: list[tuple[tuple[Any, ...], int]] = []
            locations = session.scalars(select(PropertyLocation)).all()
            location_by_id = {location.id: location for location in locations}
            rates = session.scalars(select(DCValuationRate)).all()
            for rate in rates:
                location = location_by_id.get(rate.location_id)
                if location is not None:
                    location_values = {
                        field: getattr(location, field) for field in LOCATION_FIELDS
                    }
                    existing_keys.append(
                        (
                            _rate_identity(
                                {
                                    "schedule_type": rate.schedule_type,
                                    "property_type": rate.property_type,
                                    "classification": rate.classification,
                                    "floor": rate.floor,
                                    "rate_unit": rate.rate_unit,
                                    "effective_from": rate.effective_from,
                                    "effective_to": rate.effective_to,
                                },
                                _location_key(location_values),
                            ),
                            rate.id,
                        )
                    )

        new_candidates = []
        for candidate in candidates:
            identity = _rate_identity(candidate, _location_key(candidate["location"]))
            conflict_id = next(
                (
                    rate_id
                    for existing_key, rate_id in existing_keys
                    if existing_key[:6] == identity[:6]
                    and _periods_overlap(
                        existing_key[6],
                        existing_key[7],
                        identity[6],
                        identity[7],
                    )
                ),
                None,
            )
            if conflict_id is not None:
                issues.append({
                    "row": candidate["row"],
                    "kind": "duplicate in database",
                    "message": (
                        f"Conflicts with existing record #{conflict_id} for the "
                        "same location, property class, unit, and effective period."
                    ),
                    "source_values": candidate.get("source_values", []),
                })
                continue
            new_candidates.append(candidate)
        for issue in issues:
            issue.setdefault("fix", _import_fix_suggestion(issue["message"], issue["kind"]))
        return {
            "valid_records": new_candidates,
            "issues": sorted(issues, key=lambda issue: issue["row"]),
            "rows_read": rows_read,
        }
    finally:
        workbook.close()


def import_dc_rate_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Batch-insert previewed rows while checking conflicts again at commit.

    The preview has already reported invalid input and existing conflicts. This
    final check protects against records added between preview and import, but
    builds location/rate indexes once instead of querying and flushing per row.
    """
    issues: list[dict[str, Any]] = []
    with SessionLocal.begin() as session:
        # Cache all known locations by their normalized identity so thousands
        # of rows do not repeatedly scan/query the table.
        location_by_key = {
            _location_key({field: getattr(location, field) for field in LOCATION_FIELDS}): location
            for location in session.scalars(select(PropertyLocation)).all()
        }
        prepared: list[tuple[dict[str, Any], dict[str, Any], tuple[str, ...], PropertyLocation]] = []
        new_locations: list[PropertyLocation] = []
        for candidate in records:
            row = candidate["row"]
            location_values = _location_data(candidate["location"])
            rate_values = _rate_data(candidate)
            location_key = _location_key(location_values)
            location = location_by_key.get(location_key)
            if location is None:
                location = PropertyLocation(**location_values)
                location_by_key[location_key] = location
                new_locations.append(location)
            prepared.append((candidate, rate_values, location_key, location))

        if new_locations:
            session.add_all(new_locations)
            # One flush assigns IDs for all newly created locations.
            session.flush()

        # Index effective periods for each rate identity. This catches both
        # concurrent inserts made after preview and duplicate rows in the
        # submitted batch without a SQL query for every candidate.
        location_by_id = {location.id: location for location in location_by_key.values()}
        rate_periods: dict[tuple[Any, ...], list[tuple[date, date | None, int | None]]] = {}
        for rate in session.scalars(select(DCValuationRate)).all():
            location = location_by_id.get(rate.location_id)
            if location is None:
                continue
            location_values = {field: getattr(location, field) for field in LOCATION_FIELDS}
            rate_identity = _rate_identity({
                "schedule_type": rate.schedule_type,
                "property_type": rate.property_type,
                "classification": rate.classification,
                "floor": rate.floor,
                "rate_unit": rate.rate_unit,
                "effective_from": rate.effective_from,
                "effective_to": rate.effective_to,
            }, _location_key(location_values))
            rate_periods.setdefault(rate_identity[:6], []).append(
                (rate.effective_from, rate.effective_to, rate.id)
            )

        new_rates: list[DCValuationRate] = []
        for candidate, rate_values, location_key, location in prepared:
            rate_identity = _rate_identity(rate_values, location_key)
            conflicts = [
                existing_id
                for start, end, existing_id in rate_periods.get(rate_identity[:6], [])
                if _periods_overlap(start, end, rate_identity[6], rate_identity[7])
            ]
            if conflicts:
                existing_id = conflicts[0]
                conflict_description = (
                    f"existing record #{existing_id}" if existing_id is not None
                    else "another row in this import"
                )
                issues.append({
                    "row": candidate["row"],
                    "kind": "duplicate at import time",
                    "message": f"Conflicts with {conflict_description}; skipped.",
                })
                continue
            record = DCValuationRate(location_id=location.id, **rate_values)
            new_rates.append(record)
            rate_periods.setdefault(rate_identity[:6], []).append(
                (rate_identity[6], rate_identity[7], None)
            )
        if new_rates:
            session.add_all(new_rates)
            session.flush()
    return {"imported_count": len(new_rates), "issues": issues}
