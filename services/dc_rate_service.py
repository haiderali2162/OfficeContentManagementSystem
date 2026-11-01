"""Validation and persistence for DC valuation rates and Excel imports."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO
import csv
import re
from typing import Any

from openpyxl import Workbook, load_workbook
from sqlalchemy import or_, select
from sqlalchemy.orm import joinedload

from core.database import SessionLocal
from core.models import DC_RATE_UNITS, DCValuationRate, PropertyLocation


class DCRateServiceError(ValueError):
    """Raised when a DC-rate operation fails validation or conflicts."""


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


SCHEDULE_EXPORT_COLUMNS = (
    ("schedule_type", "Schedule"),
    ("serial_no", "Serial no."),
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
    ("classification", "Land classification"),
    ("rate_value", "Schedule rate"),
    ("rate_unit", "Unit"),
    ("structure_rate", "Structure rate"),
    ("per_marla_value", "Per marla valuation"),
    ("published_year", "Published year"),
    ("source_reference", "Schedule reference"),
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
    square_no: str = "",
    kila_no: str = "",
    limit: int | None = None,
) -> list[dict[str, Any]]:
    statement = select(DCValuationRate).options(joinedload(DCValuationRate.location))
    search = search.strip()
    if mauza.strip() or (search and not search.isdigit()):
        statement = statement.join(DCValuationRate.location)
    if schedule_type in {"dc", "fbr"}:
        statement = statement.where(DCValuationRate.schedule_type == schedule_type)
    if location_id is not None:
        statement = statement.where(DCValuationRate.location_id == location_id)
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
    # Cadastral fields are stored as token lists; query candidates in SQLite,
    # then validate whole tokens below so (for example) 2 does not match 12.
    for field_name, term in (("square_no", square_no), ("kila_no", kila_no)):
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
    with SessionLocal() as session:
        if limit is not None and not search.strip():
            statement = statement.limit(max(1, limit))
        rows = session.scalars(statement).unique().all()
        if not search and not square_no.strip() and not kila_no.strip():
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
            except DCRateServiceError:
                square_match = kila_match = False
            if not square_match or not kila_match:
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
        return matches


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
