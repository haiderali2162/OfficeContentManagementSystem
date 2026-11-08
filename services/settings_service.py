"""Persistent key/value application settings.

Preferences are stored in the additive ``app_settings`` table as JSON text so
new page/tab options can be added without another schema change. Reads are
defensive: a missing table (for example in a very old database) or malformed
value falls back to the caller's default instead of breaking a page.

Two DC/FBR rate-card preference groups live here:

* ``CARD_APPEARANCE_KEY`` — heading template and colours (see
  :func:`get_card_appearance`).
* ``CARD_FIELDS_KEY`` — which body fields are shown on each card and optional
  formula lines that compute derived values from the schedule row (see
  :func:`get_card_fields`).
"""

from __future__ import annotations

import ast
import json
import operator as operator_module
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from string import Formatter
from typing import Any

from sqlalchemy import select

from core.database import SessionLocal
from core.models import DC_RATE_UNIT_LABELS
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


# --------------------------------------------------------------------------
# Rate-card content (chosen body fields + formula-calculated lines).
#
# Requirement source: improvements.txt item about letting the user decide what
# information appears on the schedule card, including formula-computed values
# such as a per-kanal equivalent derived from the per-marla rate.
#
# The stored shape under CARD_FIELDS_KEY is:
#     {"fields":    ["classification", "rate", ...],   # subset, order kept
#      "formulas":  [{"label": "...", "expression": "..."}, ...]}
# An EMPTY field list (the default) intentionally keeps the built-in card
# layout, so existing cards do not change until the user customises them.
# --------------------------------------------------------------------------

CARD_FIELDS_KEY = "dc_rates.card_fields"

#: Body fields the user may put on a card, in the order offered. Values are
#: read from the DC-rate record dictionaries; ``None``/empty values are
#: skipped on render so empty records do not produce stray ``Label:`` text.
CARD_BODY_FIELD_LABELS = {
    "classification": "Classification",
    "rate": "Rate (value + unit)",
    "rate_unit": "Unit",
    "floor": "Floor",
    "location_summary": "Location summary",
    "land_type": "Land type",
    "district": "District",
    "tehsil": "Tehsil",
    "qanoongoi": "Qanoongoi",
    "mauza": "Mauza",
    "committee_town": "Committee / town",
    "revenue_circle": "Revenue circle",
    "property_area": "Property area",
    "locality": "Locality",
    "hadbast_number": "Hadbast no.",
    "serial_no": "Serial no.",
    "square_no": "Square no.",
    "kila_no": "Kila no.",
    "khasra_no": "Khasra no.",
    "published_year": "Published year",
    "source_reference": "Source reference",
    "effective_from": "Effective from",
    "effective_to": "Effective to",
    "verification_status": "Verification",
}

#: Record keys usable inside formula expressions. Only plain numbers (values
#: convertible to :class:`decimal.Decimal`) make sense in arithmetic, so the
#: whitelist is deliberately small and numeric.
FORMULA_FIELDS = (
    "rate_value",
    "published_year",
    "serial_no",
    "hadbast_number",
    "structure_rate",
)

#: Guard rails for stored expressions: a card renders on every list draw, so
#: expressions stay short and few.
_MAX_FORMULA_LENGTH = 120
_MAX_FORMULA_LINES = 6
_MAX_FORMULA_LABEL_LENGTH = 60


class CardFormulaError(ValueError):
    """Raised when a card formula is not a supported numeric expression."""


