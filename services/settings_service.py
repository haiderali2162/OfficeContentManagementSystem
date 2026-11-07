"""Persistent key/value application settings.

Preferences are stored in the additive ``app_settings`` table as JSON text so
new page/tab options can be added without another schema change. Reads are
defensive: a missing table (for example in a very old database) or malformed
value falls back to the caller's default instead of breaking a page.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from string import Formatter
from typing import Any

from sqlalchemy import select

from core.database import SessionLocal
from core.models import AppSetting


CADASTRAL_STORAGE_KEY = "dc_rates.cadastral_storage"
CADASTRAL_STORAGE_MODES = ("ranges", "individual")
DEFAULT_CADASTRAL_STORAGE = "ranges"

FILE_LOCATION_KEY = "files.default_location"

CARD_APPEARANCE_KEY = "dc_rates.card_appearance"
DEFAULT_CARD_APPEARANCE = {
    "heading_template": "{schedule} schedule · {location}",
    "background_color": "#FFFFFF",
    "border_color": "#E2E8F0",
    "heading_color": "#0F172A",
    "body_color": "#475569",
}
CARD_COLOR_FIELDS = (
    "background_color",
    "border_color",
    "heading_color",
    "body_color",
)
CARD_COLOR_LABELS = {
    "background_color": "Card background",
    "border_color": "Card border",
    "heading_color": "Heading text",
    "body_color": "Body text",
}
CARD_HEADING_FIELDS = frozenset({
    "schedule",
    "location",
    "classification",
    "property_type",
    "district",
    "tehsil",
    "mauza",
    "land_type",
    "year",
})
_MAX_HEADING_LENGTH = 200
_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?$")


class SettingsError(ValueError):
    """Raised when a setting value is not supported."""


def get_setting(key: str, default: Any = None) -> Any:
    """Return a stored setting value, or ``default`` when unavailable."""
    try:
        with SessionLocal() as session:
            row = session.scalar(
                select(AppSetting).where(AppSetting.setting_key == key)
            )
            if row is None:
                return default
            try:
                return json.loads(row.value_json)
            except (TypeError, ValueError):
                return default
    except Exception:
        # A page must remain usable even if the settings table is temporarily
        # unavailable (for example before database initialization finishes).
        return default


def set_setting(key: str, value: Any) -> None:
    """Insert or update one setting value."""
    serialized = json.dumps(value)
    with SessionLocal.begin() as session:
        row = session.scalar(
            select(AppSetting).where(AppSetting.setting_key == key)
        )
        if row is None:
            session.add(AppSetting(setting_key=key, value_json=serialized))
        else:
            row.value_json = serialized


def get_cadastral_storage_mode() -> str:
    """Return ``ranges`` or ``individual`` for newly stored cadastral values."""
    value = get_setting(CADASTRAL_STORAGE_KEY, DEFAULT_CADASTRAL_STORAGE)
    return value if value in CADASTRAL_STORAGE_MODES else DEFAULT_CADASTRAL_STORAGE


def set_cadastral_storage_mode(mode: str) -> None:
    """Persist the cadastral storage preference for future imports/entries."""
    if mode not in CADASTRAL_STORAGE_MODES:
        raise SettingsError(
            "Cadastral storage must be 'ranges' or 'individual'."
        )
    set_setting(CADASTRAL_STORAGE_KEY, mode)


def normalize_directory_path(value: Any) -> str:
    """Validate an absolute existing folder and return its resolved path.

    An empty value intentionally clears the preference. Relative paths are
    rejected because the application's working directory can differ between
    launches and operating systems.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SettingsError("Default folder must be text.")
    value = value.strip()
    if not value:
        return ""
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise SettingsError("Default folder must be an absolute path.")
    resolved = path.resolve()
    if not resolved.exists():
        raise SettingsError("Default folder does not exist.")
    if not resolved.is_dir():
        raise SettingsError("Default folder must be a folder, not a file.")
    return str(resolved)


def get_default_file_location() -> str:
    """Return the saved default import/export folder, or an empty string."""
    stored = get_setting(FILE_LOCATION_KEY, "")
    try:
        return normalize_directory_path(stored)
    except SettingsError:
        # A folder may have been moved or deleted after it was saved. Treat the
        # preference as unset rather than breaking import/export actions.
        return ""


def set_default_file_location(path: Any) -> str:
    """Validate and persist the default import/export folder.

    An empty value clears the preference so export dialogs ask for a location.
    """
    normalized = normalize_directory_path(path)
    set_setting(FILE_LOCATION_KEY, normalized)
    return normalized


