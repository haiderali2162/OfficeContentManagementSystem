"""Naqsha Alif / Bay / Jeem module — step 1 (developer objective).

Import land and owner records from the Punjab Zameen portal or
spreadsheets, validate and normalize them, and store them hierarchically
(District → Tehsil → Qanoongoi → Mauza → Khewat No.) so other modules can
reuse them later.

Two datasets are handled:

* Dataset A — Land Details: Khewat No., Khatoni No., Square No. (Murabba),
  Killa No., Type of Land (Qism Arazi), Area (Kanal-Marla-Sarsahi / sq ft).
* Dataset B — Owner Details: Owner Name & relation/parentage, Caste
  (Qaum), Share (Hissa), Area according to share (Raqba). Extra portal
  columns are ignored.

Per khewat the service totals both datasets, compares them (checkage),
and recalculates every owner's share from their area against the khewat
total (Hissa correction), because portal share values are often corrupted.

Areas are normalized to exact decimal marla through the existing area
engine (``core.area_engine.parse_area_expression``), so K-M-F, "10k5m",
"5m", "100f", and plain kanal numbers are all accepted.
"""

from __future__ import annotations

import csv
import io
import re
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from core.area_engine import AreaError, AreaSettings, parse_area_expression
from core.database import SessionLocal
from core.models import NaqshaLandRow, NaqshaOwnerRow, NaqshaRecord
from core.search import like_pattern

# Hierarchy fields saved on every record (order matters for display).
LOCATION_FIELDS = ("district", "tehsil", "qanoongoi", "mauza", "province")

# Required columns per dataset (used for auto-mapping and validation).
LAND_FIELDS = (
    "khewat_no", "khatoni_no", "square_no", "killa_no", "land_type", "area",
)
OWNER_FIELDS = ("khewat_no", "owner_name", "caste", "share", "area")

LAND_FIELD_LABELS = {
    "khewat_no": "Khewat No.",
    "khatoni_no": "Khatoni No.",
    "square_no": "Square No. (Murabba)",
    "killa_no": "Killa No.",
    "land_type": "Type of Land (Qism Arazi)",
    "area": "Area (K-M-S / Sq. Ft.)",
}
OWNER_FIELD_LABELS = {
    "khewat_no": "Khewat No. (optional)",
    "owner_name": "Owner Name & Relation/Parentage",
    "caste": "Owner Caste (Qaum)",
    "share": "Owner Share (Hissa)",
    "area": "Owner Area according to share (Raqba)",
}

# Header keywords for auto-mapping (developer request: detect columns
# automatically, let the user override, and ignore everything else).
_LAND_FIELD_KEYWORDS = {
    "khewat_no": ("khewat", "khewt", "khatooni number field is separate"),
    "khatoni_no": ("khatoni", "khatooni"),
    "square_no": ("murabba", "square", "sq no", "sq. no"),
    "killa_no": ("killa", "kila", "killah"),
    "land_type": ("qism", "type of land", "land type", "class"),
    "area": ("raqba", "area", "kanal"),
}
_OWNER_FIELD_KEYWORDS = {
    "khewat_no": ("khewat", "khewt"),
    "owner_name": ("name of owner", "owner name", "name", "malik"),
    "caste": ("qaum", "caste", "biradari", "tribe"),
    "share": ("hissa", "share", "portion"),
    "area": ("raqba", "area according", "area"),
}
# Parentage markers copied from the portal (Name s/o d/o w/o Father).
_RELATION_PATTERN = re.compile(
    r"^(?P<name>.+?)\s*"
    r"(?:s/?/?o|s\.?o\.?|d/?/?o|d\.?o\.?|w/?/?o|w\.?o\.?|widow\s+of)\s*"
    r"(?P<rest>.+)$",
    re.IGNORECASE,
)
_RELATION_TOKENS = ("widow of", "s/o", "d/o", "w/o", "so", "do", "wo")