def _format_formula_decimal(value: Decimal) -> str:
    """Render a formula result without exponent or trailing zeros.

    ``Decimal.normalize()`` would turn 250000 into ``2.5E+5``; the explicit
    fixed-point format keeps card text plain and readable. Display results are
    rounded to four decimal places only for the card text — the underlying
    computation always keeps full Decimal precision.
    """
    try:
        rendered = value.quantize(Decimal("0.0001"))
    except InvalidOperation:
        # Values too large for that scale (scientific presets) keep full form.
        rendered = value
    text = format(rendered, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _formula_number(record: dict, name: str) -> Decimal:
    """Convert one whitelisted record value to a Decimal for formula use."""
    value = record.get(name)
    if value is None:
        raise CardFormulaError(f"{{{name}}} is empty for this record.")
    # Values are stored as Numeric (Decimal) or text/ints; Decimal(str(value))
    # avoids constructing from a rounded float per the project's Decimal rule.
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise CardFormulaError(
            f"{{{name}}} is not a usable number ('{value}')."
        ) from exc


#: Arithmetic operators a card formula may use, mapped to their implementations.
#: Everything else (power, modulo, bitwise, comparisons, function calls) is
#: deliberately rejected so a formula can never execute general Python.
_FORMULA_BINOPS = {
    ast.Add: operator_module.add,
    ast.Sub: operator_module.sub,
    ast.Mult: operator_module.mul,
    ast.Div: operator_module.truediv,
}

#: Card formulas write placeholders in the same ``{rate_value}`` style as the
#: heading template, but to Python a brace is a set literal, so the braces are
#: stripped before parsing. Each match must be a bare well-formed field name;
#: any brace that does not belong to a placeholder is rejected afterwards.
_FORMULA_PLACEHOLDER = re.compile(r"\{([A-Za-z_]\w*)\}")


def _prepare_formula_source(expression: str) -> str:
    """Turn ``{rate_value} * 20`` into the ``rate_value * 20`` Python form.

    Stray braces (an accidental ``{速率}`` or a set literal such as
    ``{1, 2}``) raise instead of being interpreted as data, keeping the
    formula grammar small and predictable.
    """
    prepared = _FORMULA_PLACEHOLDER.sub(r"\1", expression)
    if "{" in prepared or "}" in prepared:
        raise CardFormulaError(
            "Use whole {field} placeholders — braces around calculations "
            "or anything else are not supported."
        )
    return prepared


def evaluate_card_formula(expression: str, record: dict) -> Decimal:
    """Evaluate one arithmetic card formula against a rate record.

    Only four arithmetic operations, parentheses, sign, and the whitelisted
    ``{field}`` names in :data:`FORMULA_FIELDS` are allowed. The expression is
    parsed with Python's :mod:`ast` module and every disallowed construct
    (function calls, attribute access, strings, keywords, comparisons) is
    rejected, so a formula can never execute arbitrary Python or query data.

    Args:
        expression: formula text such as ``"{rate_value} * 20"``.
        record: one DC-rate record dictionary.
    Returns:
        The computed value as a :class:`~decimal.Decimal`.
    Raises:
        CardFormulaError: for syntax errors, disallowed constructs, unknown
            field names, missing/non-numeric record values, or division by
            zero.
    """
    try:
        tree = ast.parse(_prepare_formula_source(expression), mode="eval")
    except SyntaxError as exc:
        raise CardFormulaError(
            "Formula syntax is not valid: only numbers, {field} placeholders, "
            "+ - * / and ( ) are allowed."
        ) from exc

    # Strict whitelist below: each accepted node type is listed explicitly and
    # every node that falls through raises, so unintended constructs cannot
    # pass through unnoticed.
    def _eval(node) -> Decimal:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.BinOp):
            operator_fn = _FORMULA_BINOPS.get(type(node.op))
            if operator_fn is None:
                raise CardFormulaError("Only + - * / calculations are supported.")
            left = _eval(node.left)
            right = _eval(node.right)
            if isinstance(node.op, ast.Div) and right == 0:
                raise CardFormulaError("A formula divides by zero.")
            return operator_fn(left, right)
        if isinstance(node, ast.UnaryOp):
            operand = _eval(node.operand)
            if isinstance(node.op, ast.UAdd):
                return operand
            if isinstance(node.op, ast.USub):
                return -operand
            raise CardFormulaError("Only + and - signs are supported.")
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(
                node.value, (int, float)
            ):
                raise CardFormulaError("Only plain numbers may appear in a formula.")
            return Decimal(str(node.value))
        if isinstance(node, ast.Name):
            if node.id not in FORMULA_FIELDS:
                supported = ", ".join(f"{{{name}}}" for name in FORMULA_FIELDS)
                raise CardFormulaError(
                    f"Unsupported placeholder {{{node.id}}}. Use one of: {supported}."
                )
            return _formula_number(record, node.id)
        raise CardFormulaError(
            "Formulas support only numbers, {field} placeholders, + - * / and ( )."
        )

    return _eval(tree.body)


def validate_card_formula(expression: Any) -> str:
    """Validate a formula without a real record and return the cleaned text.

    Every whitelisted placeholder resolves to ``Decimal("1")`` during
    validation, which proves the syntax and the field names without touching
    any data. Stored formulas are validated with this helper when saved; the
    per-record evaluation may still fail later (a missing rate value, for
    example) and the card renders ``—`` for that line.
    """
    if not isinstance(expression, str):
        raise SettingsError("A card formula must be text.")
    expression = expression.strip()
    if not expression:
        raise SettingsError("A card formula cannot be empty.")
    if len(expression) > _MAX_FORMULA_LENGTH:
        raise SettingsError(
            f"A card formula must be at most {_MAX_FORMULA_LENGTH} characters."
        )
    try:
        evaluate_card_formula(expression, {
            name: Decimal("1") for name in FORMULA_FIELDS
        })
    except CardFormulaError as exc:
        raise SettingsError(
            f"Card formula problem ({expression}): {exc}"
        ) from exc
    return expression


