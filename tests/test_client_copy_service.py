"""FBR Clients one-click copy value tests (v0.2.31)."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from services import client_copy_service
from services.settings_service import SettingsError


def test_format_copy_value_uses_plain_clipboard_text():
    assert (
        client_copy_service.format_copy_value(
            "date_of_birth", date(1990, 1, 2)
        )
        == "1990-01-02"
    )
    assert (
        client_copy_service.format_copy_value(
            "payment_amount", Decimal("12345.60")
        )
        == "12345.60"
    )
    assert (
        client_copy_service.format_copy_value("email", "  audit@example.com ")
        == "audit@example.com"
    )
    assert client_copy_service.format_copy_value("cnic", None) == ""
    # Ciphertext is not a password value and must never reach the clipboard.
    assert client_copy_service.format_copy_value("password", b"cipher") == ""


def test_client_copy_value_composes_full_address_and_keeps_parts_available():
    client = SimpleNamespace(
        address="House 1",
        tehsil="Model Town",
        district="Lahore",
        city="Lahore",
        province="Punjab",
        cnic="12345-6789012-3",
        password_ciphertext=b"cipher",
    )

    assert client_copy_service.client_copy_value(client, "address") == (
        "House 1, Model Town, Lahore, Lahore, Punjab"
    )
    assert client_copy_service.client_copy_value(client, "tehsil") == "Model Town"
    assert client_copy_service.client_copy_value(client, "password") == ""
    assert client_copy_service.copy_button_label("cnic") == "Copy CNIC"
    assert (
        client_copy_service.copy_button_label("phone_number")
        == "Copy phone number"
    )
    assert client_copy_service.client_copy_field_label("email") == "Email"


def test_availability_uses_saved_values_and_ciphertext_presence():
    client = SimpleNamespace(
        cnic=None,
        email="audit@example.com",
        password_ciphertext=b"cipher",
    )
    assert client_copy_service.client_copy_field_available(client, "email")
    assert not client_copy_service.client_copy_field_available(client, "cnic")
    assert client_copy_service.client_copy_field_available(client, "password")

    no_password = SimpleNamespace(password_ciphertext=None)
    assert not client_copy_service.client_copy_field_available(
        no_password, "password"
    )


def test_password_is_reloaded_and_decrypted_only_when_copied(monkeypatch):
    calls = []

    class SavedClient:
        password_ciphertext = b"cipher"

    monkeypatch.setattr(
        client_copy_service,
        "get_client",
        lambda client_id: calls.append(client_id) or SavedClient(),
    )
    monkeypatch.setattr(
        client_copy_service,
        "decrypt_secret",
        lambda ciphertext: "portal-secret",
    )

    assert client_copy_service.decrypted_password_for_copy(42) == "portal-secret"
    assert calls == [42]


def test_missing_client_has_no_password_to_copy(monkeypatch):
    monkeypatch.setattr(client_copy_service, "get_client", lambda _: None)
    assert client_copy_service.decrypted_password_for_copy(99) == ""


def test_unknown_copy_field_is_rejected():
    with pytest.raises(SettingsError, match="Unsupported client copy field"):
        client_copy_service.client_copy_value(
            SimpleNamespace(), "password_ciphertext"
        )