_AREA_SETTINGS = AreaSettings()
SARSAHI_PER_MARLA = Decimal(_AREA_SETTINGS.sarsahi_per_marla)


class NaqshaError(ValueError):
    """Raised when Naqsha data cannot be parsed, validated, or stored."""


# ---------------------------------------------------------------------------
# Dataset ingestion: pasted portal tables and spreadsheet files
# ---------------------------------------------------------------------------


def parse_pasted_table(text: Any) -> tuple[list[str], list[list[str]]]:
    """Parse a table pasted from the Punjab Zameen portal or a spreadsheet.

    Tab-separated lines (the portal's copy format) are split on tabs;
    otherwise comma-separated values are tried. The first non-empty line
    is the header row. Blank lines and columns are dropped.
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    if not isinstance(text, str) or not text.strip():
        raise NaqshaError("Paste the table rows first — nothing to import.")
    lines = [line for line in text.replace("\r\n", "\n").split("\n") if line.strip()]
    if len(lines) < 2:
        raise NaqshaError(
            "The pasted table needs a header row and at least one data row."
        )
    if "\t" in lines[0]:
        rows = [line.split("\t") for line in lines]
    else:
        try:
            rows = list(csv.reader(io.StringIO("\n".join(lines))))
        except csv.Error as exc:
            raise NaqshaError(
                "The pasted table could not be read as tab- or "
                f"comma-separated data: {exc}"
            ) from exc
    headers = [
        str(cell).replace("\xa0", " ").strip() for cell in rows[0]
    ]
    # Drop columns that are empty in the header AND every row (portal
    # copies often carry trailing tabs).
    column_count = max(len(row) for row in rows)
    keep = []
    for index in range(column_count):
        header = headers[index] if index < len(headers) else ""
        values = [row[index] for row in rows[1:] if index < len(row)]
        if header or any(str(value).strip() for value in values):
            keep.append(index)
    kept_headers = [headers[i] if i < len(headers) else f"Column {i + 1}" for i in keep]
    kept_rows = [
        [str(row[i]).replace("\xa0", " ").strip() if i < len(row) else "" for i in keep]
        for row in rows[1:]
    ]
    kept_rows = [row for row in kept_rows if any(cell for cell in row)]
    if not kept_rows:
        raise NaqshaError("The pasted table has no data rows under its header.")
    return kept_headers, kept_rows


def suggest_column_mapping(
    headers: Iterable[Any], dataset: str
) -> dict[str, int | None]:
    """Auto-map table columns to the required fields of one dataset.

    Headers are matched against field keywords (Qism, Khewat, Raqba, ...).
    Each field maps to at most one column and each column is used once;
    unrecognized columns are left unmapped and simply ignored. The result
    feeds the mapping UI where every assignment can be overridden.
    """
    keyword_map = (
        _LAND_FIELD_KEYWORDS if dataset == "land" else _OWNER_FIELD_KEYWORDS
    )
    fields = LAND_FIELDS if dataset == "land" else OWNER_FIELDS
    normalized = [
        re.sub(r"\s+", " ", str(header)).strip().casefold()
        for header in headers
    ]
    mapping: dict[str, int | None] = {field: None for field in fields}
    used: set[int] = set()
    # Exact keyword-in-header matches first, in field order.
    for field in fields:
        for keyword in keyword_map[field]:
            if any(
                keyword in header and index not in used
                for index, header in enumerate(normalized)
            ):
                mapping[field] = next(
                    index for index, header in enumerate(normalized)
                    if keyword in header and index not in used
                )
                used.add(mapping[field])
                break
    return mapping


def parse_owner_name(raw: Any) -> tuple[str, str, str]:
    """Split '[Owner] s/o|d/o|w/o|widow of [Parent]' into its parts.

    The relation token is stored lowercase ("s/o", "d/o", "w/o",
    "widow of"). An unparseable value keeps the whole text as the name.
    """
    text = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not text:
        return "", "", ""
    match = _RELATION_PATTERN.match(text)
    if not match:
        return text, "", ""
    name = match.group("name").strip(" -:،")
    rest = match.group("rest").strip(" -:،")
    lowered = text.casefold()
    relation = ""
    for token in _RELATION_TOKENS:
        if token in lowered:
            relation = token
            break
    if relation == "so":
        relation = "s/o"
    elif relation == "do":
        relation = "d/o"
    elif relation == "wo":
        relation = "w/o"
    return name, relation, rest


def parse_area_marla(raw: Any) -> Decimal | None:
    """Normalize an area cell to exact decimal marla (None when blank).

    Accepts K-M-F ("10-10-0"), "10k5m", "5m", "100f", and plain kanal
    numbers — the same formats the deed parties use. Raises
    :class:`NaqshaError` for text that looks like an area but cannot be
    parsed, so preview problems are explicit per row.
    """
    text = str(raw or "").replace("\xa0", " ").strip()
    if not text:
        return None
    try:
        return parse_area_expression(text, settings=_AREA_SETTINGS)
    except AreaError as exc:
        raise NaqshaError(f"Cannot read the area '{text}': {exc}") from exc


def _clean_cell(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def build_land_rows(
    headers: list[str], rows: list[list[Any]], mapping: dict[str, int | None],
) -> list[dict[str, Any]]:
    """Turn mapped land-dataset rows into normalized row dictionaries.

    Every row keeps its original table position (``row_number``) so import
    problems can be reported with useful row numbers.
    """
    parsed: list[dict[str, Any]] = []
    for position, row in enumerate(rows, start=2):  # header is row 1
        values = {field: _clean_cell(_cell(row, mapping.get(field))) for field in LAND_FIELDS}
        area_marla: Decimal | None = None
        problem = ""
        try:
            area_marla = parse_area_marla(values["area"])
        except NaqshaError as exc:
            problem = str(exc)
        if not values["khewat_no"]:
            problem = problem or "Khewat No. is empty — the row cannot be grouped."
        parsed.append({
            "row_number": position,
            "khewat_no": values["khewat_no"],
            "khatoni_no": values["khatoni_no"],
            "square_no": values["square_no"],
            "killa_no": values["killa_no"],
            "land_type": values["land_type"],
            "area_text": values["area"],
            "area_marla": area_marla,
            "problem": problem,
        })
    return parsed


def build_owner_rows(
    headers: list[str], rows: list[list[Any]], mapping: dict[str, int | None],
) -> list[dict[str, Any]]:
    """Turn mapped owner-dataset rows into normalized row dictionaries.

    The Khewat column is optional: when the owner table carries one, rows
    are paired with the matching land khewat; otherwise they attach to the
    only khewat of the land dataset (see ``build_naqsha_preview``).
    """
    parsed: list[dict[str, Any]] = []
    for position, row in enumerate(rows, start=2):
        values = {field: _clean_cell(_cell(row, mapping.get(field))) for field in OWNER_FIELDS}
        name, relation, parent = parse_owner_name(values["owner_name"])
        area_marla: Decimal | None = None
        problem = ""
        try:
            area_marla = parse_area_marla(values["area"])
        except NaqshaError as exc:
            problem = str(exc)
        parsed.append({
            "row_number": position,
            "khewat_no": values["khewat_no"],
            "owner_name": name,
            "relation": relation,
            "parent_name": parent,
            "owner_name_raw": values["owner_name"],
            "caste": values["caste"],
            "share_text": values["share"],
            "area_text": values["area"],
            "area_marla": area_marla,
            "problem": problem,
        })
    return parsed


def _cell(row: list[Any], index: int | None) -> Any:
    if index is None or index < 0 or index >= len(row):
        return ""
    return row[index]


# ---------------------------------------------------------------------------
# Per-khewat calculations: totals, checkage, share normalization
# ---------------------------------------------------------------------------


def _marla_text(area_marla: Decimal | None) -> str:
    """Format an exact marla value for display (2 decimals at most)."""
    if area_marla is None:
        return "—"
    quantized = area_marla.quantize(Decimal("0.01"))
    text = f"{quantized.normalize():f}"
    return text


def corrected_share_text(
    owner_marla: Decimal | None, total_marla: Decimal | None,
) -> tuple[str, int | None, int | None]:
    """Recompute an owner's share from their area and the khewat total.

    Returns ``(text, numerator, denominator)``. The fraction is shown as
    whole marlas when both sides are whole ("20/80"), otherwise in whole
    sarsahis, otherwise as the exact reduced fraction.
    """
    if owner_marla is None or total_marla is None or total_marla <= 0:
        return "", None, None
    if owner_marla < 0:
        raise NaqshaError("Owner areas cannot be negative.")
    owner_fraction = Fraction(owner_marla) / Fraction(total_marla)
    _, owner_den = owner_marla.as_integer_ratio()
    _, total_den = total_marla.as_integer_ratio()
    if owner_den == 1 and total_den == 1:
        return f"{int(owner_marla)}/{int(total_marla)}", int(owner_marla), int(total_marla)
    owner_sarsahi = owner_marla * SARSAHI_PER_MARLA
    total_sarsahi = total_marla * SARSAHI_PER_MARLA
    _, owner_sarsahi_den = owner_sarsahi.as_integer_ratio()
    _, total_sarsahi_den = total_sarsahi.as_integer_ratio()
    if owner_sarsahi_den == 1 and total_sarsahi_den == 1:
        return (
            f"{int(owner_sarsahi)}/{int(total_sarsahi)}",
            int(owner_sarsahi), int(total_sarsahi),
        )
    return (
        f"{owner_fraction.numerator}/{owner_fraction.denominator}",
        owner_fraction.numerator, owner_fraction.denominator,
    )


def group_land_rows(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group rows by Khewat No. (case-insensitive, order preserved)."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["khewat_no"].strip().casefold(), []).append(row)
    return groups


def _sum_marla(rows: Iterable[dict[str, Any]]) -> Decimal | None:
    total = Decimal("0")
    counted = False
    for row in rows:
        value = row.get("area_marla")
        if value is None:
            continue
        total += value
        counted = True
    return total if counted else None


def summarize_khewat_group(
    land_rows: list[dict[str, Any]],
    owner_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Totals, checkage, and corrected shares for one khewat group.

    ``checkage`` is ``"1/1"`` when the owner total equals the land total;
    otherwise it is the mismatch ratio (Total Owner Area / Total Land
    Area) with a note naming which total is off.
    """
    land_total = _sum_marla(land_rows)
    owner_total = _sum_marla(owner_rows)
    if land_total is None and owner_total is None:
        status, ratio = "no data", ""
    elif land_total is not None and owner_total is not None:
        if land_total == owner_total:
            status, ratio = "valid", "1/1"
        else:
            status, ratio = "mismatch", (
                f"{_marla_text(owner_total)}/{_marla_text(land_total)}"
            )
    elif land_total is not None:
        status, ratio = "owners missing", ""
    else:
        status, ratio = "land missing", ""
    corrected = []
    for owner in owner_rows:
        share, numerator, denominator = corrected_share_text(
            owner.get("area_marla"), land_total,
        )
        corrected.append({
            **owner,
            "share_numerator": numerator,
            "share_denominator": denominator,
            "share_text_corrected": share,
        })
    return {
        "khewat_no": land_rows[0]["khewat_no"] if land_rows else (
            owner_rows[0].get("khewat_no", "") if owner_rows else ""
        ),
        "land_rows": land_rows,
        "owner_rows": corrected,
        "land_total_marla": land_total,
        "land_total_text": _marla_text(land_total),
        "owner_total_marla": owner_total,
        "owner_total_text": _marla_text(owner_total),
        "checkage_status": status,
        "checkage_ratio": ratio,
        "problems": [
            f"Row {row['row_number']}: {row['problem']}"
            for row in [*land_rows, *owner_rows] if row.get("problem")
        ],
    }


