"""The FBR Clients page dialogs build against the installed Flet API."""

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import flet as ft

from ui.pages import clients


class FakePage:
    def __init__(self):
        self.services = []
        self.dialogs = []

    def update(self):
        return None

    def show_dialog(self, dialog):
        self.dialogs.append(dialog)

    def pop_dialog(self):
        if self.dialogs:
            self.dialogs.pop()

    def run_task(self, handler, *args):
        asyncio.run(handler(*args))
        return None


def _walk(control):
    yield control
    for attribute in ("controls", "actions"):
        for child in getattr(control, attribute, ()) or ():
            if isinstance(child, ft.Control):
                yield from _walk(child)
    content = getattr(control, "content", None)
    if isinstance(content, ft.Control):
        yield from _walk(content)


def _fake_client():
    """Synthetic list-row data; no live client or password is used."""
    return SimpleNamespace(
        id=7,
        name="Test Client",
        father_name="Test Father",
        cnic="12345-6789012-3",
        ntn_number="NTN-1",
        cnic_expiry_date=None,
        date_of_birth=None,
        registration_date=None,
        status_updated_date=None,
        phone_number="0300-1234567",
        email="client@example.com",
        address="House 1",
        tehsil="Model Town",
        district="Lahore",
        city="Lahore",
        province="Punjab",
        payment_amount=Decimal("1000.00"),
        amount_received=Decimal("250.00"),
        current_status="Active",
        referred_by="Referrer",
        password_ciphertext=b"encrypted-only",
    )


def _copy_buttons(root):
    return {
        control.data.removeprefix("copy_client_field:"): control
        for control in _walk(root)
        if isinstance(control, ft.OutlinedButton)
        and str(control.data).startswith("copy_client_field:")
    }


def _cnic_checkbox(root):
    return next(
        (
            control for control in _walk(root)
            if isinstance(control, ft.Checkbox)
            and control.data == "cnic_without_dashes"
        ),
        None,
    )


def _patch_client_listing(monkeypatch):
    monkeypatch.setattr(clients, "list_clients", lambda **kwargs: [_fake_client()])
    monkeypatch.setattr(clients, "get_referrer_payment_totals", lambda: [])


class FakeClipboard:
    def __init__(self):
        self.copied = []

    async def set(self, value):
        self.copied.append(value)


def test_client_card_offers_atl_psid_manager_and_add_form(monkeypatch):
    """v0.2.32: the new card action opens the PSID manager and add dialog."""
    _patch_client_listing(monkeypatch)
    monkeypatch.setattr(clients, "list_psids", lambda client_id: [])
    page = FakePage()
    root = clients.build_clients_page(page)
    card_buttons = {
        getattr(control, "content", ""): control
        for control in _walk(root)
        if isinstance(control, ft.TextButton)
    }
    assert "ATL PSIDs" in card_buttons

    card_buttons["ATL PSIDs"].on_click(None)
    assert page.dialogs, "The ATL PSID manager dialog did not open."
    manager_buttons = {
        getattr(control, "content", ""): control
        for control in _walk(page.dialogs[-1])
        if isinstance(control, (ft.Button, ft.TextButton, ft.OutlinedButton))
    }
    assert "Add PSID" in manager_buttons

    manager_buttons["Add PSID"].on_click(None)
    # The manager is closed before the form opens, so the form replaces it.
    assert len(page.dialogs) == 1, "The add form did not replace the manager."
    title = getattr(page.dialogs[-1], "title", None)
    assert isinstance(title, ft.Text) and title.value == "Add ATL surcharge PSID"
    storage_dropdown = next(
        control for control in _walk(page.dialogs[-1])
        if isinstance(control, ft.Dropdown)
        and control.label == "Challan PDF storage"
    )
    assert {option.key for option in storage_dropdown.options} == {
        "database", "managed_file", "linked_path",
    }
    assert "Store inside database" in {
        option.text for option in storage_dropdown.options
    }


