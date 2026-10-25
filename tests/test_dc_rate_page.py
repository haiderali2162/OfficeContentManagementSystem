"""UI callback regression tests for DC-rate page actions."""

from datetime import date

import flet as ft

from ui.pages import dc_rates


class FakePage:
    def __init__(self):
        self.services = []
        self.dialogs = []

    def update(self):
        pass

    def show_dialog(self, dialog):
        self.dialogs.append(dialog)

    def pop_dialog(self):
        if self.dialogs:
            self.dialogs.pop()


def _walk(control):
    yield control
    for child in getattr(control, "controls", []) or []:
        yield from _walk(child)
    content = getattr(control, "content", None)
    if isinstance(content, ft.Control):
        yield from _walk(content)


def _rate_record():
    return {
        "id": 7,
        "location_id": 3,
        "location_name": "Test Mauza",
        "province": "Punjab",
        "district": "Test District",
        "tehsil": "Test Tehsil",
        "mauza": "Test Mauza",
        "hadbast_number": "123",
        "locality": "Urban",
        "property_type": "Residential",
        "classification": "Urban",
        "rate_value": 1,
        "rate_unit": "per_marla",
        "effective_from": date(2025, 7, 1),
        "effective_to": None,
        "source_reference": "Hypothetical test data",
        "source_date": None,
        "verification_status": "unverified",
        "verified_at": None,
        "verified_by": None,
        "created_at": None,
        "updated_at": None,
    }


def test_add_button_event_is_not_used_as_rate_id(monkeypatch):
    monkeypatch.setattr(dc_rates, "list_locations", lambda: [])
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [])
    monkeypatch.setattr(
        dc_rates,
        "get_dc_rate",
        lambda rate_id: (_ for _ in ()).throw(
            AssertionError(f"Add action must not query rate ID {rate_id!r}")
        ),
    )
    page = FakePage()
    root = dc_rates.build_dc_rates_page(page)

    add_button = root.controls[2].controls[0]
    add_button.on_click(object())

    assert page.dialogs
    dialog = page.dialogs[-1]
    assert dialog.title.value == "Add DC valuation rate"
    dialog_controls = list(_walk(dialog.content))
    source_field = next(
        control
        for control in dialog_controls
        if isinstance(control, ft.TextField)
        and control.label == "Source / schedule reference"
    )
    verifier_field = next(
        control
        for control in dialog_controls
        if isinstance(control, ft.TextField)
        and str(control.label).startswith("Verified by")
    )
    assert source_field.multiline is False
    assert source_field.expand is None
    assert verifier_field.visible is False
    # Expanded direct children inside a dialog's vertical Column can consume
    # the remaining height and render as a large empty gray block in Flet.
    assert all(control.expand is None for control in dialog.content.controls)


def test_rate_page_is_responsive_and_saved_rate_is_shown(monkeypatch):
    saved = [_rate_record()]
    last_filters = {}
    monkeypatch.setattr(dc_rates, "list_locations", lambda: [{"id": 3, "name": "Test Mauza"}])
    def list_rates(**kwargs):
        last_filters.clear()
        last_filters.update(kwargs)
        return saved.copy()
    monkeypatch.setattr(dc_rates, "list_dc_rates", list_rates)
    monkeypatch.setattr(dc_rates, "create_dc_rate", lambda **kwargs: saved.append({
        **_rate_record(),
        "effective_from": date(2027, 1, 1),
    }))
    monkeypatch.setattr(dc_rates, "get_dc_rate", lambda _rate_id: None)
    page = FakePage()
    root = dc_rates.build_dc_rates_page(page)

    assert root.expand is True
    assert root.width is None
    root.controls[2].controls[0].on_click(object())
    dialog = page.dialogs[-1]
    fields = [control for control in _walk(dialog.content) if isinstance(control, (ft.TextField, ft.Dropdown))]
    by_label = {control.label: control for control in fields}
    by_label["Property location"].value = "3"
    by_label["Property type / land classification"].value = "Residential"
    by_label["DC / FBR schedule rate"].value = "12500"
    by_label["Effective from (YYYY-MM-DD)"].value = "2027-01-01"
    dialog.actions[-1].on_click(object())

    assert len(saved) == 2
    assert len(page.dialogs) == 0
    list_view = next(control for control in _walk(root) if isinstance(control, ft.ListView))
    assert len([control for control in list_view.controls if isinstance(control, ft.Container)]) == 2
    assert "2 rate record" in root.controls[5].controls[0].value
    assert last_filters["search"] == ""
    assert last_filters["schedule_type"] is None
    assert last_filters["location_id"] is None
    assert last_filters["rate_unit"] is None
    assert last_filters["verification_status"] is None
    assert last_filters["effective_on"] is None


def test_edit_button_passes_the_rate_id_explicitly(monkeypatch):
    record = _rate_record()
    monkeypatch.setattr(dc_rates, "list_locations", lambda: [{"id": 3, "name": "Test Mauza"}])
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [record])
    monkeypatch.setattr(dc_rates, "get_dc_rate", lambda rate_id: record if rate_id == 7 else None)
    page = FakePage()
    root = dc_rates.build_dc_rates_page(page)

    edit_button = next(
        control
        for control in _walk(root)
        if isinstance(control, ft.TextButton) and control.content == "Edit"
    )
    edit_button.on_click(object())

    assert page.dialogs
    assert page.dialogs[-1].title.value == "Edit DC rate #7"


def test_rate_card_shows_rate_and_location_text(monkeypatch):
    record = _rate_record()
    record["rate_value"] = "12345.00"
    monkeypatch.setattr(dc_rates, "list_locations", lambda: [])
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [record])
    page = FakePage()

    root = dc_rates.build_dc_rates_page(page)
    text_values = [control.value for control in _walk(root) if isinstance(control, ft.Text)]
    assert any("Test Mauza" in value and "DC schedule" in value for value in text_values)
    assert any("Rate: 12345.00" in value for value in text_values)
    assert any("Residential" in value for value in text_values)
