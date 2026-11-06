"""Persistent key/value application settings.

Preferences are stored in the additive ``app_settings`` table as JSON text so
new page/tab options can be added without another schema change. Reads are
defensive: a missing table (for example in a very old database) or malformed
value falls back to the caller's default instead of breaking a page.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from core.database import SessionLocal
from core.models import AppSetting


CADASTRAL_STORAGE_KEY = "dc_rates.cadastral_storage"
CADASTRAL_STORAGE_MODES = ("ranges", "individual")
DEFAULT_CADASTRAL_STORAGE = "ranges"


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
