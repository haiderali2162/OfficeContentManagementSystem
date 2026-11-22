"""The FBR Clients page dialogs build against the installed Flet API."""

import asyncio

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
    for child in getattr(control, "controls", ()) or ():
        yield from _walk(child)
    content = getattr(control, "content", None)
    if isinstance(content, ft.Control):
        yield from _walk(content)


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
