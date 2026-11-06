"""Configurable duplicate detection rules for DC valuation rates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import json
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.models import DCRateDuplicateProfile


class DuplicateRuleError(ValueError):
    """Raised when a duplicate-rule profile is invalid or missing."""


SCHEDULE_SCOPES = ("rural_dc", "urban_dc", "fbr", "any")
DATE_MODES = ("overlap", "exact", "ignore")
IMPORT_ACTIONS = ("skip", "import")

MATCH_FIELD_GROUPS = (
    (
        "Location",
        (
            ("name", "Location name"),
            ("serial_no", "Serial no."),
            ("province", "Province"),
            ("district", "District"),
            ("tehsil", "Tehsil"),
            ("land_type", "Rural / urban"),
            ("committee_town", "Committee / town"),
            ("revenue_circle", "Revenue circle"),
            ("property_area", "Property area"),
            ("qanoongoi", "Qanoongoi"),
            ("patwar_circle", "Patwar circle"),
            ("mauza", "Mauza"),
            ("hadbast_number", "Hadbast number"),
            ("square_no", "Square no."),
            ("kila_no", "Kila no."),
            ("khasra_no", "Khasra no."),
            ("locality", "Locality"),
            ("notes", "Location notes"),
        ),
    ),
    (
        "Schedule",
        (
            ("schedule_type", "Schedule type"),
            ("property_type", "Property type"),
            ("classification", "Classification"),
            ("floor", "Floor"),
            ("rate_unit", "Rate unit"),
        ),
    ),
    (
        "Rate values",
        (
            ("rate_value", "Rate value"),
            ("structure_rate", "Structure rate"),
            ("per_marla_value", "Per-Marla value"),
            ("published_year", "Published year"),
        ),
    ),
    (
        "Source and verification",
        (
            ("source_reference", "Source reference"),
            ("source_date", "Source date"),
            ("verification_status", "Verification status"),
            ("verified_by", "Verified by"),
        ),
    ),
)
MATCH_FIELD_LABELS = {
    field: label for _group, fields in MATCH_FIELD_GROUPS for field, label in fields
}
MATCH_FIELDS = tuple(MATCH_FIELD_LABELS)


@dataclass(frozen=True)
class DuplicateProfile:
    """A validated, immutable duplicate-rule profile."""

    reference: str
    name: str
    schedule_scope: str
    match_fields: tuple[str, ...]
    date_mode: str
    import_action: str
    description: str = ""
    is_builtin: bool = False
    custom_id: int | None = None


_ADMIN_LOCATION_FIELDS = (
    "name",
    "province",
    "district",
    "tehsil",
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
)
_RURAL_LOCATION_FIELDS = (
    "name",
    "province",
    "district",
    "tehsil",
    "qanoongoi",
    "patwar_circle",
    "mauza",
    "hadbast_number",
    "square_no",
    "kila_no",
    "khasra_no",
    "locality",
)
_ALL_LOCATION_FIELDS = (
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
)
# Every comparable persisted location column, including free-text notes.
_FULL_LOCATION_FIELDS = (*_ALL_LOCATION_FIELDS, "notes")
_SCHEDULE_FIELDS = (
    "schedule_type",
    "property_type",
    "classification",
    "floor",
    "rate_unit",
)
_VALUE_FIELDS = (
    "rate_value",
    "structure_rate",
    "per_marla_value",
    "published_year",
)
_SOURCE_FIELDS = (
    "source_reference",
    "source_date",
    "verification_status",
    "verified_by",
)
EXACT_ALL_REFERENCE = "default:all_columns_exact"

BUILTIN_PROFILES = (
    DuplicateProfile(
        reference="default:rural_dc_overlap",
        name="Rural DC - same cadastral row (overlap)",
        schedule_scope="rural_dc",
        match_fields=(
            *_RURAL_LOCATION_FIELDS,
            *_SCHEDULE_FIELDS,
        ),
        date_mode="overlap",
        import_action="skip",
        description=(
            "Treat rural DC rows as duplicates when their cadastral and "
            "classification values match and their effective periods overlap."
        ),
        is_builtin=True,
    ),
    DuplicateProfile(
        reference="default:rural_dc_exact",
        name="Rural DC - exact source values",
        schedule_scope="rural_dc",
        match_fields=(
            *_RURAL_LOCATION_FIELDS,
            *_SCHEDULE_FIELDS,
            *_VALUE_FIELDS,
        ),
        date_mode="exact",
        import_action="skip",
        description=(
            "Treat rural DC rows as duplicates only when all selected values, "
            "including rate values, and both effective dates match."
        ),
        is_builtin=True,
    ),
    DuplicateProfile(
        reference="default:urban_dc_overlap",
        name="Urban DC - same property (overlap)",
        schedule_scope="urban_dc",
        match_fields=(
            *_ADMIN_LOCATION_FIELDS,
            *_SCHEDULE_FIELDS,
        ),
        date_mode="overlap",
        import_action="skip",
        description=(
            "Treat urban DC rows as duplicates when their location and "
            "classification values match and their effective periods overlap."
        ),
        is_builtin=True,
    ),
    DuplicateProfile(
        reference="default:fbr_overlap",
        name="FBR - same property (overlap)",
        schedule_scope="fbr",
        match_fields=(
            *_ADMIN_LOCATION_FIELDS,
            *_SCHEDULE_FIELDS,
        ),
        date_mode="overlap",
        import_action="skip",
        description=(
            "Treat FBR rows as duplicates when their location and "
            "classification values match and their effective periods overlap."
        ),
        is_builtin=True,
    ),
    DuplicateProfile(
        reference="default:any_exact",
        name="Any schedule - exact source values",
        schedule_scope="any",
        match_fields=(
            *_ALL_LOCATION_FIELDS,
            *_SCHEDULE_FIELDS,
            *_VALUE_FIELDS,
        ),
        date_mode="exact",
        import_action="skip",
        description=(
            "Treat rows as duplicates only when the complete selected location, "
            "schedule, rate values, and effective dates match."
        ),
        is_builtin=True,
    ),
    DuplicateProfile(
        reference=EXACT_ALL_REFERENCE,
        name="Any schedule - all columns exactly identical",
        schedule_scope="any",
        match_fields=(
            *_FULL_LOCATION_FIELDS,
            *_SCHEDULE_FIELDS,
            *_VALUE_FIELDS,
            *_SOURCE_FIELDS,
        ),
        date_mode="exact",
        import_action="skip",
        description=(
            "Used automatically by schedule imports. A row is removed only when "
            "every persisted location, schedule, rate, date, source, and "
            "verification value is identical."
        ),
        is_builtin=True,
    ),
)
_BUILTIN_BY_REFERENCE = {profile.reference: profile for profile in BUILTIN_PROFILES}


def profile_to_dict(profile: DuplicateProfile) -> dict[str, Any]:
    """Serialize a profile for the Flet UI."""
    return {
        "reference": profile.reference,
        "name": profile.name,
        "schedule_scope": profile.schedule_scope,
        "match_fields": list(profile.match_fields),
        "date_mode": profile.date_mode,
        "import_action": profile.import_action,
        "description": profile.description,
        "is_builtin": profile.is_builtin,
        "custom_id": profile.custom_id,
    }


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_value(value: Any) -> Any:
    """Normalize comparison values without converting money through floats."""
    if value is None:
        return None
    if isinstance(value, str):
        normalized = " ".join(value.split()).casefold()
        return normalized or None
    if isinstance(value, Decimal):
        normalized = value.normalize()
        return "0" if normalized == 0 else format(normalized, "f")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return _normalize_value(Decimal(str(value)))
    return value


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    text = _clean_text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _periods_overlap(
    first: Mapping[str, Any], second: Mapping[str, Any]
) -> bool:
    first_start = _as_date(first.get("effective_from"))
    second_start = _as_date(second.get("effective_from"))
    if first_start is None or second_start is None:
        return False
    first_end = _as_date(first.get("effective_to"))
    second_end = _as_date(second.get("effective_to"))
    return (first_end is None or second_start <= first_end) and (
        second_end is None or first_start <= second_end
    )


def normalize_match_fields(fields: Iterable[str]) -> tuple[str, ...]:
    """Validate match fields and return them in the canonical display order."""
    selected = {str(field).strip() for field in fields if str(field).strip()}
    unknown = selected - set(MATCH_FIELDS)
    if unknown:
        raise DuplicateRuleError(
            "Unknown duplicate match field(s): " + ", ".join(sorted(unknown))
        )
    if not selected:
        raise DuplicateRuleError("Select at least one value to compare.")
    return tuple(field for field in MATCH_FIELDS if field in selected)


def validate_profile_values(
    *,
    name: str,
    schedule_scope: str,
    match_fields: Iterable[str],
    date_mode: str,
    import_action: str,
    description: str | None = None,
) -> tuple[str, str, tuple[str, ...], str, str, str | None]:
    """Normalize and validate editable profile values."""
    clean_name = _clean_text(name)
    if not clean_name:
        raise DuplicateRuleError("Enter a duplicate-rule name.")
    if len(clean_name) > 120:
        raise DuplicateRuleError("Duplicate-rule names must be 120 characters or fewer.")

    clean_scope = (_clean_text(schedule_scope) or "").casefold()
    if clean_scope not in SCHEDULE_SCOPES:
        raise DuplicateRuleError("Choose a valid schedule scope.")
    clean_date_mode = (_clean_text(date_mode) or "").casefold()
    if clean_date_mode not in DATE_MODES:
        raise DuplicateRuleError("Choose Overlapping periods, Exact dates, or Ignore dates.")
    clean_action = (_clean_text(import_action) or "").casefold()
    if clean_action not in IMPORT_ACTIONS:
        raise DuplicateRuleError("Choose Skip duplicates or Import anyway.")

    return (
        clean_name,
        clean_scope,
        normalize_match_fields(match_fields),
        clean_date_mode,
        clean_action,
        _clean_text(description),
    )


def _ensure_name_available(
    session: Session, name: str, *, exclude_id: int | None = None
) -> None:
    candidate = name.casefold()
    for profile in BUILTIN_PROFILES:
        if profile.name.casefold() == candidate:
            raise DuplicateRuleError(
                f"'{name}' is a built-in duplicate rule. Choose another name."
            )
    for row in session.scalars(select(DCRateDuplicateProfile)).all():
        if row.id != exclude_id and row.name.casefold() == candidate:
            raise DuplicateRuleError(
                f"A duplicate rule named '{name}' already exists."
            )


def _row_to_profile(row: DCRateDuplicateProfile) -> DuplicateProfile:
    try:
        raw_fields = json.loads(row.match_fields_json)
        if not isinstance(raw_fields, list):
            raise ValueError
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DuplicateRuleError(
            f"Duplicate rule #{row.id} has invalid saved match fields."
        ) from exc
    try:
        name, scope, fields, date_mode, action, description = validate_profile_values(
            name=row.name,
            schedule_scope=row.schedule_scope,
            match_fields=raw_fields,
            date_mode=row.date_mode,
            import_action=row.import_action,
            description=row.description,
        )
    except DuplicateRuleError as exc:
        raise DuplicateRuleError(
            f"Duplicate rule #{row.id} is invalid: {exc}"
        ) from exc
    return DuplicateProfile(
        reference=f"custom:{row.id}",
        name=name,
        schedule_scope=scope,
        match_fields=fields,
        date_mode=date_mode,
        import_action=action,
        description=description or "",
        is_builtin=False,
        custom_id=row.id,
    )


def list_profiles(
    session: Session, *, schedule_scope: str | None = None
) -> list[DuplicateProfile]:
    """Return built-in and custom profiles, optionally filtered for a schedule."""
    profiles = list(BUILTIN_PROFILES)
    profiles.extend(
        _row_to_profile(row)
        for row in session.scalars(
            select(DCRateDuplicateProfile).order_by(DCRateDuplicateProfile.name)
        ).all()
    )
    scope = _clean_text(schedule_scope)
    if scope:
        profiles = [
            profile for profile in profiles
            if profile.schedule_scope in {scope, "any"}
        ]
    return profiles


def get_profile(session: Session, reference: str) -> DuplicateProfile:
    """Resolve a built-in reference or a database-backed custom profile."""
    clean_reference = _clean_text(reference)
    if not clean_reference:
        raise DuplicateRuleError("Select a duplicate rule before continuing.")
    builtin = _BUILTIN_BY_REFERENCE.get(clean_reference)
    if builtin is not None:
        return builtin
    if clean_reference.startswith("custom:"):
        try:
            profile_id = int(clean_reference.partition(":")[2])
        except ValueError as exc:
            raise DuplicateRuleError("Select a valid duplicate rule.") from exc
        row = session.get(DCRateDuplicateProfile, profile_id)
        if row is None:
            raise DuplicateRuleError(
                "The selected duplicate rule no longer exists. Select another rule."
            )
        return _row_to_profile(row)
    raise DuplicateRuleError("Select a valid duplicate rule.")


def create_custom_profile(
    session: Session,
    *,
    name: str,
    schedule_scope: str,
    match_fields: Iterable[str],
    date_mode: str,
    import_action: str,
    description: str | None = None,
) -> DuplicateProfile:
    """Create a custom profile without committing the surrounding transaction."""
    values = validate_profile_values(
        name=name,
        schedule_scope=schedule_scope,
        match_fields=match_fields,
        date_mode=date_mode,
        import_action=import_action,
        description=description,
    )
    clean_name, scope, fields, clean_date_mode, clean_action, clean_description = values
    _ensure_name_available(session, clean_name)
    row = DCRateDuplicateProfile(
        name=clean_name,
        schedule_scope=scope,
        match_fields_json=json.dumps(list(fields), ensure_ascii=False),
        date_mode=clean_date_mode,
        import_action=clean_action,
        description=clean_description,
    )
    session.add(row)
    session.flush()
    return _row_to_profile(row)


def update_custom_profile(
    session: Session,
    profile_id: int,
    *,
    name: str,
    schedule_scope: str,
    match_fields: Iterable[str],
    date_mode: str,
    import_action: str,
    description: str | None = None,
) -> DuplicateProfile:
    """Update a custom profile without committing the surrounding transaction."""
    row = session.get(DCRateDuplicateProfile, profile_id)
    if row is None:
        raise DuplicateRuleError("Duplicate rule not found.")
    values = validate_profile_values(
        name=name,
        schedule_scope=schedule_scope,
        match_fields=match_fields,
        date_mode=date_mode,
        import_action=import_action,
        description=description,
    )
    clean_name, scope, fields, clean_date_mode, clean_action, clean_description = values
    _ensure_name_available(session, clean_name, exclude_id=profile_id)
    row.name = clean_name
    row.schedule_scope = scope
    row.match_fields_json = json.dumps(list(fields), ensure_ascii=False)
    row.date_mode = clean_date_mode
    row.import_action = clean_action
    row.description = clean_description
    session.flush()
    return _row_to_profile(row)


def delete_custom_profile(session: Session, profile_id: int) -> None:
    """Delete a custom profile without touching built-in rules."""
    row = session.get(DCRateDuplicateProfile, profile_id)
    if row is None:
        raise DuplicateRuleError("Duplicate rule not found.")
    session.delete(row)
    session.flush()


def profile_applies(profile: DuplicateProfile, record: Mapping[str, Any]) -> bool:
    """Return whether a profile's schedule scope covers one rate record."""
    schedule_type = (_clean_text(record.get("schedule_type")) or "dc").casefold()
    land_type = (_clean_text(record.get("land_type")) or "").casefold()
    if profile.schedule_scope == "any":
        return True
    if profile.schedule_scope == "fbr":
        return schedule_type == "fbr"
    if profile.schedule_scope == "rural_dc":
        return schedule_type == "dc" and land_type == "rural"
    if profile.schedule_scope == "urban_dc":
        return schedule_type == "dc" and land_type == "urban"
    return False