def test_psid_without_pdf_can_be_edited_without_choosing_a_pdf(monkeypatch):
    """A PDF-less record must not be trapped by the storage-mode guard."""
    _patch_client_listing(monkeypatch)
    record = SimpleNamespace(
        id=9,
        storage_mode="none",
        financial_year="2025-26",
        psid_number="PSID-NOPDF",
        amount=None,
        payment_date=None,
        expiry_date=None,
        notes="before",
        original_filename=None,
        expiry_processed_at=None,
    )
    monkeypatch.setattr(clients, "list_psids", lambda client_id: [record])
    monkeypatch.setattr(clients, "get_psid", lambda psid_id: record)
    monkeypatch.setattr(clients, "notify", lambda page, message: None)
    calls = []
    monkeypatch.setattr(
        clients, "save_psid", lambda **kwargs: calls.append(kwargs) or 9
    )

    page = FakePage()
    root = clients.build_clients_page(page)
    card_buttons = {
        getattr(control, "content", ""): control
        for control in _walk(root)
        if isinstance(control, ft.TextButton)
    }
    card_buttons["ATL PSIDs"].on_click(None)
    manager_buttons = {
        getattr(control, "content", ""): control
        for control in _walk(page.dialogs[-1])
        if isinstance(control, (ft.Button, ft.TextButton, ft.OutlinedButton))
    }
    manager_buttons["Edit"].on_click(None)

    notes = next(
        control for control in _walk(page.dialogs[-1])
        if isinstance(control, ft.TextField) and control.label == "Notes"
    )
    notes.value = "after"
    form_buttons = {
        getattr(control, "content", ""): control
        for control in _walk(page.dialogs[-1])
        if isinstance(control, (ft.Button, ft.TextButton, ft.OutlinedButton))
    }
    form_buttons["Save PSID"].on_click(None)

    assert len(calls) == 1
    assert calls[0]["psid_id"] == 9
    assert calls[0]["notes"] == "after"
    assert calls[0]["clear_pdf"] is False


def test_client_dialogs_open_without_name_errors(monkeypatch):
    """Opening the import and column-swap dialogs must not raise.

    The import dialog's nested helpers annotate with typing.Any; a missing
    import only surfaces at dialog-open time (regression 2026-11-21).
    """
    monkeypatch.setattr(clients, "list_clients", lambda **kwargs: [])
    monkeypatch.setattr(clients, "get_referrer_payment_totals", lambda: [])
    page = FakePage()
    root = clients.build_clients_page(page)
    buttons = {
        getattr(control, "content", ""): control
        for control in _walk(root)
        if isinstance(control, ft.OutlinedButton)
    }
    assert "Import clients" in buttons
    assert "Export clients" in buttons
    assert "Swap saved columns" in buttons
    buttons["Import clients"].on_click(None)
    assert page.dialogs, "The import dialog did not open."
    dialog_texts = []
    for control in _walk(page.dialogs[-1]):
        content = getattr(control, "content", None)
        if isinstance(content, str):
            dialog_texts.append(content)  # button labels are plain strings
        value = getattr(control, "value", None)
        if isinstance(value, str):
            dialog_texts.append(value)
    assert "Export problem rows (Excel)" in dialog_texts, (
        "The problem-row export button was not found in the import dialog."
    )
    page.pop_dialog()
    buttons["Swap saved columns"].on_click(None)
    assert page.dialogs, "The column-swap dialog did not open."


def test_default_client_card_has_email_and_phone_copy_buttons(monkeypatch):
    """v0.2.31: previous cards offered only CNIC, address, and password."""
    _patch_client_listing(monkeypatch)
    monkeypatch.setattr(
        clients,
        "get_client_copy_fields",
        lambda: ["cnic", "phone_number", "email", "address", "password"],
    )

    root = clients.build_clients_page(FakePage())
    buttons = _copy_buttons(root)

    assert list(buttons) == [
        "cnic", "phone_number", "email", "address", "password",
    ]
    assert buttons["cnic"].content == "Copy CNIC"
    assert buttons["phone_number"].content == "Copy phone number"
    assert buttons["email"].content == "Copy email"
    assert buttons["address"].content == "Copy address"
    assert buttons["password"].content == "Copy password"
    assert all(not button.disabled for button in buttons.values())


