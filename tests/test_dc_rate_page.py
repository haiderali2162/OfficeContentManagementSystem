"""UI callback regression tests for DC-rate page actions."""

from datetime import date
import asyncio
from types import SimpleNamespace

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

    def run_task(self, handler):
        # Keep the task coroutine for an explicit assertion/run in the test.
        self.background_task = handler()


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
        "classification": "Residential",
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
        and control.label == "Schedule / notification reference (optional)"
    )
    assert source_field.multiline is False
    assert source_field.expand is None
    labels = {getattr(control, "label", None) for control in dialog_controls}
    assert "Published year (optional)" in labels
    assert "Effective to (optional)" not in labels
    assert "Source date (optional)" not in labels
    assert "Verification status" not in labels
    assert not any(str(label).startswith("Verified by") for label in labels if label)
    # Expanded direct children inside a dialog's vertical Column can consume
    # the remaining height and render as a large empty gray block in Flet.
    assert all(control.expand is None for control in dialog.content.controls)


def test_rate_page_is_responsive_and_saved_rate_is_shown(monkeypatch):
    saved = [_rate_record()]
    last_filters = {}
    monkeypatch.setattr(dc_rates, "list_locations", lambda: [{"id": 3, "name": "Test Mauza"}])
    monkeypatch.setattr(dc_rates, "summarize_dc_rates", lambda **_: {
        "count": 2, "lowest": "1", "highest": "2",
        "lowest_unit": "per_marla", "lowest_schedule": "dc",
        "highest_unit": "per_marla", "highest_schedule": "dc",
    })
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
    by_label["Land classification"].value = "Residential"
    by_label["DC / FBR schedule value"].value = "12500"
    by_label["Published year (optional)"].value = "2027"
    dialog.actions[-1].on_click(object())
    asyncio.run(page.background_task)

    assert len(saved) == 2
    assert len(page.dialogs) == 0
    list_view = next(control for control in _walk(root) if isinstance(control, ft.ListView))
    assert len([control for control in list_view.controls if isinstance(control, ft.Container)]) == 2
    assert any("Showing 2 rate" in (control.value or "") for control in _walk(root)
               if isinstance(control, ft.Text))
    assert last_filters["search"] == ""
    assert last_filters["schedule_type"] is None
    assert last_filters["location_id"] is None
    assert last_filters["rate_unit"] is None
    assert last_filters["published_year"] is None


def test_large_location_set_keeps_quick_filter_bounded(monkeypatch):
    locations = [{"id": row, "name": f"Mauza {row}"} for row in range(1, 601)]
    monkeypatch.setattr(dc_rates, "list_locations", lambda: locations)
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [])
    root = dc_rates.build_dc_rates_page(FakePage())

    location_filter = next(control for control in _walk(root)
                           if isinstance(control, ft.Dropdown) and control.label == "Location")
    assert len(location_filter.options) == 501  # all-locations option plus first 500
    assert any("search covers all locations" in (control.value or "")
               for control in _walk(root) if isinstance(control, ft.Text))


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