def build_naqsha_preview(
    land_rows: list[dict[str, Any]],
    owner_rows: list[dict[str, Any]],
    *,
    unmatched_owner_khewat: str = "",
) -> list[dict[str, Any]]:
    """Group both datasets by khewat and summarize each group.

    Owner rows carrying a khewat number are paired with the matching land
    khewat; owner rows without one attach to the only khewat when the
    land dataset has exactly one, otherwise they are reported as
    unmatched so nothing is guessed. When the caller passes
    ``unmatched_owner_khewat`` (the import UI asks the user), unmatched
    owner rows join that khewat instead.
    """
    groups = group_land_rows(land_rows)
    khewat_numbers = list(groups)
    paired: dict[str, list[dict[str, Any]]] = {key: [] for key in groups}
    unmatched: list[dict[str, Any]] = []
    forced_key = str(unmatched_owner_khewat or "").strip().casefold()
    for row in owner_rows:
        key = str(row.get("khewat_no") or "").strip().casefold()
        if key and key in groups:
            paired[key].append(row)
        elif len(khewat_numbers) == 1:
            paired[khewat_numbers[0]].append(row)
        elif forced_key and forced_key in groups:
            paired[forced_key].append(row)
        else:
            unmatched.append(row)
    summaries = [
        summarize_khewat_group(rows, paired[key])
        for key, rows in groups.items()
    ]
    if unmatched:
        orphan = summarize_khewat_group([], unmatched)
        orphan["khewat_no"] = "(khewat not in the land dataset)"
        summaries.append(orphan)
    return summaries