def normalize_card_fields(values: Any) -> dict[str, Any]:
    """Validate and complete a card-content configuration.

    Unknown field keys raise :class:`SettingsError`; duplicate fields collapse
    to their first occurrence so the remaining order stays meaningful. An
    empty configuration is the default ("keep the built-in card layout").
    """
    if values is None:
        values = {}
    if not isinstance(values, dict):
        raise SettingsError("Card content must be a mapping of settings.")
    fields_raw = values.get("fields") or []
    if not isinstance(fields_raw, (list, tuple)):
        raise SettingsError("Card fields must be a list of field names.")
    fields: list[str] = []
    for field_name in fields_raw:
        if field_name not in CARD_BODY_FIELD_LABELS:
            supported = ", ".join(sorted(CARD_BODY_FIELD_LABELS))
            raise SettingsError(
                f"Unsupported card field '{field_name}'. Use one of: {supported}."
            )
        if field_name not in fields:
            fields.append(field_name)
    formulas_raw = values.get("formulas") or []
    if not isinstance(formulas_raw, (list, tuple)):
        raise SettingsError("Card formulas must be a list of formula lines.")
    if len(formulas_raw) > _MAX_FORMULA_LINES:
        raise SettingsError(
            f"Use at most {_MAX_FORMULA_LINES} formula lines per card."
        )
    formulas = []
    for formula_raw in formulas_raw:
        if not isinstance(formula_raw, dict):
            raise SettingsError("Each formula line needs a label and expression.")
        label = formula_raw.get("label")
        if not isinstance(label, str) or not label.strip():
            raise SettingsError("Every formula line needs a label.")
        label = label.strip()
        if len(label) > _MAX_FORMULA_LABEL_LENGTH:
            raise SettingsError(
                f"Formula labels must be at most {_MAX_FORMULA_LABEL_LENGTH} characters."
            )
        formulas.append({
            "label": label,
            "expression": validate_card_formula(formula_raw.get("expression")),
        })
    return {"fields": fields, "formulas": formulas}


def get_card_fields() -> dict[str, Any]:
    """Return the saved card-content configuration, falling back safely.

    A stored config from a newer build or one malformed entry never breaks the
    rates page: unusable parts are replaced by the default (which renders the
    built-in card layout), while valid parts are still honoured.
    """
    stored = get_setting(CARD_FIELDS_KEY, None)
    try:
        return normalize_card_fields(stored)
    except SettingsError:
        return {"fields": [], "formulas": []}


def set_card_fields(values: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist the card-content configuration."""
    normalized = normalize_card_fields(values)
    set_setting(CARD_FIELDS_KEY, normalized)
    return normalized


def reset_card_fields() -> dict[str, Any]:
    """Restore the built-in card layout (empty field and formula lists)."""
    normalized = {"fields": [], "formulas": []}
    set_setting(CARD_FIELDS_KEY, normalized)
    return normalized


def _card_field_value(field_name: str, record: dict) -> str:
    """Format one chosen body field for a card; empty values are skipped.

    The special keys ``rate``/``rate_unit``/``location_summary`` reproduce the
    pieces of the built-in card so a custom card can keep (or drop) each part
    deliberately.
    """
    if field_name == "rate":
        value = record.get("rate_value")
        unit = record.get("rate_unit")
        if value is None:
            return ""
        return (
            f"{value} {DC_RATE_UNIT_LABELS.get(unit, unit)}"
            if unit in DC_RATE_UNIT_LABELS
            else str(value)
        )
    if field_name == "rate_unit":
        unit = record.get("rate_unit") or ""
        return DC_RATE_UNIT_LABELS.get(unit, str(unit))
    if field_name == "location_summary":
        # Same administrative pieces the built-in card shows: land type,
        # district, tehsil, committee/town, revenue circle, and Mauza.
        return " / ".join(
            str(part) for part in (
                record.get("land_type"),
                record.get("district"),
                record.get("tehsil"),
                record.get("committee_town"),
                record.get("revenue_circle"),
                record.get("mauza"),
            ) if part
        )
    if field_name in {"effective_from", "effective_to"}:
        value = record.get(field_name)
        return value.isoformat() if value else ""
    if field_name == "verification_status":
        return str(record.get("verification_status") or "").strip()
    value = record.get(field_name)
    return str(value).strip() if value is not None and str(value).strip() else ""


def format_card_body_lines(record: dict, config: dict | None = None) -> list[str]:
    """Build the custom body lines for one rate card.

    Selected content fields (in the saved order) form one wrapped line of
    ``Label: value`` segments; each formula line follows as
    ``<label>: <computed value>``. A formula that cannot be evaluated for the
    record renders as ``label: —`` so the card stays honest without failing.

    An empty configuration returns ``[]`` and the caller falls back to the
    built-in card layout, which keeps existing cards unchanged.
    """
    config = config or get_card_fields()
    lines: list[str] = []
    segments = []
    for field_name in config.get("fields", []):
        value = _card_field_value(field_name, record)
        if value:
            segments.append(
                f"{CARD_BODY_FIELD_LABELS[field_name]}: {value}"
            )
    if segments:
        lines.append(" · ".join(segments))
    for formula in config.get("formulas", []):
        try:
            value = _format_formula_decimal(
                evaluate_card_formula(formula["expression"], record)
            )
        except CardFormulaError:
            # A record without a numeric value (legacy import row, for
            # example) must not break the card; show the gap honestly.
            value = "—"
        lines.append(f"{formula['label']}: {value}")
    return lines
