"""Validation and persistence for DC valuation rates and Excel imports."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO
import csv
import itertools
import os
import sqlite3
import sys
import re
from pathlib import Path
from typing import Any, Sequence

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
from services import dc_rate_duplicate_service as duplicate_rules


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
    "mouza": "mauza",
    "mauza_name": "mauza",
    "mouza_name": "mauza",
    "village": "mauza",
    "village_name": "mauza",
    "mauza_village": "mauza",
    "village_mauza": "mauza",
    "hadbast": "hadbast_number",
    "hadbast_number": "hadbast_number",
    "square_no": "square_no",
    "square_number": "square_no",
    "kila_no": "kila_no",
    "killa_no": "kila_no",
    "killa_number": "kila_no",
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
    "effective_to": "effective_to",
    "published_year": "published_year",
    "publish_year": "published_year",
    "schedule_year": "published_year",
    "year": "published_year",
    "source": "source_reference",
    "source_reference": "source_reference",
    "source_date": "source_date",
    "verification_status": "verification_status",
    "verified_by": "verified_by",
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


REFERENCE_STORAGE_MODES = ("ranges", "individual")


def _canonical_reference(value: Any) -> str | None:
    """Compact Square/Kila/Khasra input into canonical range segments.

    ``1-5;6;7-9,10,11-13,15`` becomes ``1-13, 15`` and reversed ranges are
    normalized. This is used when the user chooses to keep cadastral values as
    ranges in the database, avoiding one row per value.
    """
    normalized = _text(value)
    if not normalized:
        return None
    numeric: set[int] = set()
    other: list[str] = []
    for segment in re.split(r"[,;]", normalized):
        segment = segment.strip()
        if not segment:
            continue
        interval = re.fullmatch(r"(\d+)\s*-\s*(\d+)", segment)
        if interval:
            start, end = sorted(int(part) for part in interval.groups())
            if end - start > 100_000:
                raise DCRateServiceError(
                    "A square, Kila, or Khasra range cannot exceed 100,001 values."
                )
            numeric.update(range(start, end + 1))
        elif segment.isdigit():
            numeric.add(int(segment))
        elif segment not in other:
            other.append(segment)
    ranges: list[list[int]] = []
    for number in sorted(numeric):
        if ranges and ranges[-1][1] + 1 == number:
            ranges[-1][1] = number
        else:
            ranges.append([number, number])
    parts = [
        str(start) if start == end else f"{start}-{end}"
        for start, end in ranges
    ]
    parts.extend(other)
    return ", ".join(parts) if parts else None


_MAX_EXPORTED_ROWS = 250_000


MAX_EXPANDED_ROWS_PER_SOURCE = 5_000
MAX_EXPANDED_ROWS_PER_IMPORT = 250_000
_EXPANDABLE_REFERENCE_FIELDS = ("square_no", "kila_no", "khasra_no")


def _expand_location_values(
    location: dict[str, Any], *, source_row: int | None = None
) -> list[dict[str, Any]]:
    """Expand square/Kila/Khasra tokens into one record per combination.

    A missing field is treated as a single empty value, so a row with only a
    Khasra range still expands. Every non-cadastral value is copied unchanged.
    """
    token_lists: list[list[str | None]] = []
    for field in _EXPANDABLE_REFERENCE_FIELDS:
        tokens = _reference_values(location.get(field))
        token_lists.append(tokens or [None])
    combinations = 1
    for tokens in token_lists:
        combinations *= len(tokens)
    if combinations > MAX_EXPANDED_ROWS_PER_SOURCE:
        row_label = f"Row {source_row} " if source_row is not None else "This row "
        raise DCRateServiceError(
            f"{row_label}expands to {combinations:,} cadastral records. Split the "
            f"square, Kila, or Khasra range so one source row expands to at most "
            f"{MAX_EXPANDED_ROWS_PER_SOURCE:,} records."
        )
    expanded: list[dict[str, Any]] = []
    for combination in itertools.product(*token_lists):
        item = dict(location)
        for field, value in zip(_EXPANDABLE_REFERENCE_FIELDS, combination):
            item[field] = value
        expanded.append(item)
    return expanded


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


def _location_data(
    values: dict[str, Any], *, reference_mode: str = "individual"
) -> dict[str, str | None]:
    name = _text(values.get("name", values.get("location_name")))
    if not name:
        raise DCRateServiceError("Location name is required.")
    location = {field: _text(values.get(field)) for field in LOCATION_FIELDS}
    location["name"] = name
    if reference_mode not in REFERENCE_STORAGE_MODES:
        raise DCRateServiceError(
            "Cadastral storage must be 'ranges' or 'individual'."
        )
    for field in ("square_no", "kila_no", "khasra_no"):
        if reference_mode == "ranges":
            # Keep a compact range such as "1-5, 8" instead of expanding it.
            location[field] = _canonical_reference(values.get(field))
        else:
            # Legacy/requested mode: store every token for direct matching.
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
    if start is None:
        # effective_from is retained as a required legacy storage column. A
        # missing schedule year remains NULL so later valuation can flag it.
        start = date(year, 1, 1) if year is not None else date.today()
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
        "name": location.name if location else "Unassigned",
        "location_name": location.name if location else "Unassigned",
        "location_notes": location.notes if location else None,
        "notes": location.notes if location else None,
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


def _flat_rate_record(
    rate_values: dict[str, Any], location_values: dict[str, Any]
) -> dict[str, Any]:
    """Combine normalized location and schedule values for duplicate matching."""
    return {**location_values, **rate_values}


def list_duplicate_rule_profiles(
    *, schedule_scope: str | None = None
) -> list[dict[str, Any]]:
    """Return selectable built-in and saved custom duplicate rules."""
    with SessionLocal() as session:
        return [
            duplicate_rules.profile_to_dict(profile)
            for profile in duplicate_rules.list_profiles(
                session, schedule_scope=schedule_scope
            )
        ]


def create_duplicate_rule_profile(
    *,
    name: str,
    schedule_scope: str,
    match_fields: list[str],
    date_mode: str,
    import_action: str,
    description: str | None = None,
) -> dict[str, Any]:
    """Create and save a custom duplicate-rule profile."""
    with SessionLocal.begin() as session:
        profile = duplicate_rules.create_custom_profile(
            session,
            name=name,
            schedule_scope=schedule_scope,
            match_fields=match_fields,
            date_mode=date_mode,
            import_action=import_action,
            description=description,
        )
        return duplicate_rules.profile_to_dict(profile)


def update_duplicate_rule_profile(
    profile_id: int,
    *,
    name: str,
    schedule_scope: str,
    match_fields: list[str],
    date_mode: str,
    import_action: str,
    description: str | None = None,
) -> dict[str, Any]:
    """Update and save a custom duplicate-rule profile."""
    with SessionLocal.begin() as session:
        profile = duplicate_rules.update_custom_profile(
            session,
            profile_id,
            name=name,
            schedule_scope=schedule_scope,
            match_fields=match_fields,
            date_mode=date_mode,
            import_action=import_action,
            description=description,
        )
        return duplicate_rules.profile_to_dict(profile)


def delete_duplicate_rule_profile(profile_id: int) -> None:
    """Delete a custom duplicate-rule profile."""
    with SessionLocal.begin() as session:
        duplicate_rules.delete_custom_profile(session, profile_id)


def find_duplicate_dc_rates(duplicate_rule: str) -> list[dict[str, Any]]:
    """List rows removed by the selected rule, preserving the lowest record ID."""
    with SessionLocal() as session:
        profile = duplicate_rules.get_profile(session, duplicate_rule)
        rates = session.scalars(
            select(DCValuationRate)
            .options(joinedload(DCValuationRate.location))
            .order_by(DCValuationRate.id)
        ).unique().all()
        records_by_id = {rate.id: _record_dict(rate) for rate in rates}
        pairs = duplicate_rules.find_duplicate_pairs(
            list(records_by_id.values()), profile
        )
        duplicate_rows = []
        for survivor, duplicate in pairs:
            record = dict(duplicate)
            record["duplicate_of"] = survivor.get("id")
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


def remove_duplicate_dc_rates_with_report(
    file_format: str, duplicate_rule: str
) -> tuple[int, Path | None]:
    """Export rows selected by one rule, then remove the extra rows.

    If report generation fails, no database rows are deleted. The lowest ID in
    each duplicate group is retained, and SQLAlchemy's SET NULL policy keeps
    historical estimate snapshots valid when duplicate rate links are removed.
    """
    if file_format not in {"csv", "xlsx", "ods"}:
        raise DCRateServiceError("Choose CSV, Excel, or ODS for the duplicate report.")

    report_path: Path | None = None
    try:
        with SessionLocal.begin() as session:
            profile = duplicate_rules.get_profile(session, duplicate_rule)
            rates = session.scalars(
                select(DCValuationRate)
                .options(joinedload(DCValuationRate.location))
                .order_by(DCValuationRate.id)
            ).unique().all()
            records_by_id = {rate.id: _record_dict(rate) for rate in rates}
            pairs = duplicate_rules.find_duplicate_pairs(
                list(records_by_id.values()), profile
            )
            duplicates = [
                (survivor, rates_by_id)
                for survivor, duplicate in pairs
                for rates_by_id in (session.get(DCValuationRate, duplicate["id"]),)
                if rates_by_id is not None
            ]
            if not duplicates:
                return 0, None

            report_records = []
            for survivor, duplicate in duplicates:
                record = _record_dict(duplicate)
                record["duplicate_of"] = survivor.get("id")
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
    records: list[dict[str, Any]],
    file_format: str,
    fields: list[str] | None = None,
    *,
    reference_mode: str = "stored",
) -> bytes:
    """Serialize the selected schedule dimensions to CSV, XLSX, or ODS bytes.

    ``reference_mode`` controls Square/Kila/Khasra values:
    ``stored`` keeps database values unchanged, ``ranges`` compacts them into
    range segments, and ``individual`` expands each range into one exported row
    per combination. The export mode never changes stored data.
    """
    if reference_mode not in {"stored", "ranges", "individual"}:
        raise DCRateServiceError(
            "Export reference mode must be 'stored', 'ranges', or 'individual'."
        )
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

    export_records: list[dict[str, Any]] = []
    for record in records:
        if reference_mode == "stored":
            export_records.append(record)
            continue
        if reference_mode == "ranges":
            transformed = dict(record)
            for field in _EXPANDABLE_REFERENCE_FIELDS:
                transformed[field] = _canonical_reference(record.get(field))
            export_records.append(transformed)
            continue
        # Expand the range into individual rows while keeping every other
        # column (including the same rate) on each exported row.
        for expanded in _expand_location_values(record):
            export_records.append({**record, **{
                field: expanded.get(field) for field in _EXPANDABLE_REFERENCE_FIELDS
            }})
        if len(export_records) > _MAX_EXPORTED_ROWS:
            raise DCRateServiceError(
                f"This export expands to more than {_MAX_EXPORTED_ROWS:,} rows. "
                "Export fewer records or keep ranges."
            )

    rows = [[cell(record, key) for key in selected] for record in export_records]
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


def export_import_issues(issues: list[dict[str, Any]]) -> bytes:
    """Return a UTF-8 CSV report of skipped/invalid/deduplicated import rows.

    Every reported source row keeps its worksheet name (when available),
    original row number, issue kind, reason, suggested fix, and raw values so
    nothing is silently lost during an import.
    """
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([
        "Worksheet", "Source row", "Issue", "Reason", "Suggested fix",
        "Source values",
    ])
    for issue in issues:
        source_values = issue.get("source_values") or []
        if isinstance(source_values, (list, tuple)):
            source_text = " | ".join("" if value is None else str(value) for value in source_values)
        else:
            source_text = str(source_values)
        writer.writerow([
            issue.get("sheet") or "",
            issue.get("row") or "",
            issue.get("kind") or "",
            issue.get("message") or "",
            issue.get("fix") or "",
            source_text,
        ])
    return output.getvalue().encode("utf-8-sig")



LOCATION_HINT_FIELDS = (
    "district",
    "tehsil",
    "mauza",
    "committee_town",
    "revenue_circle",
    "property_area",
    "locality",
    "square_no",
    "kila_no",
    "khasra_no",
)
_LOCATION_HINT_COLUMNS = {
    "district": PropertyLocation.district,
    "tehsil": PropertyLocation.tehsil,
    "mauza": PropertyLocation.mauza,
    "committee_town": PropertyLocation.committee_town,
    "revenue_circle": PropertyLocation.revenue_circle,
    "property_area": PropertyLocation.property_area,
    "locality": PropertyLocation.locality,
    "square_no": PropertyLocation.square_no,
    "kila_no": PropertyLocation.kila_no,
    "khasra_no": PropertyLocation.khasra_no,
}
_LOCATION_REFERENCE_FIELDS = frozenset({"square_no", "kila_no", "khasra_no"})


def _location_dict(location: PropertyLocation) -> dict[str, Any]:
    return {
        "id": location.id,
        **{field: getattr(location, field) for field in LOCATION_FIELDS},
    }


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
        return [_location_dict(location) for location in locations]


def list_location_picker(
    *, limit: int = 500, include_ids: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Bounded location data for the rate-page dropdowns.

    The location table can hold hundreds of thousands of rows, so pages load
    only the first `limit` records in display order and report whether more
    remain (`has_more`). Records named in `include_ids` are always attached so
    a just-created or currently selected location is never truncated away.
    """
    limit = max(0, int(limit))
    wanted_ids = {int(value) for value in (include_ids or []) if value is not None}
    with SessionLocal() as session:
        fetched = session.scalars(
            select(PropertyLocation)
            .order_by(
                PropertyLocation.district,
                PropertyLocation.tehsil,
                PropertyLocation.name,
                PropertyLocation.id,
            )
            .limit(limit + 1)
        ).all()
        has_more = len(fetched) > limit
        locations = fetched[:limit]
        missing_ids = sorted(wanted_ids - {location.id for location in locations})
        if missing_ids:
            locations = [*locations, *session.scalars(
                select(PropertyLocation)
                .where(PropertyLocation.id.in_(missing_ids))
                .order_by(
                    PropertyLocation.district,
                    PropertyLocation.tehsil,
                    PropertyLocation.name,
                    PropertyLocation.id,
                )
            ).all()]
        distinct_values = session.scalars(
            select(func.distinct(PropertyLocation.qanoongoi)).where(
                PropertyLocation.qanoongoi.is_not(None),
                PropertyLocation.qanoongoi != "",
            )
        ).all()
    qanoongoi: dict[str, str] = {}
    for raw_value in distinct_values:
        value = " ".join(str(raw_value or "").split())
        if value:
            qanoongoi.setdefault(value.casefold(), value)
    return {
        "locations": [_location_dict(location) for location in locations],
        "qanoongoi": sorted(qanoongoi.values(), key=str.casefold),
        "has_more": has_more,
    }