def _normalize_heading_template(template: Any) -> str:
    """Validate a card heading template and return its trimmed form.

    Only simple ``{name}`` placeholders from :data:`CARD_HEADING_FIELDS` are
    supported. Rejecting format specifications and attribute/index access keeps
    the value safe to apply to every rendered card and gives the settings page
    a precise error to show the user.
    """
    if not isinstance(template, str):
        raise SettingsError("Card heading template must be text.")
    template = template.strip()
    if not template:
        raise SettingsError("Card heading template cannot be empty.")
    if len(template) > _MAX_HEADING_LENGTH:
        raise SettingsError(
            f"Card heading template must be at most {_MAX_HEADING_LENGTH} characters."
        )
    try:
        parsed = list(Formatter().parse(template))
    except ValueError as exc:
        raise SettingsError(
            "Card heading template has an unmatched '{' or '}'."
        ) from exc
    for _literal, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if (
            field_name not in CARD_HEADING_FIELDS
            or format_spec
            or conversion
            or "." in field_name
            or "[" in field_name
        ):
            supported = ", ".join(
                f"{{{name}}}" for name in sorted(CARD_HEADING_FIELDS)
            )
            raise SettingsError(
                f"Unsupported placeholder '{{{field_name}}}'. "
                f"Use one of: {supported}."
            )
    return template


def _normalize_color(value: Any, field_name: str) -> str:
    """Return an upper-case ``#RRGGBB``/``#RRGGBBAA`` colour value."""
    if not isinstance(value, str):
        raise SettingsError(
            f"{CARD_COLOR_LABELS[field_name]} must be a hex colour."
        )
    value = value.strip()
    if not _HEX_COLOR.fullmatch(value):
        raise SettingsError(
            f"{CARD_COLOR_LABELS[field_name]} must be a hex colour "
            "such as #FFFFFF."
        )
    return value.upper()


def normalize_card_appearance(values: dict[str, Any] | None) -> dict[str, str]:
    """Validate and merge card appearance values with the defaults.

    Partial dictionaries are accepted so callers can update one option without
    restating the rest. Unknown keys are ignored deliberately; stored settings
    from a newer build must not stop an older page from rendering.
    """
    if values is None:
        values = {}
    if not isinstance(values, dict):
        raise SettingsError("Card appearance must be a mapping of settings.")
    merged = dict(DEFAULT_CARD_APPEARANCE)
    for key in DEFAULT_CARD_APPEARANCE:
        if key in values:
            merged[key] = values[key]
    merged["heading_template"] = _normalize_heading_template(
        merged["heading_template"]
    )
    for field_name in CARD_COLOR_FIELDS:
        merged[field_name] = _normalize_color(merged[field_name], field_name)
    return merged


def get_card_appearance() -> dict[str, str]:
    """Return the saved card appearance, falling back field-by-field.

    A malformed stored value never stops the rates page: each unusable field is
    replaced by its default while valid fields are still honoured.
    """
    stored = get_setting(CARD_APPEARANCE_KEY, None)
    if not isinstance(stored, dict):
        return dict(DEFAULT_CARD_APPEARANCE)

    appearance = dict(DEFAULT_CARD_APPEARANCE)
    if "heading_template" in stored:
        try:
            appearance["heading_template"] = _normalize_heading_template(
                stored["heading_template"]
            )
        except SettingsError:
            pass
    for field_name in CARD_COLOR_FIELDS:
        if field_name in stored:
            try:
                appearance[field_name] = _normalize_color(
                    stored[field_name], field_name
                )
            except SettingsError:
                pass
    return appearance


def set_card_appearance(values: dict[str, Any]) -> dict[str, str]:
    """Validate and persist rate-card heading and colour preferences.

    A partial dictionary updates only the supplied fields; omitted fields keep
    their currently saved values.
    """
    if not isinstance(values, dict):
        raise SettingsError("Card appearance must be a mapping of settings.")
    merged = get_card_appearance()
    merged.update({
        key: values[key]
        for key in DEFAULT_CARD_APPEARANCE
        if key in values
    })
    appearance = normalize_card_appearance(merged)
    set_setting(CARD_APPEARANCE_KEY, appearance)
    return appearance


def reset_card_appearance() -> dict[str, str]:
    """Restore and persist the default rate-card appearance."""
    appearance = dict(DEFAULT_CARD_APPEARANCE)
    set_setting(CARD_APPEARANCE_KEY, appearance)
    return appearance


def format_card_heading(template: str, record: dict) -> str:
    """Apply a heading template to one rate record.

    ``record`` follows the dictionaries returned by the DC-rate service. An
    invalid template falls back to the default heading so a corrupted stored
    setting cannot break the rate list.
    """
    try:
        template = _normalize_heading_template(template)
    except SettingsError:
        template = DEFAULT_CARD_APPEARANCE["heading_template"]

    schedule = str(record.get("schedule_type") or "dc").upper()
    values = {
        "schedule": schedule,
        "location": record.get("location_name") or "Unknown location",
        "classification": (
            record.get("classification") or record.get("property_type") or ""
        ),
        "property_type": record.get("property_type") or "",
        "district": record.get("district") or "",
        "tehsil": record.get("tehsil") or "",
        "mauza": record.get("mauza") or "",
        "land_type": record.get("land_type") or "",
        "year": str(record.get("published_year") or "Not supplied"),
    }
    return template.format_map(values)
