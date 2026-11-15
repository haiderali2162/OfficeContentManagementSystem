"""Saved schedule lookups by khewat number (Calculate tab, developer request).

A searched schedule — its location fields plus the selected DC and FBR rate
rows — can be stored under a khewat number and recalled later without
re-typing anything. Several schedule rows may live under one khewat number
(one per square/kila), and urban entries also store the found FBR schedule.

Storage uses the existing JSON app-setting store, so no schema change is
needed and the data stays local to the office database.

Developer-request batch (2026-11-15):

* Each row carries a stable fingerprint of its schedule identity
  (location + square/kila/locality context), so saving the same schedule
  again never adds a duplicate row; re-saving with a different rate pick
  refreshes the stored row in place.
* Rows can be edited or deleted (``update_schedule_lookup`` /
  ``delete_schedule_lookup``), and the whole store can be summarised
  mauza-wise (``summarize_schedule_lookups``).
* Rural rows with a square/Kila are mirrored into the matching khewat
  record of the DC / FBR rates tab (``sync_saved_lookup_to_registry``),
  and deleting a saved row removes that mirror again — both tabs keep
  showing the same khewat membership.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from services import dc_rate_service
from services.settings_service import get_setting, set_setting

_STORE_KEY = "calculate.saved_schedule_lookups"

# Fields that identify a schedule row inside a khewat: the location context
# plus the cadastral reference (rural square/kila, urban locality). The
# selected DC/FBR rate ids are deliberately NOT part of the identity: the
# schedule row stays the same when a different rate row is picked, so
# re-saving refreshes the stored selection instead of adding a duplicate.
_IDENTITY_FIELDS = (
    "land_type", "district", "tehsil", "qanoongoi", "mauza",
    "square_no", "kila_no", "locality", "committee_town",
    "revenue_circle", "property_area", "classification",
)

# Fields kept out when comparing a stored row against a new save (metadata).
_RECORD_META_FIELDS = ("khewat_no", "saved_at", "row_fingerprint")


class ScheduleLookupError(ValueError):
    """Raised when a saved-schedule lookup cannot be stored or read."""


def _normalize_khewat(khewat_no: Any) -> str:
    text = str(khewat_no or "").strip()
    if not text:
        raise ScheduleLookupError("Enter a khewat number before saving or loading.")
    return text


def _load_store() -> list[dict[str, Any]]:
    stored = get_setting(_STORE_KEY, None)
    if isinstance(stored, list):
        return [entry for entry in stored if isinstance(entry, dict)]
    return []


def row_fingerprint(entry: dict[str, Any]) -> str:
    """Return the stable identity fingerprint of one schedule row.

    The fingerprint hashes the casefolded schedule-identity fields, so it is
    reproducible for stored rows of any older version (no migration needed)
    and equal rows can never be stored twice under one khewat number.
    """
    values = []
    for field in _IDENTITY_FIELDS:
        value = entry.get(field)
        values.append("" if value is None else str(value).strip().casefold())
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _stored_fingerprint(record: dict[str, Any]) -> str:
    """Fingerprint of a stored record (fast path; recomputed for legacy rows)."""
    fingerprint = record.get("row_fingerprint")
    if isinstance(fingerprint, str) and fingerprint:
        return fingerprint
    return row_fingerprint(record)


def save_schedule_lookup(khewat_no: Any, entry: dict[str, Any]) -> dict[str, Any]:
    """Store one schedule entry under the khewat number — never duplicated.

    The schedule-identity fingerprint decides the outcome:

    * a new schedule row is appended (status ``saved``);
    * an identical row that is already stored is left untouched
      (status ``duplicate``);
    * a row whose schedule identity exists but whose stored rate selection
      differs is refreshed in place (status ``updated``).

    Returns ``{"status", "khewat_no", "rows", "record"}`` where ``rows`` is
    the number of schedule rows now stored under that khewat number.
    """
    number = _normalize_khewat(khewat_no)
    if not isinstance(entry, dict) or not entry:
        raise ScheduleLookupError("Nothing to save: the schedule details are empty.")
    store = _load_store()
    fingerprint = row_fingerprint(entry)
    existing_index: int | None = None
    for index, record in enumerate(store):
        if (
            str(record.get("khewat_no") or "").strip().casefold()
            == number.casefold()
            and _stored_fingerprint(record) == fingerprint
        ):
            existing_index = index
            break
    if existing_index is None:
        record = {
            "khewat_no": number,
            "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "row_fingerprint": fingerprint,
            **{key: value for key, value in entry.items() if key != "khewat_no"},
        }
        store.append(record)
        status = "saved"
    else:
        record = store[existing_index]
        new_payload = {
            key: value for key, value in entry.items() if key != "khewat_no"
        }
        old_payload = {
            key: value for key, value in record.items()
            if key not in _RECORD_META_FIELDS
        }
        if old_payload == new_payload:
            status = "duplicate"
        else:
            # Same schedule row, different stored selection: refresh the
            # row in place (keeps the original save position and time).
            record.update(new_payload)
            record["row_fingerprint"] = fingerprint
            status = "updated"
    set_setting(_STORE_KEY, store)
    rows = sum(
        1 for item in store
        if str(item.get("khewat_no") or "").strip().casefold() == number.casefold()
    )
    return {
        "status": status,
        "khewat_no": number,
        "rows": rows,
        "record": record,
    }


def update_schedule_lookup(
    khewat_no: Any,
    fingerprint: str,
    entry: dict[str, Any],
) -> dict[str, Any]:
    """Replace one saved row (matched by its fingerprint) with an edited one.

    The khewat number may be edited too: pass the new number inside
    ``entry["khewat_no"]`` to move the row to another khewat group. Raises
    :class:`ScheduleLookupError` when the row is not found or when the
    edited details would duplicate another row of the target khewat.
    """
    number = _normalize_khewat(khewat_no)
    if not isinstance(entry, dict) or not entry:
        raise ScheduleLookupError("Nothing to save: the schedule details are empty.")
    store = _load_store()
    match_indexes = [
        index for index, record in enumerate(store)
        if str(record.get("khewat_no") or "").strip().casefold() == number.casefold()
        and _stored_fingerprint(record) == str(fingerprint or "")
    ]
    if not match_indexes:
        raise ScheduleLookupError(
            "The saved row to edit was not found — refresh the khewat view."
        )
    target_number = _normalize_khewat(entry.get("khewat_no") or number)
    new_fingerprint = row_fingerprint(entry)
    for index, record in enumerate(store):
        if index in match_indexes:
            continue
        if (
            str(record.get("khewat_no") or "").strip().casefold()
            == target_number.casefold()
            and _stored_fingerprint(record) == new_fingerprint
        ):
            raise ScheduleLookupError(
                "Another row in this khewat already holds these schedule details."
            )
    record = store[match_indexes[0]]
    payload = {
        key: value for key, value in entry.items()
        if key not in _RECORD_META_FIELDS
    }
    record.clear()
    record.update({
        "khewat_no": target_number,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "row_fingerprint": new_fingerprint,
        **payload,
    })
    set_setting(_STORE_KEY, store)
    return record


def delete_schedule_lookup(khewat_no: Any, fingerprint: str) -> bool:
    """Remove the saved row matching the fingerprint. True when removed."""
    number = _normalize_khewat(khewat_no)
    store = _load_store()
    keep = [
        record for record in store
        if not (
            str(record.get("khewat_no") or "").strip().casefold() == number.casefold()
            and _stored_fingerprint(record) == str(fingerprint or "")
        )
    ]
    if len(keep) == len(store):
        return False
    set_setting(_STORE_KEY, keep)
    return True


def list_all_schedule_lookups() -> list[dict[str, Any]]:
    """Return every saved schedule row, in storage order."""
    return _load_store()


def list_schedule_lookups(
    khewat_no: Any,
    *,
    mauza: str = "",
    district: str = "",
    tehsil: str = "",
    qanoongoi: str = "",
) -> list[dict[str, Any]]:
    """Return schedule entries stored under the khewat number, in order.

    The optional location arguments scope the search: each khewat belongs to
    a mauza (of a tehsil & district, qanoongoi optional), so the same khewat
    number in two mauzas stays distinguishable (developer request).
    """
    number = _normalize_khewat(khewat_no).casefold()
    scopes = {
        "mauza": str(mauza or "").strip().casefold(),
        "district": str(district or "").strip().casefold(),
        "tehsil": str(tehsil or "").strip().casefold(),
        "qanoongoi": str(qanoongoi or "").strip().casefold(),
    }
    matches: list[dict[str, Any]] = []
    for entry in _load_store():
        if str(entry.get("khewat_no") or "").strip().casefold() != number:
            continue
        if all(
            not text or text in str(entry.get(field) or "").casefold()
            for field, text in scopes.items()
        ):
            matches.append(entry)
    return matches


def row_reference_label(entry: dict[str, Any]) -> str:
    """Human-readable schedule reference of one saved row.

    Rural rows show their Square/Kila; rows without them (urban schedules)
    fall back to the locality, then the committee town.
    """
    parts = []
    if entry.get("square_no"):
        parts.append(f"Square {entry['square_no']}")
    if entry.get("kila_no"):
        parts.append(f"Kila {entry['kila_no']}")
    if not parts:
        for field in ("locality", "committee_town"):
            if entry.get(field):
                parts.append(str(entry[field]))
                break
    return " · ".join(str(part) for part in parts) or "Schedule row"


def _natural_key(text: Any) -> tuple:
    """Sort key that orders embedded numbers numerically (2 < 10 < 22-123)."""
    segments = re.split(r"(\d+)", str(text))
    return tuple(
        int(segment) if segment.isdigit() else segment.casefold()
        for segment in segments
        if segment != ""
    )


def summarize_schedule_lookups() -> list[dict[str, Any]]:
    """Summarise the saved schedule rows mauza-wise and khewat-wise.

    One entry per mauza + khewat number: how many rows are saved and the
    lowest/highest schedule reference stored in the khewat (developer
    request for the view-all dialog). The location context of the rows
    (district/tehsil/qanoongoi, first row's values) is included so the
    dialog's search can scope by it.
    """
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for record in _load_store():
        mauza_display = str(record.get("mauza") or "").strip()
        khewat_display = str(record.get("khewat_no") or "").strip()
        key = (mauza_display.casefold(), khewat_display.casefold())
        group = groups.setdefault(key, {
            "mauza": mauza_display or "(mauza not set)",
            "khewat_no": khewat_display,
            "district": "",
            "tehsil": "",
            "qanoongoi": "",
            "labels": [],
        })
        for field in ("district", "tehsil", "qanoongoi"):
            if not group[field] and record.get(field):
                group[field] = str(record[field]).strip()
        group["labels"].append(row_reference_label(record))
    summaries = []
    for group in groups.values():
        labels = sorted(set(group["labels"]), key=_natural_key)
        summaries.append({
            "mauza": group["mauza"],
            "khewat_no": group["khewat_no"],
            "district": group["district"],
            "tehsil": group["tehsil"],
            "qanoongoi": group["qanoongoi"],
            "rows": len(group["labels"]),
            "lowest": labels[0],
            "highest": labels[-1],
        })
    summaries.sort(key=lambda item: (
        _natural_key(item["mauza"]), _natural_key(item["khewat_no"]),
    ))
    return summaries


def search_saved_lookups_summary(
    *, khewat_no: str = "", mauza: str = "", district: str = "",
    tehsil: str = "", qanoongoi: str = "",
) -> list[dict[str, Any]]:
    """Search the mauza-wise summaries for the view-all dialog.

    The khewat number may be typed partially and matches across mauzas;
    mauza/district/tehsil/qanoongoi narrow it (all optional). An empty
    scope matches everything, so calling it without arguments returns the
    complete list.
    """
    scopes = {
        "khewat_no": str(khewat_no or "").strip().casefold(),
        "mauza": str(mauza or "").strip().casefold(),
        "district": str(district or "").strip().casefold(),
        "tehsil": str(tehsil or "").strip().casefold(),
        "qanoongoi": str(qanoongoi or "").strip().casefold(),
    }
    matches = []
    for summary in summarize_schedule_lookups():
        if all(
            not text or text in str(summary.get(field) or "").casefold()
            for field, text in scopes.items()
        ):
            matches.append(summary)
    return matches


# --------------------------------------------------------------------------
# Mirror of saved rows into the khewat registry of the DC / FBR rates tab
# (developer request: both tabs must show the same khewat membership).
# --------------------------------------------------------------------------


def _registry_matches_for_entry(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Khewat registry records matching a saved row's mauza + khewat number."""
    mauza = str(entry.get("mauza") or "").strip()
    number = str(entry.get("khewat_no") or "").strip()
    if mauza:
        records = dc_rate_service.search_khewats(mauza=mauza)
    else:
        records = dc_rate_service.search_khewats(khewat_number=number)
    return [
        record for record in records
        if str(record.get("khewat_no") or record.get("khewat_number") or "").strip().casefold()
        == number.casefold()
    ]


def _entry_references(entry: dict[str, Any]) -> tuple[list[str], list[str]]:
    squares = [str(entry.get("square_no") or "").strip()]
    kilas = [str(entry.get("kila_no") or "").strip()]
    return [v for v in squares if v], [v for v in kilas if v]


def sync_saved_lookup_to_registry(khewat_no: Any, entry: dict[str, Any]) -> str:
    """Best-effort mirror: add the row's Square/Kila to the rates-tab khewat.

    Returns a short note for the status line: what was mirrored, or why the
    mirror was skipped (no khewat record, several RHZ-year records, or the
    registry rejected the reference). Failures never block the save.
    """
    number = _normalize_khewat(khewat_no)
    scoped = dict(entry)
    scoped.setdefault("khewat_no", number)
    squares, kilas = _entry_references(scoped)
    if not squares and not kilas:
        return (
            "This row has no Square/Kila reference, so nothing was mirrored "
            "to the rates-tab khewat."
        )
    matches = _registry_matches_for_entry(scoped)
    if not matches:
        return (
            "No khewat record with this number exists on the DC / FBR rates "
            "tab yet, so the Square/Kila was not mirrored."
        )
    if len(matches) > 1:
        return (
            "This khewat number matches several RHZ-year records on the "
            "rates tab; the Square/Kila was not mirrored automatically."
        )
    record = matches[0]
    try:
        dc_rate_service.update_khewat_record(
            khewat_id=int(record["id"]),
            khewat_number=str(record["khewat_number"]),
            rhz_year=str(record.get("rhz_year") or ""),
            khewat_size=record.get("khewat_size"),
            square_numbers=list(dict.fromkeys(
                list(record.get("squares") or []) + squares
            )),
            kila_numbers=list(dict.fromkeys(
                list(record.get("kilas") or []) + kilas
            )),
        )
    except dc_rate_service.DCRateServiceError as exc:
        return f"The rates-tab khewat was not updated: {exc}"
    return "Square/Kila mirrored to the matching rates-tab khewat."


def unsync_deleted_lookup_from_registry(
    khewat_no: Any,
    entry: dict[str, Any],
) -> str:
    """Best-effort mirror of a row deletion: drop its Square/Kila reference.

    Only references that no other saved row of the same khewat (scoped by
    mauza) still uses are removed, so shared references survive. Returns a
    status note; failures never raise.
    """
    number = _normalize_khewat(khewat_no)
    scoped = dict(entry)
    scoped.setdefault("khewat_no", number)
    squares, kilas = _entry_references(scoped)
    if not squares and not kilas:
        return ""
    matches = _registry_matches_for_entry(scoped)
    if not matches:
        return ""
    if len(matches) > 1:
        return (
            "The khewat number matches several RHZ-year records on the rates "
            "tab; the Square/Kila reference was not removed there."
        )
    # Keep references still used by other saved rows of the same khewat.
    try:
        remaining = list_schedule_lookups(
            number, mauza=str(scoped.get("mauza") or ""),
        )
    except ScheduleLookupError:
        remaining = []
    keep_squares = {
        str(row.get("square_no") or "").strip()
        for row in remaining if row.get("square_no")
    }
    keep_kilas = {
        str(row.get("kila_no") or "").strip()
        for row in remaining if row.get("kila_no")
    }
    remove_squares = [value for value in squares if value not in keep_squares]
    remove_kilas = [value for value in kilas if value not in keep_kilas]
    if not remove_squares and not remove_kilas:
        return ""
    record = matches[0]
    try:
        removed = dc_rate_service.remove_khewat_references(
            khewat_id=int(record["id"]),
            square_numbers=remove_squares,
            kila_numbers=remove_kilas,
        )
    except dc_rate_service.DCRateServiceError as exc:
        return f"The rates-tab khewat was not updated: {exc}"
    if not removed:
        return ""
    return (
        f"Square/Kila reference removed from the matching rates-tab khewat "
        f"({removed} reference(s))."
    )