def _apply_location_land_type(statement, land_type: str | None):
    """Apply the calculator's rural/urban convention to a location query."""
    if land_type in {"rural", "urban"}:
        # Legacy locations without a type were treated as urban by the page.
        statement = statement.where(
            func.lower(
                func.trim(func.coalesce(PropertyLocation.land_type, "urban"))
            ) == land_type
        )
    return statement


def _apply_location_context(statement, context: dict[str, Any] | None):
    """Apply non-empty location inputs using the same substring semantics as hints."""
    for field_name, raw_value in (context or {}).items():
        value = _text(raw_value)
        if not value or field_name not in _LOCATION_HINT_COLUMNS:
            continue
        pattern = f"%{value}%"
        if field_name == "locality":
            statement = statement.where(or_(
                PropertyLocation.locality.ilike(pattern),
                PropertyLocation.name.ilike(pattern),
            ))
        else:
            statement = statement.where(
                _LOCATION_HINT_COLUMNS[field_name].ilike(pattern)
            )
    return statement


def _apply_location_hint_search(statement, field_name: str, raw_value: Any):
    value = _text(raw_value)
    if not value:
        return statement
    pattern = f"%{value}%"
    if field_name == "locality":
        return statement.where(or_(
            PropertyLocation.locality.ilike(pattern),
            PropertyLocation.name.ilike(pattern),
        ))
    return statement.where(_LOCATION_HINT_COLUMNS[field_name].ilike(pattern))