def test_copy_buttons_follow_saved_selection(monkeypatch):
    _patch_client_listing(monkeypatch)
    monkeypatch.setattr(
        clients,
        "get_client_copy_fields",
        lambda: ["name", "father_name", "city"],
    )

    root = clients.build_clients_page(FakePage())

    assert list(_copy_buttons(root)) == ["name", "father_name", "city"]


def test_cnic_copy_checkbox_is_temporary_and_per_card(monkeypatch):
    """v0.2.33: checked copies digits only; rebuilding resets to dashes."""
    _patch_client_listing(monkeypatch)
    monkeypatch.setattr(clients, "get_client_copy_fields", lambda: ["cnic"])
    monkeypatch.setattr(ft, "Clipboard", FakeClipboard)
    page = FakePage()
    clipboard = FakeClipboard()
    page.services.append(clipboard)
    root = clients.build_clients_page(page)

    checkbox = _cnic_checkbox(root)
    assert checkbox is not None
    assert checkbox.label == "CNIC without dashes"
    assert checkbox.value is False

    button = _copy_buttons(root)["cnic"]
    asyncio.run(button.on_click(None))
    assert clipboard.copied == ["12345-6789012-3"]

    checkbox.value = True
    asyncio.run(button.on_click(None))
    assert clipboard.copied == [
        "12345-6789012-3", "1234567890123",
    ]

    checkbox.value = False
    asyncio.run(button.on_click(None))
    assert clipboard.copied == [
        "12345-6789012-3", "1234567890123", "12345-6789012-3",
    ]
    assert _cnic_checkbox(clients.build_clients_page(FakePage())).value is False


def test_cnic_checkbox_is_absent_when_cnic_copy_is_not_configured(monkeypatch):
    _patch_client_listing(monkeypatch)
    monkeypatch.setattr(clients, "get_client_copy_fields", lambda: ["email"])

    root = clients.build_clients_page(FakePage())

    assert list(_copy_buttons(root)) == ["email"]
    assert _cnic_checkbox(root) is None


def test_cnic_form_explains_dashes_are_optional(monkeypatch):
    _patch_client_listing(monkeypatch)
    page = FakePage()
    root = clients.build_clients_page(page)
    add_button = next(
        control for control in _walk(root)
        if isinstance(control, (ft.Button, ft.TextButton))
        and getattr(control, "content", "") == "Add client"
    )

    add_button.on_click(None)
    cnic_field = next(
        control for control in _walk(page.dialogs[-1])
        if isinstance(control, ft.TextField)
        and control.label == "CNIC number (optional)"
    )
    assert cnic_field.hint_text == "00000-0000000-0"
    assert "with or without dashes" in cnic_field.helper


def test_email_and_phone_copy_handlers_are_awaited(monkeypatch):
    """A factory-made async handler, not a coroutine-returning lambda."""
    _patch_client_listing(monkeypatch)
    monkeypatch.setattr(
        clients,
        "get_client_copy_fields",
        lambda: ["email", "phone_number"],
    )
    monkeypatch.setattr(ft, "Clipboard", FakeClipboard)
    page = FakePage()
    clipboard = FakeClipboard()
    page.services.append(clipboard)
    root = clients.build_clients_page(page)
    buttons = _copy_buttons(root)

    asyncio.run(buttons["email"].on_click(None))
    asyncio.run(buttons["phone_number"].on_click(None))

    assert clipboard.copied == ["client@example.com", "0300-1234567"]


def test_password_copy_still_decrypts_only_on_click(monkeypatch):
    _patch_client_listing(monkeypatch)
    monkeypatch.setattr(clients, "get_client_copy_fields", lambda: ["password"])
    monkeypatch.setattr(ft, "Clipboard", FakeClipboard)
    calls = []
    monkeypatch.setattr(
        clients,
        "decrypted_password_for_copy",
        lambda client_id: calls.append(client_id) or "portal-secret",
    )
    page = FakePage()
    clipboard = FakeClipboard()
    page.services.append(clipboard)
    root = clients.build_clients_page(page)

    asyncio.run(_copy_buttons(root)["password"].on_click(None))

    assert calls == [7]
    assert clipboard.copied == ["portal-secret"]
    assert "portal-secret" not in str(_fake_client())