def test_primary_filters_and_collapsed_extended_filters_are_present(monkeypatch):
    monkeypatch.setattr(dc_rates, "list_locations", lambda: [])
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [])
    monkeypatch.setattr(dc_rates, "summarize_dc_rates", lambda **_: {
        "count": 0, "lowest": None, "highest": None,
    })
    root = dc_rates.build_dc_rates_page(FakePage())
    controls = list(_walk(root))
    labels = {control.label for control in controls if isinstance(control, (ft.TextField, ft.Dropdown))}
    text_labels = {control.value for control in controls if isinstance(control, ft.Text)}
    assert {"Mauza", "Square no.", "Kila no.", "Khasra no."} <= text_labels
    assert {"Rate unit", "Schedule"} <= labels
    autocompletes = [control for control in controls if isinstance(control, ft.AutoComplete)]
    assert len(autocompletes) == 4
    schedule = next(control for control in controls if isinstance(control, ft.Dropdown) and control.label == "Schedule")
    assert [option.key for option in schedule.options] == ["dc_rural", "dc_urban", "fbr", "all"]
    search = next(control for control in controls if isinstance(control, ft.TextField)
                  and control.label == "Search location, property, classification, or source")
    search_bar = next(control for control in controls if isinstance(control, ft.ResponsiveRow)
                      and any(child.content is search for child in control.controls
                              if isinstance(child, ft.Container)))
    extended = next(control for control in controls if isinstance(control, ft.ResponsiveRow)
                    and any(isinstance(child.content, ft.Dropdown) and child.content.label == "Location"
                            for child in control.controls if isinstance(child, ft.Container)))
    assert extended.visible is False

    filter_panel = next(control for control in controls if isinstance(control, ft.Container)
                        and control.height == 170 and isinstance(control.content, ft.Column))
    assert search_bar is not filter_panel.content.controls[0]
    assert filter_panel.content.scroll == ft.ScrollMode.AUTO
    assert filter_panel.visible is False


def test_filter_and_import_export_sections_toggle_independently(monkeypatch):
    monkeypatch.setattr(dc_rates, "list_locations", lambda: [])
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [])
    monkeypatch.setattr(dc_rates, "summarize_dc_rates", lambda **_: {
        "count": 0, "lowest": None, "highest": None,
    })
    page = FakePage()
    root = dc_rates.build_dc_rates_page(page)
    toolbar = root.controls[2]
    filter_button = next(control for control in toolbar.controls
                         if isinstance(control, ft.TextButton) and control.content == "Show filters")
    import_export_button = next(control for control in toolbar.controls
                                if isinstance(control, ft.TextButton)
                                and control.content == "Show import / export")
    filter_panel = next(control for control in _walk(root) if isinstance(control, ft.Container)
                        and control.height == 170)
    import_export_panel = next(control for control in _walk(root) if isinstance(control, ft.Column)
                               and control.visible is False
                               and any(isinstance(child, ft.Text) and "Import mapped" in (child.value or "")
                                       for child in control.controls))

    filter_button.on_click(None)
    assert filter_panel.visible is True
    assert filter_button.content == "Hide filters"
    assert import_export_panel.visible is False

    import_export_button.on_click(None)
    assert import_export_panel.visible is True
    assert import_export_button.content == "Hide import / export"
    assert filter_panel.visible is True


def test_deferred_rate_load_populates_mauza_suggestions_and_summary(monkeypatch):
    cadastral_calls = []
    monkeypatch.setattr(dc_rates, "list_locations", lambda: [])
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [_rate_record()])
    monkeypatch.setattr(dc_rates, "list_mauza_names", lambda **_kwargs: ["Test Mauza", "Other Mauza"])
    def list_cadastral(field_name, **kwargs):
        cadastral_calls.append((field_name, kwargs.get("search", "")))
        return ["12", "123"] if field_name == "square_no" else ["4"]
    monkeypatch.setattr(dc_rates, "list_cadastral_values", list_cadastral)
    monkeypatch.setattr(dc_rates, "summarize_dc_rates", lambda **_: {
        "count": 2, "lowest": "10", "highest": "50",
        "lowest_unit": "per_marla", "lowest_schedule": "dc",
        "highest_unit": "per_square_foot", "highest_schedule": "fbr",
    })
    page = FakePage()
    root = dc_rates.build_dc_rates_page(page, defer_initial_load=True)
    mauza = next(control for control in _walk(root) if isinstance(control, ft.AutoComplete))
    assert mauza.suggestions == []
    mauza.value = "Test Mauza"
    asyncio.run(page.background_task)

    assert [item.value for item in mauza.suggestions] == ["Test Mauza", "Other Mauza"]
    square, kila, khasra = [control for control in _walk(root) if isinstance(control, ft.AutoComplete)][1:]
    assert [option.value for option in square.suggestions] == ["12", "123"]
    assert [option.value for option in kila.suggestions] == ["4"]
    assert [option.value for option in khasra.suggestions] == ["4"]
    summary = next(control for control in _walk(root)
                   if isinstance(control, ft.Text) and "Lowest:" in (control.value or ""))
    assert "Lowest: 10 Per marla" in summary.value
    assert "Highest: 50 Per square foot" in summary.value

    square.value = "12"
    square.on_change(None)
    asyncio.run(page.background_task)
    assert ("square_no", "12") in cadastral_calls


