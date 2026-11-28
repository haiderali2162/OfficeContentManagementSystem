"""Formatting helpers for FBR Clients one-click copy buttons (v0.2.33).

v0.2.30 cards hard-coded three copy actions (CNIC, full address, password).
v0.2.31 makes those buttons configurable and adds all built-in client fields.
v0.2.33 adds an optional digits-only CNIC copy format for a card's temporary
checkbox. User-defined custom columns stay on the FBR Clients column manager:
they have no stable identity, so a saved copy preference could not safely
follow them if a definition is renamed or deleted.

Values are derived from the already-loaded list row except for the password.
Encrypted credential bytes are never converted to text here; an encrypted
password is reloaded and decrypted only when the user explicitly clicks its
copy button. No plaintext credential is stored, logged, or included in card
state.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from core.security import decrypt_secret
from services.client_service import get_client
from services.settings_service import CLIENT_COPY_FIELD_LABELS, SettingsError


DATE_COPY_FIELDS = frozenset({
    "cnic_expiry_date",
    "date_of_birth",
    "registration_date",
    "status_updated_date",
})
AMOUNT_COPY_FIELDS = frozenset({"payment_amount", "amount_received"})
ADDRESS_PARTS = ("address", "tehsil", "district", "city", "province")
PASSWORD_FIELD = "password"

#: Buttons use concise wording even where the Settings label is longer.
COPY_BUTTON_LABELS = {
    "name": "Copy name",
    "father_name": "Copy father name",
    "cnic": "Copy CNIC",
    "ntn_number": "Copy NTN",
    "cnic_expiry_date": "Copy CNIC expiry date",
    "date_of_birth": "Copy date of birth",
    "registration_date": "Copy registration date",
    "status_updated_date": "Copy status updated date",
    "phone_number": "Copy phone number",
    "email": "Copy email",
    "address": "Copy address",
    "tehsil": "Copy tehsil",
    "district": "Copy district",
    "city": "Copy city",
    "province": "Copy province",
    "payment_amount": "Copy payment amount",
    "amount_received": "Copy amount received",
    "current_status": "Copy status",
    "referred_by": "Copy referred by",
    "password": "Copy password",
}


def _validate_field(field: str) -> str:
    """Return a supported built-in field key or raise a clear setting error."""
    key = str(field).strip()
    if key not in CLIENT_COPY_FIELD_LABELS:
        supported = ", ".join(sorted(CLIENT_COPY_FIELD_LABELS))
        raise SettingsError(
            f"Unsupported client copy field '{key}'. Use one of: {supported}."
        )
    return key


def format_copy_value(field: str, value: Any) -> str:
    """Format one built-in field value as plain clipboard text.

    Dates use ISO ``YYYY-MM-DD``. Money uses the exact stored decimal value
    without a currency symbol so the result can be pasted into office forms.
    """
    key = _validate_field(field)
    if value is None:
        return ""
    if key == PASSWORD_FIELD:
        # Ciphertext must never be treated as a copyable password value.
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if key in DATE_COPY_FIELDS:
        isoformat = getattr(value, "isoformat", None)
        return str(isoformat()) if callable(isoformat) else text
    if key in AMOUNT_COPY_FIELDS:
        try:
            return format(Decimal(text), "f")
        except (InvalidOperation, ValueError):
            return text
    return text


def client_copy_value(client: Any, field: str) -> str:
    """Return the clipboard text for a non-secret built-in client field."""
    key = _validate_field(field)
    if key == PASSWORD_FIELD:
        return ""
    if key == "address":
        parts = (
            str(getattr(client, part, None) or "").strip()
            for part in ADDRESS_PARTS
        )
        return ", ".join(part for part in parts if part)
    return format_copy_value(key, getattr(client, key, None))


def cnic_copy_value(client: Any, without_dashes: bool = False) -> str:
    """Return CNIC clipboard text, optionally reduced to digits only.

    The stored value keeps its canonical ``00000-0000000-0`` shape. The
    dash-free form is computed at copy time only, so no client data or saved
    copy preference is changed.
    """
    value = client_copy_value(client, "cnic")
    return re.sub(r"\D", "", value) if without_dashes else value


def client_copy_field_available(client: Any, field: str) -> bool:
    """Return whether a card's copy button should be enabled for this client."""
    key = _validate_field(field)
    if key == PASSWORD_FIELD:
        return bool(getattr(client, "password_ciphertext", None))
    return bool(client_copy_value(client, key))


def copy_button_label(field: str) -> str:
    """Return the short label shown on a client card copy button."""
    return COPY_BUTTON_LABELS[_validate_field(field)]


def client_copy_field_label(field: str) -> str:
    """Return the Settings label used in copy success/error messages."""
    return CLIENT_COPY_FIELD_LABELS[_validate_field(field)]


def decrypted_password_for_copy(client_id: int) -> str:
    """Reload and decrypt a password only inside an explicit copy click."""
    saved_client = get_client(client_id)
    if saved_client is None:
        return ""
    return decrypt_secret(saved_client.password_ciphertext) or ""