# ---------------------------------------------------------------------------
# Hierarchical storage (District -> Tehsil -> Qanoongoi -> Mauza -> Khewat)
# ---------------------------------------------------------------------------


def _validated_context(values: dict[str, Any]) -> dict[str, str]:
    mauza = _clean_cell(values.get("mauza"))
    if not mauza:
        raise NaqshaError("Select or type the Mauza before saving.")
    khewat_no = _clean_cell(values.get("khewat_no"))
    if not khewat_no:
        raise NaqshaError("Enter the Khewat No. before saving.")
    if len(khewat_no) > 100:
        raise NaqshaError("Khewat No. cannot exceed 100 characters.")
    return {
        "province": _clean_cell(values.get("province")),
        "district": _clean_cell(values.get("district")),
        "tehsil": _clean_cell(values.get("tehsil")),
        "qanoongoi": _clean_cell(values.get("qanoongoi")),
        "mauza": mauza,
        "khewat_no": khewat_no,
        "khatoni_no": _clean_cell(values.get("khatoni_no")),
        "source_note": _clean_cell(values.get("source_note")),
    }


def save_naqsha_records(
    context: dict[str, Any],
    groups: list[dict[str, Any]],
) -> dict[str, int]:
    """Save the previewed khewat groups under the location hierarchy.

    Each group becomes one :class:`NaqshaRecord` with its land and owner
    rows. Re-saving a khewat that already exists in the same mauza keeps
    the record and skips rows that are identical to stored ones (portal
    re-copies must never duplicate data); changed or new rows are added.
    Returns ``{"records", "land_rows_added", "owner_rows_added", "skipped"}``.
    """
    if not groups:
        raise NaqshaError("Nothing to save: the preview is empty.")
    saved_records = 0
    land_added = 0
    owner_added = 0
    skipped = 0
    for group in groups:
        khewat_no = _clean_cell(group.get("khewat_no"))
        if not khewat_no or khewat_no.startswith("("):
            raise NaqshaError(
                "Every land row needs a Khewat No. before saving "
                f"(group '{group.get('khewat_no') or '?'}')."
            )
        values = _validated_context({**context, "khewat_no": khewat_no})
        with SessionLocal.begin() as session:
            # Match on exactly the unique-constraint fields so a re-import
            # with a differently-typed province still finds the record.
            record = session.scalar(select(NaqshaRecord).where(
                *(getattr(NaqshaRecord, field) == value
                  for field, value in values.items()
                  if field in ("district", "tehsil", "qanoongoi", "mauza",
                               "khewat_no")),
            ))
            if record is None:
                record = NaqshaRecord(**{
                    field: value for field, value in values.items()
                    if field != "source_note"
                })
                record.source_note = values["source_note"]
                session.add(record)
                session.flush()
                saved_records += 1
            elif not record.province and values["province"]:
                record.province = values["province"]
            elif values["khatoni_no"] and not record.khatoni_no:
                record.khatoni_no = values["khatoni_no"]
            existing_land = {
                (row.square_no, row.killa_no, row.land_type, row.area_text)
                for row in session.scalars(
                    select(NaqshaLandRow).where(
                        NaqshaLandRow.record_id == record.id)
                ).all()
            }
            for row in group["land_rows"]:
                key = (row["square_no"], row["killa_no"], row["land_type"],
                       row["area_text"])
                if key in existing_land:
                    skipped += 1
                    continue
                session.add(NaqshaLandRow(
                    record_id=record.id,
                    khewat_no=row["khewat_no"],
                    khatoni_no=row["khatoni_no"],
                    square_no=row["square_no"],
                    killa_no=row["killa_no"],
                    land_type=row["land_type"],
                    area_text=row["area_text"],
                    area_marla=row["area_marla"],
                ))
                existing_land.add(key)
                land_added += 1
            existing_owner = {
                (row.owner_name, row.relation, row.parent_name, row.caste,
                 row.share_text, row.area_text)
                for row in session.scalars(
                    select(NaqshaOwnerRow).where(
                        NaqshaOwnerRow.record_id == record.id)
                ).all()
            }
            for row in group["owner_rows"]:
                if row.get("problem"):
                    raise NaqshaError(
                        f"Khewat {khewat_no}: {row['problem']} Fix the row "
                        "before saving."
                    )
                key = (row["owner_name"], row["relation"], row["parent_name"],
                       row["caste"], row["share_text"], row["area_text"])
                if key in existing_owner:
                    skipped += 1
                    continue
                session.add(NaqshaOwnerRow(
                    record_id=record.id,
                    owner_name=row["owner_name"],
                    relation=row["relation"],
                    parent_name=row["parent_name"],
                    caste=row["caste"],
                    share_text=row["share_text"],
                    area_text=row["area_text"],
                    area_marla=row["area_marla"],
                    share_numerator=row.get("share_numerator"),
                    share_denominator=row.get("share_denominator"),
                    share_text_corrected=row.get("share_text_corrected", ""),
                ))
                existing_owner.add(key)
                owner_added += 1
    return {
        "records": saved_records,
        "land_rows_added": land_added,
        "owner_rows_added": owner_added,
        "skipped": skipped,
    }