def _location_matches_context(location: dict[str, Any], context: dict[str, Any]) -> bool:
    """Validate a candidate with exact cadastral tokens after broad SQL filtering."""
    for field_name, raw_value in context.items():
        value = _text(raw_value)
        if not value or field_name not in _LOCATION_HINT_COLUMNS:
            continue
        if field_name == "locality":
            if not any(
                value.casefold() in str(location.get(field) or "").casefold()
                for field in ("locality", "name")
            ):
                return False
            continue
        if field_name in _LOCATION_REFERENCE_FIELDS:
            try:
                requested = set(_reference_values(value))
                stored = set(_reference_values(location.get(field_name)))
            except DCRateServiceError:
                return False
            if not requested.intersection(stored):
                return False
            continue
        if value.casefold() not in str(location.get(field_name) or "").casefold():
            return False
    return True


def list_location_hint_values(
    *,
    land_type: str | None = None,
    context: dict[str, Any] | None = None,
    fields: list[str] | tuple[str, ...] | None = None,
    limit: int = 100,
) -> dict[str, list[str]]:
    """Return schedule-backed type-ahead values from the current database."""
    requested = [
        field for field in (fields or LOCATION_HINT_FIELDS)
        if field in _LOCATION_HINT_COLUMNS
    ]
    context = dict(context or {})
    hint_limit = max(1, limit)
    result: dict[str, list[str]] = {}
    with SessionLocal() as session:
        for field_name in requested:
            # A stale child value must not hide valid Square choices.
            field_context = {
                key: value for key, value in context.items()
                if key != field_name
                and not (field_name == "square_no" and key == "kila_no")
            }
            search_value = context.get(field_name)
            if field_name == "locality":
                statements = []
                for column in (PropertyLocation.locality, PropertyLocation.name):
                    statement = (
                        select(column.label("value"))
                        .join(
                            DCValuationRate,
                            DCValuationRate.location_id == PropertyLocation.id,
                        )
                        .where(column.is_not(None), func.trim(column) != "")
                    )
                    statement = _apply_location_land_type(statement, land_type)
                    statement = _apply_location_context(statement, field_context)
                    statement = _apply_location_hint_search(
                        statement, field_name, search_value
                    )
                    statements.append(statement)
                statement = statements[0].union(statements[1]).order_by("value")
                values = session.scalars(
                    statement.limit(hint_limit * 3)
                ).all()
            else:
                column = _LOCATION_HINT_COLUMNS[field_name]
                statement = (
                    select(column)
                    .join(
                        DCValuationRate,
                        DCValuationRate.location_id == PropertyLocation.id,
                    )
                    .where(column.is_not(None), func.trim(column) != "")
                )
                statement = _apply_location_land_type(statement, land_type)
                statement = _apply_location_context(statement, field_context)
                statement = _apply_location_hint_search(
                    statement, field_name, search_value
                )
                values = session.scalars(
                    statement.distinct().order_by(column).limit(hint_limit * 3)
                ).all()

            cleaned = []
            seen = set()
            for value in values:
                text_value = str(value or "").strip()
                key = text_value.casefold()
                if text_value and key not in seen:
                    seen.add(key)
                    cleaned.append(text_value)
            cleaned.sort(key=str.casefold)
            result[field_name] = cleaned[:hint_limit]
    return result


