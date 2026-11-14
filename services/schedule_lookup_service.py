"""Saved schedule lookups by khewat number (Calculate tab, developer request).

A searched schedule — its location fields plus the selected DC and FBR rate
rows — can be stored under a khewat number and recalled later without
re-typing anything. Several schedule rows may live under one khewat number
(one per square/kila), and urban entries also store the found FBR schedule.

Storage uses the existing JSON app-setting store, so no schema change is
needed and the data stays local to the office database.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from services.settings_service import get_setting, set_setting

_STORE_KEY = "calculate.saved_schedule_lookups"


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


def save_schedule_lookup(khewat_no: Any, entry: dict[str, Any]) -> int:
    """Append one schedule entry under the khewat number.

    Returns how many schedule rows are now stored under that khewat number,
    so the caller can confirm the multi-row behaviour.
    """
    number = _normalize_khewat(khewat_no)
    if not isinstance(entry, dict) or not entry:
        raise ScheduleLookupError("Nothing to save: the schedule details are empty.")
    store = _load_store()
    record = {
        "khewat_no": number,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **{key: value for key, value in entry.items() if key != "khewat_no"},
    }
    store.append(record)
    set_setting(_STORE_KEY, store)
    return sum(1 for item in store if item.get("khewat_no") == number)


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