def _record_dict(record: NaqshaRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "province": record.province,
        "district": record.district,
        "tehsil": record.tehsil,
        "qanoongoi": record.qanoongoi,
        "mauza": record.mauza,
        "khewat_no": record.khewat_no,
        "khatoni_no": record.khatoni_no,
        "source_note": record.source_note,
        "land_rows": [
            {
                "id": row.id,
                "khewat_no": row.khewat_no,
                "khatoni_no": row.khatoni_no,
                "square_no": row.square_no,
                "killa_no": row.killa_no,
                "land_type": row.land_type,
                "area_text": row.area_text,
                "area_marla": row.area_marla,
            }
            for row in record.land_rows
        ],
        "owner_rows": [
            {
                "id": row.id,
                "owner_name": row.owner_name,
                "relation": row.relation,
                "parent_name": row.parent_name,
                "owner_name_raw": " ".join(
                    part for part in (
                        row.owner_name,
                        row.relation and row.relation,
                        row.parent_name,
                    ) if part
                ),
                "caste": row.caste,
                "share_text": row.share_text,
                "area_text": row.area_text,
                "area_marla": row.area_marla,
                "share_text_corrected": row.share_text_corrected,
                "share_numerator": row.share_numerator,
                "share_denominator": row.share_denominator,
            }
            for row in record.owner_rows
        ],
    }