def find_schedule_location_matches(
    *,
    land_type: str | None = None,
    context: dict[str, Any] | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return schedule-backed locations matching the current calculator inputs."""
    context = dict(context or {})
    hint_limit = max(1, limit)
    statement = (
        select(PropertyLocation)
        .join(
            DCValuationRate,
            DCValuationRate.location_id == PropertyLocation.id,
        )
        .distinct()
    )
    statement = _apply_location_land_type(statement, land_type)
    statement = _apply_location_context(statement, context)
    statement = statement.order_by(
        PropertyLocation.district,
        PropertyLocation.tehsil,
        PropertyLocation.mauza,
        PropertyLocation.name,
        PropertyLocation.id,
    )
    with SessionLocal() as session:
        candidates = session.scalars(
            statement.limit(max(500, hint_limit * 5))
        ).unique().all()
    matches = []
    for location in candidates:
        record = _location_dict(location)
        if _location_matches_context(record, context):
            matches.append(record)
    return matches[:hint_limit]


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
    field_name: str, *, search: str = "", qanoongoi: str = "", mauza: str = "",
    square_no: str = "", kila_no: str = "", schedule_type: str | None = None,
    land_type: str | None = None, location_id: int | None = None, limit: int = 50,
) -> list[str]:
    """Return cadastral tokens from schedules matching the selected parent context.

    Suggestions feed the square/kila/khasra auto-complete boxes. Values stored
    as token lists or compact ranges (``1-5,6``) are expanded, so the caller
    receives single usable tokens like ``3`` even though the record itself
    stores ``1-5,6``.

    With a Mauza filter the query takes the same *locations-first* plan as the
    summary above: matched location ids are resolved once through the Mauza
    covering index and the fabricated ``IN`` condition keeps the rate join
    probes on ``ix_dc_valuation_rates_location_id`` instead of scanning the
    whole rate table per keystroke.

    Args:
        field_name: ``square_no``, ``kila_no``, or ``khasra_no`` — the column
            whose tokens are suggested.
        search: optional substring the suggestions must start with.
        qanoongoi / mauza / land_type: location-side parent filters.
        square_no / kila_no: optional sibling cadastral filters; rows must
            satisfy *whole-token* matches of these values (rows whose sibling
            range straddles the request are intentionally excluded here and
            handled by the rate listing).
        schedule_type: restrict to one schedule when given.
        location_id: restrict suggestions to one saved location.
        limit: maximum number of suggestions returned (at least 1).
    Raises:
        DCRateServiceError: for an unsupported field_name.
    """
    fields = {
        "square_no": PropertyLocation.square_no,
        "kila_no": PropertyLocation.kila_no,
        "khasra_no": PropertyLocation.khasra_no,
    }
    field = fields.get(field_name)
    if field is None:
        raise DCRateServiceError("Choose Square no., Kila no., or Khasra no. for suggestions.")
    # Sibling cadastral filters (e.g. a Kila chosen before asking for squares)
    # must hold for every returned stored value. Their whole-token validation
    # happens in Python below; the query stays a broad superset.
    active_contexts = [
        (filter_name, fields[filter_name], term)
        for filter_name, term in (("square_no", square_no), ("kila_no", kila_no))
        if filter_name != field_name and _text(term)
    ]
    statement = (
        select(field, *(column for _, column, _ in active_contexts))
        .join(DCValuationRate, DCValuationRate.location_id == PropertyLocation.id)
        .where(field.is_not(None), field != "")
        .distinct()
    )
    if mauza.strip():
        # Locations-first plan: resolve matching location ids once, then let
        # the rate join probe only those locations (comment detail in
        # _mauza_location_match).
        matched_ids = _resolve_mauza_location_ids(
            mauza, qanoongoi=qanoongoi, land_type=land_type,
        )
        statement = statement.where(
            DCValuationRate.location_id.in_(
                _match_locations_expression(
                    matched_ids,
                    _mauza_location_match(
                        mauza, qanoongoi=qanoongoi, land_type=land_type,
                    ),
                )
            )
        )
    statement = _apply_qanoongoi_filter(statement, qanoongoi)
    if schedule_type in {"dc", "fbr"}:
        statement = statement.where(DCValuationRate.schedule_type == schedule_type)
    if land_type in {"rural", "urban"} and not mauza.strip():
        # With a Mauza filter the land type is already applied inside the
        # matched-location query; a second (joined) condition would be
        # duplicated work and, without ids hoisted, the same scan.
        statement = statement.where(PropertyLocation.land_type == land_type)
    if location_id is not None:
        statement = statement.where(DCValuationRate.location_id == location_id)
    query = search.strip()
    if query:
        statement = statement.where(field.ilike(f"%{query}%"))
    requested_tokens = [set(_reference_values(term)) for _, _, term in active_contexts]
    with SessionLocal() as session:
        stored_rows = session.execute(statement).all()
    # Expand stored lists/ranges into single tokens and drop candidates whose
    # sibling cadastral values do not contain the requested tokens exactly.
    tokens = set()
    for stored_row in stored_rows:
        context_values = stored_row[1:]
        if any(
            not requested.intersection(_reference_values(stored_value))
            for requested, stored_value in zip(requested_tokens, context_values)
        ):
            continue
        for token in _reference_values(stored_row[0]):
            if not query or token.casefold().startswith(query.casefold()):
                tokens.add(token)
    # Numeric tokens sort numerically, everything else case-insensitively.
    return sorted(tokens, key=lambda token: (not token.isdigit(), int(token) if token.isdigit() else token.casefold()))[:max(1, limit)]


def _mauza_location_match(
    mauza: str, *, qanoongoi: str = "", land_type: str | None = None,
):
    """Build ``SELECT id FROM property_locations`` matching the Mauza filter.

    This is the *locations-first* plan that the Mauza-filtered queries below
    share. All location-side filters (Mauza substring, Qanoongoi, rural/urban
    type) are applied inside this location-only query so the remaining
    rate-side statements can reach matched locations through the covering
    ``ix_property_locations_mauza`` index instead of scanning the whole
    ``dc_valuation_rates`` table in a join across every location.

    Args:
        mauza: Mauza substring filter; the caller must ensure it is non-blank.
        qanoongoi: optional Qanoongoi substring filter (case-insensitive).
        land_type: optional ``"rural"``/``"urban"`` location type filter.
    Returns:
        A SQLAlchemy select of ``PropertyLocation.id`` rows.
    """
    statement = select(PropertyLocation.id).where(
        PropertyLocation.mauza.ilike(f"%{mauza.strip()}%")
    )
    statement = _apply_qanoongoi_filter(statement, qanoongoi)
    if land_type in {"rural", "urban"}:
        statement = statement.where(PropertyLocation.land_type == land_type)
    return statement


def _match_locations_expression(
    ids: list[int], location_match,
):
    """Wrap matched location ids for a rate-side ``IN`` condition.

    Small match sets become literal id lists: the repeated statements around a
    Mauza-filtered page (summary aggregate, cadastral extremes, first/last
    rate) then reuse SQLite's variable cache instead of re-running the
    location scan once per statement. Very large match sets keep the embedded
    subquery so the SQL stays within the backend's bind-parameter limit; the
    subquery form re-runs the location scan per statement but stays correct
    and bounded by the mauza covering index.
    """
    if len(ids) <= _LOCATION_ID_LITERAL_LIMIT:
        return ids
    return location_match


def _resolve_mauza_location_ids(
    mauza: str, *, qanoongoi: str = "", land_type: str | None = None,
) -> list[int]:
    """Resolve the Mauza filter to location ids with one locations-first query.

    Resolving the ids once per user action matters because the Mauza scan is
    the dominant cost (~40+ ms on a 295k-row table); downstream statements
    become trivial lookups. Callers that embed the ids in more than one
    statement must respect :data:`_LOCATION_ID_LITERAL_LIMIT` via
    :func:`_match_locations_expression`; this helper only reads the ids.
    """
    with SessionLocal() as session:
        return list(session.scalars(
            _mauza_location_match(
                mauza, qanoongoi=qanoongoi, land_type=land_type,
            )
        ).all())


# Matched location id sets up to this size are passed as literal SQL ids so the
# repeated statements (summary aggregate, cadastral extremes, first/last rate)
# reuse the SQLite variable cache instead of re-running a large location scan.
# The bound respects the backend's bind-parameter limit: SQLite >= 3.32 allows
# 32,766 parameters, older builds default to 999. Bigger match sets fall back to
# an embedded location subquery.
_SQLITE_BIND_PARAMETER_LIMIT = (
    32766 if sqlite3.sqlite_version_info >= (3, 32, 0) else 999
)
_LOCATION_ID_LITERAL_LIMIT = min(10000, _SQLITE_BIND_PARAMETER_LIMIT - 20)


def summarize_dc_rates(
    *, mauza: str = "", qanoongoi: str = "", rate_unit: str | None = None,
    schedule_type: str | None = None, land_type: str | None = None,
) -> dict[str, Any]:
    """Aggregate rate extremes for the selected location filters.

    With a Mauza filter the query takes the *locations-first* plan: matching
    location ids are resolved once (see :func:`_mauza_location_match`) and the
    rate aggregate, cadastral extraction, and the lowest/highest lookup all
    reuse that id set, so the dominant cost is one indexed Mauza scan instead
    of four rate-table joins per statement.
    """
    statement: Any = None
    if mauza.strip():
        # Locations-first plan (comment detail in _mauza_location_match): the
        # id set is resolved once here; each statement below then filters the
        # rate table by location id, which SQLite serves from the
        # ix_dc_valuation_rates_location_id index.
        location_match = _mauza_location_match(
            mauza, qanoongoi=qanoongoi, land_type=land_type,
        )
        with SessionLocal() as session:
            matched_ids = session.scalars(location_match).all()
        statement = select(DCValuationRate.id).where(
            DCValuationRate.location_id.in_(
                _match_locations_expression(matched_ids, location_match)
            )
        )
    else:
        statement = (
            select(DCValuationRate.id)
            .join(PropertyLocation, DCValuationRate.location_id == PropertyLocation.id)
        )
        statement = _apply_qanoongoi_filter(statement, qanoongoi)
        if land_type in {"rural", "urban"}:
            statement = statement.where(PropertyLocation.land_type == land_type)
    if rate_unit:
        statement = statement.where(DCValuationRate.rate_unit == rate_unit)
    if schedule_type in {"dc", "fbr"}:
        statement = statement.where(DCValuationRate.schedule_type == schedule_type)
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


def create_location(
    *, reference_mode: str = "individual", **values: Any
) -> tuple[int, bool]:
    """Return (location ID, created); exact duplicates reuse the location."""
    normalized = _location_data(values, reference_mode=reference_mode)
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


def _reference_sql_conditions(column, term: str) -> list[Any]:
    """Broad SQL candidate conditions for an individual or range reference.

    The Python pass below performs the exact token/range intersection. This
    helper must remain a superset so an overlapping range query such as ``2-4``
    is not excluded by the SQL prefilter before it can match stored ``1-3``.
    """
    term = term.strip()
    conditions: list[Any] = [column.ilike(f"%{term}%")]
    try:
        tokens = _reference_values(term)
    except DCRateServiceError:
        tokens = []
    bare = re.sub(r"[,;\s]", "", term)
    looks_numeric = bool(bare) and bool(re.fullmatch(r"\d+(?:-\d+)?", bare))
    for token in tokens:
        conditions.append(column.ilike(f"%{token}%"))
    if looks_numeric:
        # Stored range rows may contain the query value without literally
        # containing it (for example stored "1-9" queried with "5").
        conditions.append(column.like("%-%"))
    return conditions


def list_dc_rates(
    *,
    search: str = "",
    schedule_type: str | None = None,
    location_id: int | None = None,
    location_ids: list[int] | tuple[int, ...] | None = None,
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
    mauza_term = mauza.strip()
    cadastral_terms = (square_no.strip(), kila_no.strip(), khasra_no.strip())
    # With a Mauza filter and no free-text/cadastral search, match location
    # fields inside a location-id subquery: SQLite then probes rates through
    # their location index instead of joining every location into the scan.
    use_location_subquery = bool(
        mauza_term and not search and not any(cadastral_terms)
    )
    if use_location_subquery:
        location_match = select(PropertyLocation.id).where(
            PropertyLocation.mauza.ilike(f"%{mauza_term}%")
        )
        location_match = _apply_qanoongoi_filter(location_match, qanoongoi)
        if land_type in {"rural", "urban"}:
            location_match = location_match.where(
                PropertyLocation.land_type == land_type
            )
        statement = statement.where(DCValuationRate.location_id.in_(location_match))
    elif (mauza_term or qanoongoi.strip() or any(cadastral_terms)
            or land_type in {"rural", "urban"} or search):
        statement = statement.join(DCValuationRate.location)
    if schedule_type in {"dc", "fbr"}:
        statement = statement.where(DCValuationRate.schedule_type == schedule_type)
    if location_ids is not None:
        statement = statement.where(
            DCValuationRate.location_id.in_([int(value) for value in location_ids])
        )
    elif location_id is not None:
        statement = statement.where(DCValuationRate.location_id == location_id)
    if land_type in {"rural", "urban"} and not use_location_subquery:
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
    if not use_location_subquery:
        if mauza_term:
            statement = statement.where(
                PropertyLocation.mauza.ilike(f"%{mauza_term}%")
            )
        statement = _apply_qanoongoi_filter(statement, qanoongoi)
    # Cadastral fields are stored as token lists or compact ranges; query a
    # broad superset in SQLite, then validate whole tokens/ranges below so
    # (for example) 2 does not match 12 and overlapping ranges still match.
    for field_name, term in (
        ("square_no", square_no), ("kila_no", kila_no), ("khasra_no", khasra_no)
    ):
        if term.strip():
            field = getattr(PropertyLocation, field_name)
            statement = statement.where(or_(
                *_reference_sql_conditions(field, term)
            ))
    if search:
        # Numeric terms need the same SQL prefilter as text terms so a bulky
        # schedule is not loaded into Python for token validation. The Python
        # pass below still enforces exact cadastral tokens after this broad
        # substring candidate filter.
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
        reference_columns = (
            PropertyLocation.square_no, PropertyLocation.kila_no,
            PropertyLocation.khasra_no,
        )
        reference_conditions = []
        for column in reference_columns:
            reference_conditions.extend(_reference_sql_conditions(column, search))
        statement = statement.where(or_(
            *(field.ilike(f"%{search}%") for field in searchable_rate_fields),
            *(field.ilike(f"%{search}%") for field in searchable_location_fields),
            *reference_conditions,
        ))
    statement = statement.order_by(
        DCValuationRate.effective_from.desc(), DCValuationRate.id.desc()
    )
    # Whole-token reference validation and legacy range expansion happen in
    # Python below, so paginate only unfiltered listing in SQL. Searching
    # streams candidates in schedule order and stops as soon as the requested
    # page is full; the SQL prefilter is a superset of valid token matches.
    has_reference_filter = any((square_no.strip(), kila_no.strip(), khasra_no.strip()))
    sql_pagination = limit is not None and not has_reference_filter and not search
    with SessionLocal() as session:
        if sql_pagination:
            statement = statement.limit(max(1, limit)).offset(max(0, offset))
        if not search and not has_reference_filter:
            # The location relationship is many-to-one, so joined eager loads
            # do not duplicate rows and chunked streaming is safe here.
            return [
                _record_dict(row)
                for row in session.scalars(statement).yield_per(256)
            ]

        search_key = search.casefold()
        reference_fields = ("square_no", "kila_no", "khasra_no")
        start = max(0, offset)
        stop = None if limit is None else start + max(1, limit)
        matches: list[dict[str, Any]] = []
        for row in session.scalars(statement).yield_per(256):
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
            else:
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
            if stop is not None and len(matches) >= stop:
                break
        return matches[start:] if limit is not None else matches


def _header_key(value: Any) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().casefold())
    return normalized.strip("_")


_FISCAL_YEAR_TOKEN = re.compile(r"^(?:19|20)\d{2}$")
_FISCAL_RANGE_TOKEN = re.compile(r"^(?:19|20)\d{2}[-_](?:\d{2}|(?:19|20)\d{2})$")
_FISCAL_SHORT_SUFFIX = re.compile(r"^(?P<end>\d{2})$")


def _rate_header_key(value: Any) -> str:
    """Header alias key with a trailing fiscal-year label removed.

    Published schedules name the rate column with its year, for example
    "DC Rate (2026-2027)" or "DC Rate 2026-27". The year words are not part of
    the column identity; without this the rate column is silently unmapped and
    the import reports a missing required column.
    """
    tokens = [token for token in _header_key(value).split("_") if token]
    while tokens:
        token = tokens[-1]
        if _FISCAL_YEAR_TOKEN.fullmatch(token) or _FISCAL_RANGE_TOKEN.fullmatch(token):
            tokens.pop()
            continue
        short = _FISCAL_SHORT_SUFFIX.match(token)
        if short and len(tokens) >= 2 and _FISCAL_YEAR_TOKEN.fullmatch(tokens[-2]):
            start = int(tokens[-2])
            if start % 100 + 1 == int(short.group("end")):
                tokens.pop()
                tokens.pop()
                continue
        break
    return "_".join(tokens)


def _sheet_from_rows(
    name: str, rows: list[tuple[int, list[Any]]]
) -> dict[str, Any] | None:
    """Build a sheet record with accurate source row numbers."""
    nonempty = [
        (row_number, list(values))
        for row_number, values in rows
        if any(value is not None and str(value).strip() for value in values)
    ]
    if not nonempty:
        return None
    _header_row, header_values = nonempty[0]
    headers = [str(value or "").strip() for value in header_values]
    data = nonempty[1:]
    return {
        "name": name,
        "headers": headers,
        "rows": [values for _row_number, values in data],
        "row_numbers": [row_number for row_number, _values in data],
    }


def read_schedule_sheets(
    file_bytes: bytes, filename: str
) -> list[dict[str, Any]]:
    """Read every non-empty worksheet from a CSV, XLSX, or ODS workbook.

    Reading only the active sheet silently dropped whole villages in
    multi-sheet workbooks. Every sheet is returned with its own headers, rows,
    and original row numbers so the importer can import and report all of them.
    """
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    sheets: list[dict[str, Any]] = []
    if extension == "csv":
        decoded = None
        for encoding in ("utf-8-sig", "cp1252"):
            try:
                decoded = file_bytes.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if decoded is None:
            raise DCRateServiceError(
                "Could not decode this CSV file. Save it as UTF-8 and try again."
            )
        try:
            rows = list(csv.reader(decoded.splitlines()))
        except csv.Error as exc:
            raise DCRateServiceError(f"Could not read CSV file: {exc}") from exc
        sheet = _sheet_from_rows(
            Path(filename).stem or "CSV", list(enumerate(rows, start=1))
        )
        if sheet:
            sheets.append(sheet)
    elif extension == "xlsx":
        try:
            workbook = load_workbook(
                BytesIO(file_bytes), read_only=True, data_only=True
            )
            for worksheet in workbook.worksheets:
                rows = list(enumerate(
                    worksheet.iter_rows(values_only=True), start=1
                ))
                sheet = _sheet_from_rows(worksheet.title, rows)
                if sheet:
                    sheets.append(sheet)
            workbook.close()
        except Exception as exc:
            raise DCRateServiceError(f"Could not open Excel workbook: {exc}") from exc
    elif extension == "ods":
        try:
            from odf.opendocument import load as load_ods
            from odf.table import Table, TableCell, TableRow
            from odf import teletype

            document = load_ods(BytesIO(file_bytes))
            tables = document.spreadsheet.getElementsByType(Table)
            if not tables:
                raise DCRateServiceError("The ODS file has no worksheet.")
            for table in tables:
                rows: list[tuple[int, list[Any]]] = []
                for row_number, row in enumerate(
                    table.getElementsByType(TableRow), start=1
                ):
                    values = []
                    for cell in row.getElementsByType(TableCell):
                        repeat = int(cell.getAttribute("numbercolumnsrepeated") or 1)
                        raw = cell.getAttribute("datevalue") or cell.getAttribute("value")
                        if raw is None:
                            raw = teletype.extractText(cell)
                        values.extend([raw] * min(repeat, 256))
                    rows.append((row_number, values))
                sheet = _sheet_from_rows(
                    table.getAttribute("name") or f"Sheet {len(sheets) + 1}",
                    rows,
                )
                if sheet:
                    sheets.append(sheet)
        except DCRateServiceError:
            raise
        except Exception as exc:
            raise DCRateServiceError(f"Could not open ODS workbook: {exc}") from exc
    else:
        raise DCRateServiceError("Choose a CSV, XLSX, or ODS spreadsheet.")
    if not sheets:
        raise DCRateServiceError("The selected spreadsheet is empty.")
    return sheets


def group_schedule_sheets(
    sheets: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Group worksheets that share the same normalized header row.

    Sheets with identical columns can be imported together under one mapping.
    Sheets with different columns remain separate choices so no column is
    guessed or silently ignored.
    """
    groups: dict[tuple[str, ...], dict[str, Any]] = {}
    for sheet in sheets:
        key = tuple(_header_key(header) for header in sheet["headers"])
        group = groups.get(key)
        if group is None:
            group = {
                "key": key,
                "headers": sheet["headers"],
                "sheets": [],
                "rows": [],
                "row_numbers": [],
                "row_sheets": [],
            }
            groups[key] = group
        group["sheets"].append(sheet["name"])
        group["rows"].extend(sheet["rows"])
        group["row_numbers"].extend(sheet["row_numbers"])
        group["row_sheets"].extend([sheet["name"]] * len(sheet["rows"]))
    return list(groups.values())


def read_schedule_spreadsheet(
    file_bytes: bytes, filename: str
) -> tuple[list[str], list[list[Any]]]:
    """Backward-compatible reader for one header layout.

    Prefer :func:`read_schedule_sheets` in the UI so every worksheet can be
    reviewed and imported. This helper combines all sheets that share the first
    sheet's headers, preserving the previous two-value return.
    """
    groups = group_schedule_sheets(read_schedule_sheets(file_bytes, filename))
    first = groups[0]
    return first["headers"], first["rows"]


def _column_variety(index: int, rows: list[list[Any]], sample_size: int = 250) -> int:
    """Count distinct non-empty values for a candidate source column."""
    values = set()
    for row in rows[:sample_size]:
        if index >= len(row):
            continue
        value = _text(row[index])
        if value:
            values.add(value.casefold())
    return len(values)


def _choose_mauza_column(
    candidates: list[int], rows: list[list[Any]] | None
) -> int:
    """Pick the real village column when a sheet repeats the Mauza header.

    Some published schedules contain two columns labelled "Mauza": the first
    repeats the district/tehsil and the second holds the actual villages. A
    constant administrative column has almost no variety, so prefer the
    candidate with the most distinct sample values. Ties keep the right-most
    column because schedules generally put the specific village after the
    broader administrative label.
    """
    if not candidates:
        raise DCRateServiceError("No Mauza column was found in this worksheet.")
    if len(candidates) == 1 or not rows:
        return candidates[0]
    return max(
        candidates,
        key=lambda index: (_column_variety(index, rows), index),
    )


def suggested_column_mapping(
    headers: list[str], rows: list[list[Any]] | None = None
) -> dict[str, int]:
    """Suggest mappings from familiar schedule headers; user confirms them.

    ``rows`` is optional and only used to disambiguate repeated headers. When
    several columns look like Mauza, the most varied sample column is chosen so
    a repeated district/tehsil column does not hide the real villages.
    """
    mapping = {}
    mauza_candidates: list[int] = []
    for index, header in enumerate(headers):
        key = _rate_header_key(header)
        target = RATE_HEADERS.get(key)
        if key == "location":
            target = "locality"
        if target == "mauza":
            mauza_candidates.append(index)
            continue
        if target and target not in mapping:
            mapping[target] = index
    # Some official files label the village column with wording that is not an
    # exact alias, such as "Name of Village" or "Mauza / Village". Match those
    # tokens explicitly so a whole village is not silently left unmapped.
    if not mauza_candidates:
        for index, header in enumerate(headers):
            key = _header_key(header)
            if any(token in key for token in ("mauza", "mouza", "village")):
                mauza_candidates.append(index)
    if mauza_candidates:
        mapping["mauza"] = _choose_mauza_column(mauza_candidates, rows)
    # The current official schedule exports usually expose Mauza and Location,
    # but not a separate display-name column for the database location record.
    if "location_name" not in mapping:
        if "mauza" in mapping:
            mapping["location_name"] = mapping["mauza"]
        elif "locality" in mapping:
            mapping["location_name"] = mapping["locality"]
    return mapping


def _preview_import_items(
    items: list[dict[str, Any]],
    *,
    duplicate_policy: str = "check_now",
    duplicate_rule: str | None = None,
    storage_mode: str = "ranges",
    initial_issues: list[dict[str, Any]] | None = None,
    initial_invalid_rows: int = 0,
) -> dict[str, Any]:
    """Validate prepared import rows and apply the chosen duplicate policy.

    ``items`` each contain a source row number, optional worksheet name, raw
    location/rate values, and the raw source values used in reports. Rows whose
    required profile fields are blank are supplied through ``initial_issues``
    by the caller so their accurate source row numbers are preserved.
    """
    if duplicate_policy not in {"check_now", "import_all"}:
        raise DCRateServiceError("Choose whether to check duplicates now or later.")
    if storage_mode not in REFERENCE_STORAGE_MODES:
        raise DCRateServiceError("Cadastral storage must be 'ranges' or 'individual'.")
    with SessionLocal() as session:
        profile = duplicate_rules.get_profile(
            session,
            duplicate_rule or duplicate_rules.EXACT_ALL_REFERENCE,
        )
    issues: list[dict[str, Any]] = list(initial_issues or [])
    warnings: list[dict[str, Any]] = []
    invalid_rows = initial_invalid_rows
    candidates: list[dict[str, Any]] = []
    workbook_index = duplicate_rules.DuplicateIndex(profile)
    expanded_rows = 0
    workbook_duplicates = 0
    database_duplicates = 0
    duplicates_kept = 0

    def attach_sheet(issue: dict[str, Any], sheet: str | None) -> dict[str, Any]:
        if sheet:
            issue.setdefault("sheet", sheet)
        return issue

    def duplicate_issue(
        row_number: int,
        prior: dict[str, Any],
        source_values: list[str],
        location: dict[str, Any],
        *,
        where: str,
        sheet: str | None,
    ) -> dict[str, Any]:
        cadastral = " / ".join(
            value
            for value in (
                location.get("square_no"),
                location.get("kila_no"),
                location.get("khasra_no"),
            )
            if value
        ) or "same cadastral reference"
        if prior.get("id") is not None:
            match_text = f"existing record #{prior['id']}"
        else:
            match_text = f"row {prior.get('row')} in this import"
        return attach_sheet({
            "row": row_number,
            "kind": f"exact duplicate in {where}",
            "message": (
                f"All compared columns exactly match {match_text} "
                f"({cadastral}). This duplicate is removed; the first row is kept."
            ),
            "source_values": source_values,
            "allowed_by_rule": False,
        }, sheet)

    def duplicate_warning(
        row_number: int,
        prior: dict[str, Any],
        source_values: list[str],
        *,
        where: str,
        sheet: str | None,
    ) -> dict[str, Any]:
        if prior.get("id") is not None:
            match_text = f"existing record #{prior['id']}"
        else:
            match_text = f"row {prior.get('row')} in this import"
        return attach_sheet({
            "row": row_number,
            "kind": f"exact duplicate in {where} (kept for later cleanup)",
            "message": (
                f"All compared columns exactly match {match_text}. It will be "
                "imported now and can be removed later with the duplicate tools."
            ),
            "source_values": source_values,
            "allowed_by_rule": True,
        }, sheet)

    for item in items:
        row_number = int(item["row"])
        sheet = item.get("sheet")
        source_values = item.get("source_values", [])
        try:
            location = _location_data(
                item.get("location_values") or {}, reference_mode=storage_mode
            )
            rate = _rate_data(item.get("rate_values") or {})
            if storage_mode == "ranges":
                # One compact record represents the whole range.
                expanded_locations = [location]
            else:
                expanded_locations = _expand_location_values(
                    location, source_row=row_number
                )
            new_expanded_total = expanded_rows + len(expanded_locations)
            if new_expanded_total > MAX_EXPANDED_ROWS_PER_IMPORT:
                raise DCRateServiceError(
                    f"This import expands to more than "
                    f"{MAX_EXPANDED_ROWS_PER_IMPORT:,} cadastral records. "
                    "Split the schedule into smaller files or keep ranges."
                )
            expanded_rows = new_expanded_total
            seen_combinations: set[tuple[Any, ...]] = set()
            for expanded_index, expanded_location in enumerate(
                expanded_locations, start=1
            ):
                flat = _flat_rate_record(rate, expanded_location)
                combination_key = duplicate_rules.record_key(flat, profile)
                if combination_key in seen_combinations:
                    continue
                seen_combinations.add(combination_key)
                prior = workbook_index.find(flat)
                if prior is not None:
                    if duplicate_policy == "check_now":
                        workbook_duplicates += 1
                        issues.append(duplicate_issue(
                            row_number,
                            prior,
                            source_values,
                            expanded_location,
                            where="workbook",
                            sheet=sheet,
                        ))
                    else:
                        duplicates_kept += 1
                        warnings.append(duplicate_warning(
                            row_number,
                            prior,
                            source_values,
                            where="workbook",
                            sheet=sheet,
                        ))
                    # Exact duplicates are already represented by the first row;
                    # a later cleanup can only keep one copy.
                    if duplicate_policy == "check_now":
                        continue
                workbook_index.add({**flat, "row": row_number})
                candidates.append({
                    "row": row_number,
                    "source_row": row_number,
                    "sheet": sheet,
                    "expanded_index": expanded_index,
                    "expanded_count": len(expanded_locations),
                    "location": expanded_location,
                    "source_values": source_values,
                    **rate,
                })
        except DCRateServiceError as exc:
            invalid_rows += 1
            issues.append(attach_sheet({
                "row": row_number,
                "kind": "invalid",
                "message": str(exc),
                "source_values": source_values,
            }, sheet))

    db_index = duplicate_rules.DuplicateIndex(profile)
    with SessionLocal() as session:
        rates = session.scalars(
            select(DCValuationRate)
            .options(joinedload(DCValuationRate.location))
            .order_by(DCValuationRate.id)
        ).unique().all()
        for rate in rates:
            db_index.add(_record_dict(rate))

    new_candidates = []
    for candidate in candidates:
        flat = _flat_rate_record(
            dict(candidate),
            candidate.get("location") or {},
        )
        duplicate = db_index.find(flat)
        if duplicate is not None:
            if duplicate_policy == "check_now":
                database_duplicates += 1
                issues.append(duplicate_issue(
                    candidate["row"],
                    dict(duplicate),
                    candidate.get("source_values", []),
                    candidate.get("location") or {},
                    where="database",
                    sheet=candidate.get("sheet"),
                ))
                continue
            duplicates_kept += 1
            warnings.append(duplicate_warning(
                candidate["row"],
                dict(duplicate),
                candidate.get("source_values", []),
                where="database",
                sheet=candidate.get("sheet"),
            ))
        new_candidates.append(candidate)
        db_index.add({**flat, "row": candidate["row"], "id": None})

    for issue in issues:
        issue.setdefault(
            "fix", _import_fix_suggestion(issue["message"], issue["kind"])
        )
    for warning in warnings:
        warning.setdefault(
            "fix",
            "This row is imported now. Use 'Find / remove duplicates' after the "
            "import to remove exact copies according to a rule.",
        )
    return {
        "valid_records": new_candidates,
        "issues": sorted(issues, key=lambda issue: issue["row"]),
        "warnings": sorted(warnings, key=lambda warning: warning["row"]),
        "duplicate_rule": duplicate_rules.profile_to_dict(profile),
        "duplicate_policy": duplicate_policy,
        "storage_mode": storage_mode,
        "expanded_rows": expanded_rows,
        "duplicates_removed": workbook_duplicates + database_duplicates,
        "duplicates_kept": duplicates_kept,
        "workbook_duplicates": workbook_duplicates,
        "database_duplicates": database_duplicates,
        "invalid_rows": invalid_rows,
    }


def preview_mapped_schedule(
    headers: list[str],
    rows: list[list[Any]],
    mapping: dict[str, int],
    schedule_type: str,
    *,
    duplicate_policy: str = "check_now",
    duplicate_rule: str | None = None,
    storage_mode: str = "ranges",
    row_numbers: list[int] | None = None,
    row_sheets: list[str] | None = None,
) -> dict[str, Any]:
    """Normalize a rural/urban DC or FBR profile and validate it without writes.

    ``duplicate_policy='check_now'`` removes exact duplicates immediately using
    ``duplicate_rule`` (all columns by default). ``import_all`` imports every
    row and reports exact duplicates as warnings so they can be cleaned later.
    """
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
    if duplicate_policy == "check_now":
        with SessionLocal() as session:
            selected_profile = duplicate_rules.get_profile(
                session, duplicate_rule or duplicate_rules.EXACT_ALL_REFERENCE
            )
        try:
            duplicate_rules.validate_profile_scope(
                selected_profile,
                schedule_type=normalized_schedule,
                land_type=land_type,
            )
        except duplicate_rules.DuplicateRuleError:
            # Older callers may pass a rule for a different schedule. Imports
            # must stay usable, so fall back to the safe all-columns-exact rule
            # instead of failing the whole preview.
            duplicate_rule = duplicate_rules.EXACT_ALL_REFERENCE
    # Report blank cells in required profile fields by original spreadsheet
    # row number. Optional fields remain nullable and can be omitted.
    row_numbers = list(row_numbers) if row_numbers is not None else [
        number for number in range(2, len(rows) + 2)
    ]
    row_sheets = list(row_sheets) if row_sheets is not None else [None] * len(rows)
    if len(row_numbers) != len(rows) or len(row_sheets) != len(rows):
        raise DCRateServiceError(
            "The worksheet row metadata does not match the supplied rows."
        )
    row_issues: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    required_targets = required_by_profile[schedule_type]
    rate_fields = (
        "property_type", "classification", "floor", "rate_value",
        "structure_rate", "per_marla_value", "rate_unit", "effective_from",
        "effective_to", "published_year", "source_reference", "source_date",
        "verification_status", "verified_by",
    )
    for index, row in enumerate(rows):
        row_number = row_numbers[index]
        sheet_name = row_sheets[index]

        def mapped(field: str):
            column = mapping.get(field)
            if column is None:
                return None
            if column == -1:
                return "per_square_foot"
            return row[column] if column < len(row) else None

        empty = [
            field for field in required_targets
            if field in mapping
            and (
                mapping[field] == -1
                or mapping[field] >= len(row)
                or _text(row[mapping[field]]) is None
            )
        ]
        if schedule_type in {"rural_dc", "fbr"}:
            def has_value(field: str) -> bool:
                column = mapping.get(field)
                return (
                    column is not None
                    and column >= 0
                    and column < len(row)
                    and _text(row[column]) is not None
                )

            # A Khasra-based record may legitimately omit square and Kila.
            # Rural rows otherwise need both; FBR rows need a square number.
            if has_value("khasra_no"):
                empty = [field for field in empty if field not in {"square_no", "kila_no"}]
            elif schedule_type == "rural_dc" and (not has_value("square_no") or not has_value("kila_no")):
                empty.extend(
                    field for field in ("square_no", "kila_no")
                    if not has_value(field) and field not in empty
                )
            elif schedule_type == "fbr" and not has_value("square_no"):
                empty.append("square_no")
        source_values = [
            "" if value is None else str(value) for value in row
        ]
        if empty:
            row_issues.append({
                "row": row_number,
                "sheet": sheet_name,
                "kind": "invalid",
                "message": "Required value(s) are blank: " + ", ".join(sorted(empty)),
                "source_values": source_values,
                "fix": "Fill the missing required value or select the correct spreadsheet column mapping.",
            })
            continue
        location_values = {
            field: mapped(field) for field in LOCATION_FIELDS if field in mapping
        }
        # The schedule profile, not a source column, defines rural/urban.
        location_values["land_type"] = land_type
        location_values["name"] = (
            mapped("location_name")
            or mapped("mauza")
            or mapped("locality")
        )
        rate_values = {
            field: mapped(field) for field in rate_fields if field in mapping
        }
        rate_values["schedule_type"] = normalized_schedule
        rate_values["land_type"] = land_type
        items.append({
            "row": row_number,
            "sheet": sheet_name,
            "location_values": location_values,
            "rate_values": rate_values,
            "source_values": source_values,
        })

    preview = _preview_import_items(
        items,
        duplicate_policy=duplicate_policy,
        duplicate_rule=duplicate_rule,
        storage_mode=storage_mode,
        initial_issues=row_issues,
        initial_invalid_rows=len(row_issues),
    )
    preview["rows_read"] = len(rows)
    preview["source_rows"] = len(rows)
    preview["sheet_names"] = sorted({
        name for name in row_sheets if name
    })
    return preview


def preview_dc_rate_workbook(
    file_bytes: bytes,
    *,
    duplicate_policy: str = "check_now",
    duplicate_rule: str | None = None,
    storage_mode: str = "ranges",
) -> dict[str, Any]:
    """Validate an XLSX sheet and report invalid/exact-duplicate rows without writes.

    This entry point recognises the standard schedule headers directly. For
    user-defined column mappings use :func:`preview_mapped_schedule`, which
    shares the same validation, duplicate policy, and range storage pipeline.
    """
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
            target = RATE_HEADERS.get(_rate_header_key(value))
            if target is None and any(
                token in raw_header for token in ("mauza", "mouza", "village")
            ):
                target = "mauza"
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

        items: list[dict[str, Any]] = []
        for excel_row, values in enumerate(iterator, start=2):
            if not any(value is not None and str(value).strip() for value in values):
                continue
            raw = {
                field: values[index] if index < len(values) else None
                for field, index in columns.items()
            }
            items.append({
                "row": excel_row,
                "sheet": None,
                "location_values": {
                    **{field: raw.get(field) for field in LOCATION_FIELDS},
                    "name": (
                        raw.get("location_name")
                        or raw.get("mauza")
                        or raw.get("locality")
                    ),
                },
                "rate_values": raw,
                "source_values": [
                    "" if value is None else str(value) for value in values
                ],
            })

        preview = _preview_import_items(
            items,
            duplicate_policy=duplicate_policy,
            duplicate_rule=duplicate_rule,
            storage_mode=storage_mode,
        )
        preview["rows_read"] = len(items)
        preview["source_rows"] = len(items)
        return preview
    finally:
        workbook.close()


def import_dc_rate_records(
    records: list[dict[str, Any]],
    *,
    duplicate_policy: str = "check_now",
    duplicate_rule: str | None = None,
    storage_mode: str = "ranges",
) -> dict[str, Any]:
    """Batch-insert previewed rows using the chosen storage and duplicate policy.

    ``storage_mode='ranges'`` stores one compact Square/Kila/Khasra range per
    previewed record, while ``individual`` expands it into one row per value.
    ``duplicate_policy='import_all'`` inserts exact duplicates too so they can be
    removed later with the duplicate tools.
    """
    if duplicate_policy not in {"check_now", "import_all"}:
        raise DCRateServiceError("Choose whether to check duplicates now or later.")
    if storage_mode not in REFERENCE_STORAGE_MODES:
        raise DCRateServiceError("Cadastral storage must be 'ranges' or 'individual'.")
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    duplicates_removed = 0
    duplicates_kept = 0
    expanded_total = 0
    with SessionLocal.begin() as session:
        profile = duplicate_rules.get_profile(
            session, duplicate_rule or duplicate_rules.EXACT_ALL_REFERENCE
        )
        # Cache all known locations by their normalized identity so thousands
        # of rows do not repeatedly scan/query the table.
        location_by_key = {
            _location_key({field: getattr(location, field) for field in LOCATION_FIELDS}): location
            for location in session.scalars(select(PropertyLocation)).all()
        }
        prepared: list[
            tuple[dict[str, Any], dict[str, Any], dict[str, Any], PropertyLocation]
        ] = []
        new_locations: list[PropertyLocation] = []
        for candidate in records:
            base_location = _location_data(
                candidate["location"], reference_mode=storage_mode
            )
            rate_values = _rate_data(candidate)
            if storage_mode == "ranges":
                expanded_locations = [base_location]
            else:
                expanded_locations = _expand_location_values(
                    base_location, source_row=candidate.get("row")
                )
            expanded_total += len(expanded_locations)
            if expanded_total > MAX_EXPANDED_ROWS_PER_IMPORT:
                raise DCRateServiceError(
                    f"This import expands to more than "
                    f"{MAX_EXPANDED_ROWS_PER_IMPORT:,} cadastral records. "
                    "Split the schedule into smaller files or keep ranges."
                )
            for location_values in expanded_locations:
                location_key = _location_key(location_values)
                location = location_by_key.get(location_key)
                if location is None:
                    location = PropertyLocation(**location_values)
                    location_by_key[location_key] = location
                    new_locations.append(location)
                prepared.append((candidate, rate_values, location_values, location))

        if new_locations:
            session.add_all(new_locations)
            # One flush assigns IDs for all newly created locations.
            session.flush()

        # Index existing rates once so commit detects changes made after preview.
        rate_index = duplicate_rules.DuplicateIndex(profile)
        if duplicate_policy == "check_now":
            for rate in session.scalars(
                select(DCValuationRate)
                .options(joinedload(DCValuationRate.location))
                .order_by(DCValuationRate.id)
            ).unique().all():
                rate_index.add(_record_dict(rate))

        new_rates: list[DCValuationRate] = []
        for candidate, rate_values, location_values, location in prepared:
            flat = _flat_rate_record(rate_values, location_values)
            if duplicate_policy == "check_now":
                duplicate = rate_index.find(flat)
                if duplicate is not None:
                    duplicates_removed += 1
                    if duplicate.get("id") is not None:
                        duplicate_description = f"existing record #{duplicate['id']}"
                    else:
                        duplicate_description = (
                            f"row {duplicate.get('row')} in this import"
                        )
                    issues.append({
                        "row": candidate.get("row"),
                        "sheet": candidate.get("sheet"),
                        "kind": "exact duplicate at import time",
                        "message": (
                            f"All compared columns exactly match {duplicate_description}. "
                            "This duplicate was not inserted; the first row is kept."
                        ),
                        "allowed_by_rule": False,
                    })
                    continue
            else:
                # Duplicates are intentionally imported; report them so the
                # user can find them later with the cleanup tools.
                if rate_index.find(flat) is not None:
                    duplicates_kept += 1
                    warnings.append({
                        "row": candidate.get("row"),
                        "sheet": candidate.get("sheet"),
                        "kind": "exact duplicate imported for later cleanup",
                        "message": (
                            "An exact duplicate of this row is already stored. "
                            "It was imported because duplicates are set to be "
                            "cleaned later."
                        ),
                        "allowed_by_rule": True,
                    })
            record = DCValuationRate(location_id=location.id, **rate_values)
            new_rates.append(record)
            rate_index.add({
                **flat,
                "row": candidate.get("row"),
                "id": None,
            })
        if new_rates:
            session.add_all(new_rates)
            session.flush()
    return {
        "imported_count": len(new_rates),
        "issues": issues,
        "warnings": warnings,
        "allowed_duplicate_count": duplicates_kept,
        "duplicates_removed": duplicates_removed,
        "duplicates_kept": duplicates_kept,
        "duplicate_policy": duplicate_policy,
        "storage_mode": storage_mode,
        "expanded_count": expanded_total,
        "source_records": len(records),
        "duplicate_rule": duplicate_rules.profile_to_dict(profile),
    }