def test_duplicate_cleanup_dialog_previews_candidates_before_removal(monkeypatch):
    record = {**_rate_record(), "duplicate_of": 6, "id": 7}
    monkeypatch.setattr(dc_rates, "list_locations", lambda: [])
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [])
    monkeypatch.setattr(dc_rates, "find_duplicate_dc_rates", lambda: [record])
    page = FakePage()
    root = dc_rates.build_dc_rates_page(page)
    duplicate_button = next(control for control in _walk(root)
                            if isinstance(control, ft.OutlinedButton)
                            and control.content == "Find / remove duplicates")

    duplicate_button.on_click(None)
    dialog = page.dialogs[-1]
    scan_button = next(control for control in dialog.actions
                       if isinstance(control, ft.OutlinedButton)
                       and control.content == "Scan for duplicates")
    scan_button.on_click(None)
    asyncio.run(page.background_task)

    remove_button = next(control for control in dialog.actions
                         if isinstance(control, ft.Button)
                         and str(control.content).startswith("Export and remove"))
    assert remove_button.disabled is False
    assert any("1 extra row(s)" in (control.value or "") for control in _walk(dialog.content)
               if isinstance(control, ft.Text))


def test_location_form_switches_rural_and_urban_fields(monkeypatch):
    monkeypatch.setattr(dc_rates, "list_locations", lambda: [])
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [])
    page = FakePage()
    root = dc_rates.build_dc_rates_page(page)
    root.controls[2].controls[2].on_click(object())
    dialog = page.dialogs[-1]
    controls = list(_walk(dialog.content))
    land_type = next(
        control for control in controls
        if isinstance(control, ft.RadioGroup)
    )
    rural = next(
        control for control in controls
        if isinstance(control, ft.Column) and control.controls
        and isinstance(control.controls[0], ft.Text)
        and control.controls[0].value == "Rural schedule details"
    )
    urban = next(
        control for control in controls
        if isinstance(control, ft.Column) and control.controls
        and isinstance(control.controls[0], ft.Text)
        and control.controls[0].value == "Urban schedule details"
    )
    assert urban.visible is True
    assert rural.visible is False
    land_type.value = "rural"
    land_type.on_change(object())
    assert rural.visible is True
    assert urban.visible is False