def list_naqsha_records(
    *, khewat_no: str = "", mauza: str = "", district: str = "",
    tehsil: str = "", qanoongoi: str = "", limit: int = 200,
) -> list[dict[str, Any]]:
    """Search saved Naqsha khewats; empty scopes return everything."""
    statement = (
        select(NaqshaRecord)
        .options(joinedload(NaqshaRecord.land_rows),
                 joinedload(NaqshaRecord.owner_rows))
        .order_by(NaqshaRecord.mauza, NaqshaRecord.khewat_no)
        .limit(max(1, limit))
    )
    for field, value in (("khewat_no", khewat_no), ("mauza", mauza),
                         ("district", district), ("tehsil", tehsil),
                         ("qanoongoi", qanoongoi)):
        if str(value or "").strip():
            statement = statement.where(
                getattr(NaqshaRecord, field).ilike(
                    like_pattern(str(value)), escape="\\",
                )
            )
    with SessionLocal() as session:
        return [
            _record_dict(record)
            for record in session.scalars(statement).unique().all()
        ]


def get_naqsha_record(record_id: int) -> dict[str, Any] | None:
    with SessionLocal() as session:
        record = session.scalar(
            select(NaqshaRecord)
            .options(joinedload(NaqshaRecord.land_rows),
                     joinedload(NaqshaRecord.owner_rows))
            .where(NaqshaRecord.id == record_id)
        )
        return _record_dict(record) if record else None