def validate_profile_scope(
    profile: DuplicateProfile, *, schedule_type: str, land_type: str
) -> None:
    """Reject a profile that does not apply to the selected import schedule."""
    record = {"schedule_type": schedule_type, "land_type": land_type}
    if not profile_applies(profile, record):
        raise DuplicateRuleError(
            f"'{profile.name}' does not apply to the selected import schedule."
        )


def record_key(
    record: Mapping[str, Any], profile: DuplicateProfile
) -> tuple[Any, ...]:
    """Build the selected comparison key, including exact dates when requested."""
    values = tuple(
        _normalize_value(record.get(field))
        for field in profile.match_fields
    )
    if profile.date_mode == "exact":
        values = (
            *values,
            _normalize_value(_as_date(record.get("effective_from"))),
            _normalize_value(_as_date(record.get("effective_to"))),
        )
    return values


def records_match(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
    profile: DuplicateProfile,
) -> bool:
    """Compare two records using the profile's fields and date mode."""
    if record_key(first, profile) != record_key(second, profile):
        return False
    if profile.date_mode == "overlap":
        return _periods_overlap(first, second)
    return True


class DuplicateIndex:
    """Index records by profile key for deterministic duplicate detection."""

    def __init__(self, profile: DuplicateProfile):
        self.profile = profile
        self._groups: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}

    def find(self, record: Mapping[str, Any]) -> Mapping[str, Any] | None:
        if not profile_applies(self.profile, record):
            return None
        key = record_key(record, self.profile)
        if self.profile.date_mode == "overlap":
            return next(
                (
                    existing
                    for existing in self._groups.get(key, ())
                    if _periods_overlap(existing, record)
                ),
                None,
            )
        return next(iter(self._groups.get(key, ())), None)

    def add(self, record: Mapping[str, Any]) -> None:
        if not profile_applies(self.profile, record):
            return
        self._groups.setdefault(record_key(record, self.profile), []).append(record)


def find_duplicate_pairs(
    records: Sequence[Mapping[str, Any]], profile: DuplicateProfile
) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    """Return (survivor, duplicate) pairs, always preserving the lowest ID.

    Overlapping periods are handled sequentially: a record is removed only
    when it overlaps an already-kept survivor, never through a removed chain.
    """
    ordered = sorted(
        (record for record in records if profile_applies(profile, record)),
        key=lambda record: (
            record.get("id") is None,
            record.get("id") if record.get("id") is not None else 0,
        ),
    )
    survivors: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
    duplicates: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for record in ordered:
        key = record_key(record, profile)
        if profile.date_mode == "overlap":
            survivor = next(
                (
                    existing
                    for existing in survivors.get(key, ())
                    if _periods_overlap(existing, record)
                ),
                None,
            )
        else:
            survivor = next(iter(survivors.get(key, ())), None)
        if survivor is not None:
            duplicates.append((survivor, record))
        else:
            survivors.setdefault(key, []).append(record)
    return duplicates