def test_import_mapping_dialog_switches_between_schedule_field_profiles(monkeypatch):
    class FakePicker:
        async def pick_files(self, **_kwargs):
            return [SimpleNamespace(bytes=b"test", path=None, name="schedule.csv")]

    monkeypatch.setattr(ft, "FilePicker", FakePicker)
    monkeypatch.setattr(dc_rates, "list_locations", lambda: [])
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [])
    monkeypatch.setattr(dc_rates, "summarize_dc_rates", lambda **_: {
        "count": 0, "lowest": None, "highest": None,
    })
    monkeypatch.setattr(dc_rates, "read_schedule_spreadsheet", lambda *_: (
        ["District", "Tehsil", "Mauza", "Location", "Rate"], []
    ))
    page = FakePage()
    root = dc_rates.build_dc_rates_page(page)

    import_button = next(control for control in _walk(root)
                         if isinstance(control, ft.OutlinedButton)
                         and control.content == "Import CSV / Excel / ODS")
    asyncio.run(import_button.on_click())
    dialog = page.dialogs[-1]
    radio = next(control for control in _walk(dialog.content) if isinstance(control, ft.RadioGroup))
    labels = {control.label for control in _walk(dialog.content) if isinstance(control, ft.Dropdown)}
    assert "Mauza" in labels
    assert "Qanoongoi" in labels
    assert "Khasra no. (optional)" in labels
    assert "Structure rate" not in labels

    radio.value = "urban_dc"
    radio.on_change(None)
    labels = {control.label for control in _walk(dialog.content) if isinstance(control, ft.Dropdown)}
    assert "Town" in labels
    assert "Structure rate" in labels
    assert "Mauza" not in labels

    radio.value = "fbr"
    radio.on_change(None)
    labels = {control.label for control in _walk(dialog.content) if isinstance(control, ft.Dropdown)}
    assert "Value per square foot (Rs.)" in labels
    assert any(label.startswith("Per marla valuation") for label in labels)
    assert "Kila no. (optional)" in labels
    assert "Khasra no. (optional)" in labels
    assert "Unit" not in labels


def test_large_import_is_scheduled_in_background_and_reports_completion(monkeypatch):
    class FakePicker:
        async def pick_files(self, **_kwargs):
            return [SimpleNamespace(bytes=b"test", path=None, name="bulk.csv")]

    monkeypatch.setattr(ft, "FilePicker", FakePicker)
    monkeypatch.setattr(dc_rates, "list_locations", lambda: [])
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [])
    monkeypatch.setattr(dc_rates, "summarize_dc_rates", lambda **_: {
        "count": 0, "lowest": None, "highest": None,
    })
    monkeypatch.setattr(dc_rates, "read_schedule_spreadsheet", lambda *_: (
        ["Tehsil", "Qanoongoi", "Mauza", "Classification", "Location", "Square No", "Kila No", "DC Rate", "Unit"],
        [["Tehsil", "Qanoongoi", "Mauza", "Agricultural", "Farm", "1", "2", "100", "per kanal"]],
    ))
    candidate = {
        "row": 2,
        "location": {"name": "Mauza"},
        "property_type": "Agricultural",
        "classification": "Agricultural",
        "rate_value": 100,
        "rate_unit": "per_kanal",
    }
    monkeypatch.setattr(dc_rates, "preview_mapped_schedule", lambda *_: {
        "valid_records": [candidate], "issues": [], "rows_read": 1,
    })
    imported = []
    monkeypatch.setattr(dc_rates, "import_dc_rate_records", lambda records: (
        imported.extend(records) or {"imported_count": len(records), "issues": []}
    ))

    page = FakePage()
    root = dc_rates.build_dc_rates_page(page)
    import_button = next(control for control in _walk(root)
                         if isinstance(control, ft.OutlinedButton)
                         and control.content == "Import CSV / Excel / ODS")
    asyncio.run(import_button.on_click())
    mapping_dialog = page.dialogs[-1]
    mapping_dialog.actions[-1].on_click(None)
    review_dialog = page.dialogs[-1]
    year_field = next(control for control in _walk(review_dialog.content)
                      if isinstance(control, ft.TextField)
                      and control.label == "Published year for this import (optional)")
    year_field.value = "2028"
    review_dialog.actions[-1].on_click(None)

    status_text = next(control for control in _walk(root)
                       if isinstance(control, ft.Text) and "running in the background" in (control.value or ""))
    assert imported == []
    assert "running in the background" in status_text.value

    asyncio.run(page.background_task)
    # The import task schedules a separate refresh task after its database
    # write; await that second task as well to avoid leaking test coroutines.
    asyncio.run(page.background_task)

    assert imported[0]["published_year"] == 2028
    assert imported[0]["effective_from"] == date(2028, 1, 1)
    assert "Imported 1 Rural DC rate(s)" in status_text.value