def delete_naqsha_record(record_id: int) -> bool:
    """Delete one saved Naqsha record with its rows. True when deleted."""
    with SessionLocal.begin() as session:
        record = session.get(NaqshaRecord, record_id)
        if record is None:
            return False
        session.delete(record)
    return True


def summarize_naqsha_record(record: dict[str, Any]) -> dict[str, Any]:
    """Totals/checkage for a saved record (recomputed, never stored stale)."""
    land_rows = [
        {**row, "row_number": row["id"], "problem": "",
         "khewat_no": record["khewat_no"]}
        for row in record["land_rows"]
    ]
    owner_rows = [
        {**row, "row_number": row["id"], "problem": "",
         "khewat_no": record["khewat_no"]}
        for row in record["owner_rows"]
    ]
    return summarize_khewat_group(land_rows, owner_rows)


def naqsha_land_rows_for_valuation(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the land data for the Valuation & Taxes page.

    Owner details are filtered out. Rural rows need Mauza + Square/Killa;
    urban rows need Revenue Circle + Property Area and either Khasra No.
    or Square/Killa (mutually exclusive) — the naqsha dataset only
    carries Mauza/Square/Killa, so callers complete the urban fields.
    """
    return [
        {
            "mauza": record["mauza"],
            "district": record["district"],
            "tehsil": record["tehsil"],
            "qanoongoi": record["qanoongoi"],
            "square_no": row["square_no"],
            "killa_no": row["killa_no"],
            "land_type": row["land_type"],
            "area_text": row["area_text"],
        }
        for row in record["land_rows"]
    ]


def list_naqsha_context_suggestions() -> dict[str, list[str]]:
    """Distinct saved hierarchy values for the save form's type-ahead."""
    with SessionLocal() as session:
        rows = session.execute(
            select(
                NaqshaRecord.district, NaqshaRecord.tehsil,
                NaqshaRecord.qanoongoi, NaqshaRecord.mauza,
            ).distinct()
        ).all()
    merged: dict[str, list[str]] = {field: [] for field in LOCATION_FIELDS}
    for district, tehsil, qanoongoi, mauza in rows:
        for field, value in (
            ("district", district), ("tehsil", tehsil),
            ("qanoongoi", qanoongoi), ("mauza", mauza),
        ):
            if value and value not in merged[field]:
                merged[field].append(value)
    for field, values in merged.items():
        merged[field] = sorted(values)[:100]
    return merged
