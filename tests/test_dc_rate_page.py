"""UI callback regression tests for DC-rate page actions."""

from datetime import date
import asyncio
from types import SimpleNamespace
from types import SimpleNamespace

import flet as ft
import pytest

from services.dc_rate_duplicate_service import BUILTIN_PROFILES, profile_to_dict
from ui.pages import dc_rates


@pytest.fixture(autouse=True)
def _isolate_cadastral_settings(monkeypatch):
    """Keep UI tests away from the live settings/database."""
    saved_modes = []
    monkeypatch.setattr(dc_rates, "get_cadastral_storage_mode", lambda: "ranges")
    monkeypatch.setattr(
        dc_rates, "set_cadastral_storage_mode", lambda mode: saved_modes.append(mode)
    )
    monkeypatch.setattr(
        dc_rates,
        "get_card_appearance",
        lambda: {
            "heading_template": "{schedule} schedule · {location}",
            "background_color": "#FFFFFF",
            "border_color": "#E2E8F0",
            "heading_color": "#0F172A",
            "body_color": "#475569",
        },
    )
    monkeypatch.setattr(dc_rates, "get_default_file_location", lambda: "")
    return saved_modes


@pytest.fixture(autouse=True)
def _builtin_card_layout(monkeypatch):
    """Pin the built-in card layout for every rate-card test.

    ``get_card_fields`` usually reads the developer's *live* saved settings;
    the new card-content editor means a configured card body would replace
    the built-in text this module asserts on. Tests that exercise custom
    card content patch ``get_card_fields`` again with their own
    ``monkeypatch.setattr``, which simply overrides this default.
    """
    monkeypatch.setattr(
        dc_rates, "get_card_fields", lambda: {"fields": [], "formulas": []}
    )


def _mock_duplicate_profiles(monkeypatch):
    monkeypatch.setattr(
        dc_rates,
        "list_duplicate_rule_profiles",
        lambda **_: [profile_to_dict(profile) for profile in BUILTIN_PROFILES],
    )


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


def _page_column(root):
    """Return the scrollable page column when a busy overlay wraps it."""
    if isinstance(root, ft.Stack) and root.controls:
        return root.controls[0]
    return root


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
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
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

    add_button = _page_column(root).controls[2].controls[0]
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
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [{"id": 3, "name": "Test Mauza"}], "qanoongoi": [], "has_more": False})
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
    _page_column(root).controls[2].controls[0].on_click(object())
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
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **kwargs: {"locations": locations, "qanoongoi": dc_rates._unique_location_values(locations, "qanoongoi"), "has_more": len(locations) > kwargs.get("limit", 500)})
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [])
    root = dc_rates.build_dc_rates_page(FakePage())

    location_filter = next(control for control in _walk(root)
                           if isinstance(control, ft.Dropdown) and control.label == "Location")
    assert len(location_filter.options) == 501  # all-locations option plus first 500
    assert any("search covers all locations" in (control.value or "")
               for control in _walk(root) if isinstance(control, ft.Text))


def test_qanoongoi_choices_are_unique_and_cascade_to_mauza_suggestions(monkeypatch):
    locations = [
        {"id": 1, "name": "Location A", "qanoongoi": "Qanoongoi A"},
        {"id": 2, "name": "Location B", "qanoongoi": " qanoongoi a "},
        {"id": 3, "name": "Location C", "qanoongoi": "Qanoongoi B"},
        {"id": 4, "name": "Location D", "qanoongoi": ""},
    ]
    calls = []
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **kwargs: {"locations": locations, "qanoongoi": dc_rates._unique_location_values(locations, "qanoongoi"), "has_more": len(locations) > kwargs.get("limit", 500)})
    monkeypatch.setattr(
        dc_rates, "list_dc_rates",
        lambda **kwargs: calls.append(("rates", kwargs)) or [],
    )
    monkeypatch.setattr(
        dc_rates, "summarize_dc_rates",
        lambda **_: {
            "count": 0, "lowest": None, "highest": None,
            "lowest_square": None, "highest_square": None,
            "lowest_kila": None, "highest_kila": None,
        },
    )
    monkeypatch.setattr(
        dc_rates, "list_mauza_names",
        lambda **kwargs: calls.append(("mauza", kwargs)) or ["Location A"],
    )
    page = FakePage()
    root = dc_rates.build_dc_rates_page(page)
    qanoongoi = next(
        control for control in _walk(root)
        if isinstance(control, ft.AutoComplete) and control.data == "qanoongoi"
    )
    mauza = next(
        control for control in _walk(root)
        if isinstance(control, ft.AutoComplete) and control.data == "mauza"
    )

    assert [item.value for item in qanoongoi.suggestions] == [
        "Qanoongoi A", "Qanoongoi B",
    ]
    mauza.value = "Old Mauza"
    qanoongoi.value = "Qanoongoi A"
    qanoongoi.on_change(None)
    asyncio.run(page.background_task)

    assert mauza.value == ""
    rate_filters = [kwargs for kind, kwargs in calls if kind == "rates"][-1]
    mauza_filters = [kwargs for kind, kwargs in calls if kind == "mauza"][-1]
    assert rate_filters["qanoongoi"] == "Qanoongoi A"
    assert mauza_filters["qanoongoi"] == "Qanoongoi A"
    # The filter must not hide Mauzas merely because the district has more
    # than the previous 50-name default.
    assert mauza_filters["limit"] >= 500


def test_area_summary_dialog_shows_hierarchy_counts(monkeypatch):
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [])
    monkeypatch.setattr(dc_rates, "summarize_location_hierarchy", lambda: [
        {
            "district": "District A",
            "tehsil": "Tehsil A",
            "qanoongoi": "Qanoongoi A",
            "mauza_count": 3,
            "tehsil_count": 2,
            "qanoongoi_count": 2,
        },
        {
            "district": "District A",
            "tehsil": "Tehsil B",
            "qanoongoi": "Qanoongoi B",
            "mauza_count": 4,
            "tehsil_count": 2,
            "qanoongoi_count": 1,
        },
    ])
    page = FakePage()
    root = dc_rates.build_dc_rates_page(page)
    summary_button = next(
        control for control in _page_column(root).controls[2].controls
        if isinstance(control, ft.OutlinedButton) and control.content == "Tehsil Details"
    )
    summary_button.on_click(None)

    dialog = page.dialogs[-1]
    assert dialog.title.value == "Tehsil details"
    assert any("1 district(s) · 2 tehsil(s) · 2 qanoongoi(s)" in (control.value or "")
               for control in _walk(dialog.content) if isinstance(control, ft.Text))
    tables = [control for control in _walk(dialog.content) if isinstance(control, ft.DataTable)]
    assert len(tables) == 2
    district_table, detail_table = tables
    assert [[cell.content.value for cell in row.cells] for row in district_table.rows] == [[
        "District A", "2", "2",
    ]]
    assert [[cell.content.value for cell in row.cells] for row in detail_table.rows] == [
        ["District A", "Tehsil A", "Qanoongoi A", "3"],
        ["District A", "Tehsil B", "Qanoongoi B", "4"],
    ]


def test_edit_button_passes_the_rate_id_explicitly(monkeypatch):
    record = _rate_record()
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [{"id": 3, "name": "Test Mauza"}], "qanoongoi": [], "has_more": False})
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
    record.update({"square_no": "7-8", "kila_no": "4", "khasra_no": "21/3"})
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [record])
    monkeypatch.setattr(dc_rates, "get_dc_rate", lambda _rate_id: record)
    page = FakePage()

    root = dc_rates.build_dc_rates_page(page)
    text_values = [control.value for control in _walk(root) if isinstance(control, ft.Text)]
    assert any("Test Mauza" in value and "DC schedule" in value for value in text_values)
    assert any("Rate: 12345.00" in value for value in text_values)
    assert any("Residential" in value for value in text_values)
    assert any("Square no.: 7-8" in value and "Kila no.: 4" in value
               and "Khasra no.: 21/3" in value and "Unit: Marla" in value for value in text_values)
    next(control for control in _walk(root)
         if isinstance(control, ft.TextButton) and control.content == "Details").on_click(None)
    detail_text = [control.value for control in _walk(page.dialogs[-1].content)
                   if isinstance(control, ft.Text)]
    assert any("Square no.: 7-8" in value and "Khasra no.: 21/3" in value for value in detail_text)
    assert "Unit: Marla" in detail_text


def test_primary_filters_and_collapsed_extended_filters_are_present(monkeypatch):
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [])
    monkeypatch.setattr(dc_rates, "summarize_dc_rates", lambda **_: {
        "count": 0, "lowest": None, "highest": None,
    })
    root = dc_rates.build_dc_rates_page(FakePage())
    controls = list(_walk(root))
    labels = {control.label for control in controls if isinstance(control, (ft.TextField, ft.Dropdown))}
    text_labels = {control.value for control in controls if isinstance(control, ft.Text)}
    assert {"Qanoongoi", "Mauza", "Square no.", "Kila no.", "Khasra no."} <= text_labels
    assert {"Rate unit", "Schedule"} <= labels
    autocompletes = [control for control in controls if isinstance(control, ft.AutoComplete)]
    assert len(autocompletes) == 5
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
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [])
    monkeypatch.setattr(dc_rates, "summarize_dc_rates", lambda **_: {
        "count": 0, "lowest": None, "highest": None,
    })
    page = FakePage()
    root = dc_rates.build_dc_rates_page(page)
    toolbar = _page_column(root).controls[2]
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


def test_cadastral_selection_dialog_summarizes_and_saves_slash_khewat(monkeypatch):
    context = {
        "province": "Punjab", "district": "Test District", "tehsil": "Test Tehsil",
        "qanoongoi": None, "patwar_circle": None, "mauza_name": "Test Mauza",
        "hadbast_number": "123",
    }
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [])
    monkeypatch.setattr(dc_rates, "summarize_dc_rates", lambda **_: {
        "count": 0, "lowest": None, "highest": None,
    })
    monkeypatch.setattr(dc_rates, "list_mauza_contexts", lambda: [context])
    monkeypatch.setattr(dc_rates, "summarize_mauza_references", lambda **kwargs: [
        {"serial_no": 1, "mauza": "Test Mauza", "square_no": "12", "kila_no": "",
         "khasra_no": "5", "unit": "Marla", "schedule": "DC valuation: Rs. 100"},
        {"serial_no": 2, "mauza": "Test Mauza", "square_no": "", "kila_no": "4",
         "khasra_no": "6", "unit": "Acre", "schedule": "FBR valuation: Rs. 200"},
    ])
    monkeypatch.setattr(dc_rates, "list_rhz_years", lambda _context=None: ["2020-21", "2019-20"])
    monkeypatch.setattr(
        dc_rates, "list_mauza_cadastral_values",
        lambda _context, field, **_: ["12"] if field == "square_no" else ["4"],
    )
    monkeypatch.setattr(dc_rates, "list_khewats_for_mauza", lambda _context: [])
    monkeypatch.setattr(dc_rates, "find_khewat_year_conflicts", lambda **_: [])
    monkeypatch.setattr(dc_rates, "latest_rhz_year", lambda _context: "2020-21")
    summary_calls = []
    monkeypatch.setattr(dc_rates, "summarize_mauza_cadastral_selection", lambda **kwargs: (
        summary_calls.append(kwargs) or {
            "count": 2,
            "lowest_square": "12", "highest_square": "12",
            "lowest_kila": "4", "highest_kila": "4",
            "lowest_rate": 100, "highest_rate": 200,
        }
    ))
    assignments = []
    monkeypatch.setattr(dc_rates, "assign_cadastral_references_to_khewat", lambda **kwargs: (
        assignments.append(kwargs) or {
            "khewat_number": kwargs["khewat_number"], "added": 2, "existing": 0,
        }
    ))

    page = FakePage()
    root = dc_rates.build_dc_rates_page(page)
    selection_button = next(control for control in _page_column(root).controls[2].controls
                            if isinstance(control, ft.OutlinedButton)
                            and control.content == "Select squares / Kilas")
    selection_button.on_click(None)
    dialog = page.dialogs[-1]
    controls = list(_walk(dialog.content))
    location_fields = [control for control in controls if isinstance(control, ft.AutoComplete)]
    assert len(location_fields) == 3
    assert {control.value for control in location_fields} == {"Test Mauza", "Test District", "Test Tehsil"}
    assert {control.value for control in controls if isinstance(control, ft.Text)} >= {
        "Mauza", "District", "Tehsil"
    }
    boxes = [control for control in controls if isinstance(control, ft.Checkbox)]
    assert [box.label for box in boxes] == ["12", "4"]
    next(control for control in _walk(dialog.content)
         if isinstance(control, ft.OutlinedButton) and control.content == "Clear selection").on_click(None)
    assert all(not control.value for control in _walk(dialog.content)
               if isinstance(control, ft.Checkbox))
    boxes = [control for control in _walk(dialog.content) if isinstance(control, ft.Checkbox)]
    for box in boxes:
        box.value = True
        box.on_change(SimpleNamespace(control=box))
    cadastral_table = next(control for control in _walk(dialog.content)
                           if isinstance(control, ft.DataTable) and len(control.columns) == 7)
    assert len(cadastral_table.rows) == 2
    assert cadastral_table.rows[0].cells[-2].content.value == "Marla"
    assert cadastral_table.rows[0].cells[-1].content.value == "DC valuation: Rs. 100"

    next(control for control in _walk(dialog.content)
         if isinstance(control, ft.Button) and control.content == "Show summary").on_click(None)
    assert summary_calls[0]["square_numbers"] == ["12"]
    assert summary_calls[0]["kila_numbers"] == ["4"]
    assert any("Square range: 12 to 12" in (control.value or "")
               for control in _walk(dialog.content) if isinstance(control, ft.Text))
    table_button = next(control for control in _walk(dialog.content)
                        if isinstance(control, ft.TextButton) and control.content == "Show table")
    table_button.on_click(None)
    table = next(control for control in _walk(dialog.content)
                 if isinstance(control, ft.DataTable) and len(control.rows) == 7)
    assert table.visible is True
    assert len(table.rows) == 7

    khewat = next(control for control in _walk(dialog.content)
                  if isinstance(control, ft.TextField) and control.label == "Khewat number")
    khewat.value = "30/2/1"
    year = next(control for control in _walk(dialog.content)
                if isinstance(control, ft.Dropdown) and control.label == "RHZ year")
    year.value = "2019-20"
    next(control for control in _walk(dialog.content)
         if isinstance(control, ft.Button) and control.content == "Add selected to Khewat").on_click(None)
    assert assignments[0]["khewat_number"] == "30/2/1"
    assert assignments[0]["rhz_year"] == "2019-20"

    khewat.value = "31/1"
    year.value = None
    year.text = ""
    next(control for control in _walk(dialog.content)
         if isinstance(control, ft.Button) and control.content == "Add selected to Khewat").on_click(None)
    confirmation = page.dialogs[-1]
    assert confirmation.title.value == "Use the latest saved RHZ year?"
    next(control for control in confirmation.actions
         if isinstance(control, ft.Button) and control.content == "Continue with 2020-21").on_click(None)
    assert assignments[1]["khewat_number"] == "31/1"
    assert assignments[1]["rhz_year"] == "2020-21"

    existing_record = {
        **context, "id": 8, "khewat_number": "30/2/1", "rhz_year": "2019-20",
        "khewat_size": None, "squares": ["12"], "kilas": ["4"],
    }
    monkeypatch.setattr(dc_rates, "find_khewat_year_conflicts", lambda **_: [existing_record])
    khewat.value = "32/1"
    year.value = "2019-20"
    next(control for control in _walk(dialog.content)
         if isinstance(control, ft.Button) and control.content == "Add selected to Khewat").on_click(None)
    conflict_dialog = page.dialogs[-1]
    assert conflict_dialog.title.value == "Khewats already exist for this RHZ year"
    next(control for control in conflict_dialog.actions
         if isinstance(control, ft.Button) and control.content == "Create another Khewat").on_click(None)
    assert assignments[2]["khewat_number"] == "32/1"


def test_main_khewat_search_filters_and_opens_saved_record(monkeypatch):
    context = {
        "province": "Punjab", "district": "Test District", "tehsil": "Test Tehsil",
        "qanoongoi": None, "patwar_circle": None, "mauza_name": "Test Mauza",
        "hadbast_number": "123",
    }
    record = {
        **context, "id": 42, "khewat_number": "30/1", "rhz_year": "2019-20",
        "khewat_size": "12-4-0", "squares": ["12"], "kilas": ["4"],
    }
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [])
    monkeypatch.setattr(dc_rates, "summarize_dc_rates", lambda **_: {
        "count": 0, "lowest": None, "highest": None,
    })
    monkeypatch.setattr(dc_rates, "search_khewats", lambda **_: [record])
    monkeypatch.setattr(dc_rates, "list_saved_khewat_mauzas", lambda **_: ["Test Mauza"])
    monkeypatch.setattr(dc_rates, "list_saved_khewat_numbers", lambda **_: ["30/1"])
    monkeypatch.setattr(dc_rates, "list_mauza_contexts", lambda: [context])
    monkeypatch.setattr(dc_rates, "list_rhz_years", lambda _context=None: ["2019-20"])
    monkeypatch.setattr(
        dc_rates, "list_mauza_cadastral_values",
        lambda _context, field, **_: ["12"] if field == "square_no" else ["4"],
    )
    monkeypatch.setattr(dc_rates, "list_khewats_for_mauza", lambda _context: [record])

    page = FakePage()
    root = dc_rates.build_dc_rates_page(page)
    view_button = next(control for control in _page_column(root).controls[2].controls
                       if isinstance(control, ft.OutlinedButton) and control.content == "View Khewats")
    view_button.on_click(None)
    search_dialog = page.dialogs[-1]
    autocomplete = [control for control in _walk(search_dialog.content)
                    if isinstance(control, ft.AutoComplete)]
    assert autocomplete[0].suggestions[0].value == "Test Mauza"
    assert autocomplete[1].suggestions[0].value == "30/1"
    autocomplete[0].value = "Test"
    autocomplete[0].on_change(None)
    assert any(isinstance(control, ft.Dropdown) and control.label == "RHZ year"
               for control in _walk(search_dialog.content))
    edit_button = next(control for control in _walk(search_dialog.content)
                       if isinstance(control, ft.OutlinedButton) and control.content == "View / edit")
    edit_button.on_click(None)
    edit_dialog = page.dialogs[-1]
    fields = list(_walk(edit_dialog.content))
    assert next(field for field in fields if isinstance(field, ft.TextField)
                and field.label == "Khewat number").value == "30/1"
    assert next(field for field in fields if isinstance(field, ft.Dropdown)
                and field.label == "RHZ year").value == "2019-20"
    assert next(field for field in fields if isinstance(field, ft.TextField)
                and field.label == "Khewat size (optional, K-M-F)").value == "12-4-0"


def test_deferred_rate_load_populates_mauza_suggestions_and_summary(monkeypatch):
    cadastral_calls = []
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [_rate_record()])
    def list_mauza_names(**_kwargs):
        return ["Test Mauza", "Other Mauza"]
    monkeypatch.setattr(dc_rates, "list_mauza_names", list_mauza_names)
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
    qanoongoi = next(
        control for control in _walk(root)
        if isinstance(control, ft.AutoComplete) and control.data == "qanoongoi"
    )
    mauza = next(
        control for control in _walk(root)
        if isinstance(control, ft.AutoComplete) and control.data == "mauza"
    )
    assert mauza.suggestions == []
    mauza.value = "Test Mauza"
    asyncio.run(page.background_task)

    assert [item.value for item in mauza.suggestions] == ["Test Mauza", "Other Mauza"]
    square, kila, khasra = [
        control for control in _walk(root)
        if isinstance(control, ft.AutoComplete) and control not in (qanoongoi, mauza)
    ]
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


def test_square_change_clears_kila_and_narrows_its_suggestions(monkeypatch):
    cadastral_calls = []
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [])
    monkeypatch.setattr(dc_rates, "summarize_dc_rates", lambda **_: {
        "count": 0, "lowest": None, "highest": None,
    })
    monkeypatch.setattr(dc_rates, "list_mauza_names", lambda **_: [])

    def list_cadastral(field_name, **kwargs):
        cadastral_calls.append((field_name, kwargs))
        if field_name == "square_no":
            return ["10", "11"]
        if field_name == "kila_no" and kwargs.get("square_no") == "10":
            return ["1", "2"]
        return []

    monkeypatch.setattr(dc_rates, "list_cadastral_values", list_cadastral)
    page = FakePage()
    root = dc_rates.build_dc_rates_page(page, defer_initial_load=True)
    asyncio.run(page.background_task)
    qanoongoi, mauza, square, kila, _ = [
        control for control in _walk(root)
        if isinstance(control, ft.AutoComplete)
    ]

    qanoongoi.value = "Jandiala"
    mauza.value = "Madar"
    kila.value = "9"
    square.value = "10"
    square.on_change(None)
    asyncio.run(page.background_task)

    assert kila.value == ""
    assert [option.value for option in kila.suggestions] == ["1", "2"]
    assert any(
        field_name == "kila_no"
        and kwargs.get("qanoongoi") == "Jandiala"
        and kwargs.get("mauza") == "Madar"
        and kwargs.get("square_no") == "10"
        for field_name, kwargs in cadastral_calls
    )


def test_duplicate_cleanup_dialog_previews_candidates_before_removal(monkeypatch):
    record = {**_rate_record(), "duplicate_of": 6, "id": 7}
    _mock_duplicate_profiles(monkeypatch)
    scanned_rules = []
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [])
    monkeypatch.setattr(
        dc_rates,
        "find_duplicate_dc_rates",
        lambda rule: scanned_rules.append(rule) or [record],
    )
    page = FakePage()
    root = dc_rates.build_dc_rates_page(page)
    duplicate_button = next(control for control in _walk(root)
                            if isinstance(control, ft.OutlinedButton)
                            and control.content == "Find / remove duplicates")

    duplicate_button.on_click(None)
    dialog = page.dialogs[-1]
    rule_dropdown = next(
        control for control in _walk(dialog.content)
        if isinstance(control, ft.Dropdown)
        and control.label == "Duplicate rule (required)"
    )
    rule_dropdown.value = "default:rural_dc_overlap"
    scan_button = next(control for control in dialog.actions
                       if isinstance(control, ft.OutlinedButton)
                       and control.content == "Scan for duplicates")
    scan_button.on_click(None)
    asyncio.run(page.background_task)

    assert scanned_rules == ["default:rural_dc_overlap"]

    remove_button = next(control for control in dialog.actions
                         if isinstance(control, ft.Button)
                         and str(control.content).startswith("Export and remove"))
    assert remove_button.disabled is False
    assert any("1 extra row(s)" in (control.value or "") for control in _walk(dialog.content)
               if isinstance(control, ft.Text))


def test_location_form_switches_rural_and_urban_fields(monkeypatch):
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [])
    page = FakePage()
    root = dc_rates.build_dc_rates_page(page)
    _page_column(root).controls[2].controls[2].on_click(object())
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


def _sheet(headers, rows, name="Sheet1"):
    return {
        "name": name,
        "headers": headers,
        "rows": rows,
        "row_numbers": list(range(2, len(rows) + 2)),
    }


def test_import_mapping_dialog_switches_between_schedule_field_profiles(monkeypatch):
    class FakePicker:
        async def pick_files(self, **_kwargs):
            return [SimpleNamespace(bytes=b"test", path=None, name="schedule.csv")]

    monkeypatch.setattr(ft, "FilePicker", FakePicker)
    _mock_duplicate_profiles(monkeypatch)
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [])
    monkeypatch.setattr(dc_rates, "summarize_dc_rates", lambda **_: {
        "count": 0, "lowest": None, "highest": None,
    })
    monkeypatch.setattr(dc_rates, "read_schedule_sheets", lambda *_: [
        _sheet(["District", "Tehsil", "Mauza", "Location", "Rate"], [])
    ])
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
    # Urban schedules may carry a Mauza column, so it is offered as optional.
    assert "Mauza (optional)" in labels

    radio.value = "fbr"
    radio.on_change(None)
    labels = {control.label for control in _walk(dialog.content) if isinstance(control, ft.Dropdown)}
    assert "Value per square foot (Rs.)" in labels
    assert any(label.startswith("Per marla valuation") for label in labels)
    assert "Kila no. (optional)" in labels
    assert "Khasra no. (optional)" in labels
    assert "Unit" not in labels
    assert "Mauza (optional)" in labels


def test_import_mapping_uses_row_values_to_disambiguate_duplicate_mauza_headers(monkeypatch):
    class FakePicker:
        async def pick_files(self, **_kwargs):
            return [SimpleNamespace(bytes=b"test", path=None, name="schedule.csv")]

    headers = [
        "District", "Tehsil", "Mauza", "Qanoongoi", "Mauza",
        "Classification", "Location", "Square No", "Killa No", "DC Rate", "Unit",
    ]
    rows = [
        ["District A", "Tehsil A", "District A", "Qanoongoi A", "Village One",
         "Residential", "Off road", "16", "8", "181600", "per_marla"],
        ["District A", "Tehsil A", "District A", "Qanoongoi A", "Village Two",
         "Residential", "Off road", "17", "9", "181600", "per_marla"],
    ]

    monkeypatch.setattr(ft, "FilePicker", FakePicker)
    _mock_duplicate_profiles(monkeypatch)
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [])
    monkeypatch.setattr(dc_rates, "summarize_dc_rates", lambda **_: {
        "count": 0, "lowest": None, "highest": None,
    })
    monkeypatch.setattr(dc_rates, "read_schedule_sheets", lambda *_: [
        _sheet(headers, rows)
    ])
    suggestion_calls = []
    original_suggestion = dc_rates.suggested_column_mapping

    def spy_suggestion(source_headers, source_rows=None):
        suggestion_calls.append((source_headers, source_rows))
        return original_suggestion(source_headers, source_rows)

    monkeypatch.setattr(dc_rates, "suggested_column_mapping", spy_suggestion)

    page = FakePage()
    root = dc_rates.build_dc_rates_page(page)
    import_button = next(control for control in _walk(root)
                         if isinstance(control, ft.OutlinedButton)
                         and control.content == "Import CSV / Excel / ODS")
    asyncio.run(import_button.on_click())
    dialog = page.dialogs[-1]

    assert suggestion_calls[-1] == (headers, rows)
    mauza_dropdown = next(
        control for control in _walk(dialog.content)
        if isinstance(control, ft.Dropdown) and control.label == "Mauza"
    )
    assert mauza_dropdown.value == "4"
    option_labels = [option.text for option in mauza_dropdown.options]
    assert "Mauza (column 3)" in option_labels
    assert "Mauza (column 5)" in option_labels


def test_large_import_is_scheduled_in_background_and_reports_completion(monkeypatch):
    class FakePicker:
        async def pick_files(self, **_kwargs):
            return [SimpleNamespace(bytes=b"test", path=None, name="bulk.csv")]

    monkeypatch.setattr(ft, "FilePicker", FakePicker)
    _mock_duplicate_profiles(monkeypatch)
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [])
    monkeypatch.setattr(dc_rates, "summarize_dc_rates", lambda **_: {
        "count": 0, "lowest": None, "highest": None,
    })
    monkeypatch.setattr(dc_rates, "read_schedule_sheets", lambda *_: [
        _sheet(
            ["Tehsil", "Qanoongoi", "Mauza", "Classification", "Location",
             "Square No", "Kila No", "DC Rate", "Unit"],
            [["Tehsil", "Qanoongoi", "Mauza", "Agricultural", "Farm",
              "1", "2", "100", "per kanal"]],
        )
    ])
    candidate = {
        "row": 2,
        "location": {"name": "Mauza"},
        "property_type": "Agricultural",
        "classification": "Agricultural",
        "rate_value": 100,
        "rate_unit": "per_kanal",
    }
    preview_calls = []
    monkeypatch.setattr(dc_rates, "preview_mapped_schedule", lambda *_, **kwargs: (
        preview_calls.append(kwargs)
        or {
            "valid_records": [candidate],
            "issues": [],
            "warnings": [],
            "rows_read": 1,
            "duplicate_rule": {"name": "Rural DC - same cadastral row (overlap)"},
        }
    ))
    imported = []
    monkeypatch.setattr(dc_rates, "import_dc_rate_records", lambda records, **kwargs: (
        imported.extend(records)
        or {"imported_count": len(records), "issues": [], "warnings": []}
    ))

    page = FakePage()
    root = dc_rates.build_dc_rates_page(page)
    import_button = next(control for control in _walk(root)
                         if isinstance(control, ft.OutlinedButton)
                         and control.content == "Import CSV / Excel / ODS")
    asyncio.run(import_button.on_click())
    mapping_dialog = page.dialogs[-1]
    assert not any(
        isinstance(control, ft.Dropdown)
        and control.label == "Duplicate rule (required)"
        for control in _walk(mapping_dialog.content)
    )
    assert any(
        isinstance(control, ft.Text)
        and "removed only when" in (control.value or "")
        for control in _walk(mapping_dialog.content)
    )
    mapping_dialog.actions[-1].on_click(None)
    review_dialog = page.dialogs[-1]
    assert preview_calls[-1]["duplicate_policy"] == "check_now"
    assert preview_calls[-1]["storage_mode"] == "ranges"
    assert preview_calls[-1]["duplicate_rule"] == dc_rates.EXACT_ALL_REFERENCE
    assert preview_calls[-1]["row_numbers"] == [2]
    assert preview_calls[-1]["row_sheets"] == ["Sheet1"]
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


def test_import_mapping_shows_automatic_exact_duplicate_rule(monkeypatch):
    class FakePicker:
        async def pick_files(self, **_kwargs):
            return [SimpleNamespace(bytes=b"test", path=None, name="schedule.csv")]

    monkeypatch.setattr(ft, "FilePicker", FakePicker)
    _mock_duplicate_profiles(monkeypatch)
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [])
    monkeypatch.setattr(dc_rates, "summarize_dc_rates", lambda **_: {
        "count": 0, "lowest": None, "highest": None,
    })
    monkeypatch.setattr(dc_rates, "read_schedule_sheets", lambda *_: [
        _sheet(
            ["Tehsil", "Qanoongoi", "Mauza", "Classification", "Location",
             "DC Rate", "Unit"],
            [["Tehsil", "Qanoongoi", "Mauza", "Agricultural", "Farm",
              "100", "per kanal"]],
        )
    ])
    preview_calls = []
    monkeypatch.setattr(dc_rates, "preview_mapped_schedule", lambda *_, **kwargs: (
        preview_calls.append(kwargs)
        or {
            "valid_records": [],
            "issues": [],
            "warnings": [],
            "rows_read": 1,
            "expanded_rows": 1,
            "duplicates_removed": 0,
            "invalid_rows": 0,
            "duplicate_rule": {"name": "Any schedule - all columns exactly identical"},
        }
    ))
    page = FakePage()
    root = dc_rates.build_dc_rates_page(page)
    import_button = next(control for control in _walk(root)
                         if isinstance(control, ft.OutlinedButton)
                         and control.content == "Import CSV / Excel / ODS")

    asyncio.run(import_button.on_click())
    dialog = page.dialogs[-1]
    assert not any(
        isinstance(control, ft.Dropdown)
        and control.label == "Duplicate rule (required)"
        for control in _walk(dialog.content)
    )
    assert any(
        isinstance(control, ft.Text)
        and "removed only when" in (control.value or "")
        for control in _walk(dialog.content)
    )
    dialog.actions[-1].on_click(None)

    assert len(preview_calls) == 1
    assert preview_calls[0]["duplicate_policy"] == "check_now"
    assert preview_calls[0]["storage_mode"] == "ranges"
    assert preview_calls[0]["duplicate_rule"] == dc_rates.EXACT_ALL_REFERENCE
    assert len(page.dialogs) == 1
    assert any(
        "Any schedule - all columns exactly identical" in (control.value or "")
        for control in _walk(page.dialogs[-1].content)
        if isinstance(control, ft.Text)
    )


def test_duplicate_rules_dialog_lists_rules_and_opens_editor(monkeypatch):
    _mock_duplicate_profiles(monkeypatch)
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [])
    page = FakePage()
    root = dc_rates.build_dc_rates_page(page)
    rules_button = next(control for control in _walk(root)
                        if isinstance(control, ft.OutlinedButton)
                        and control.content == "Duplicate rules")

    rules_button.on_click(None)
    dialog = page.dialogs[-1]
    assert dialog.title.value == "Duplicate detection rules"
    text_values = {
        control.value for control in _walk(dialog.content)
        if isinstance(control, ft.Text)
    }
    assert "Rural DC - same cadastral row (overlap)" in text_values
    assert any("Built-in" in (value or "") for value in text_values)

    dialog.actions[-1].on_click(None)
    editor = page.dialogs[-1]
    assert editor.title.value == "New duplicate rule"
    labels = {
        control.label for control in _walk(editor.content)
        if isinstance(control, ft.Dropdown)
    }
    assert {"Schedule scope", "Effective-date matching", "When a duplicate is found"} <= labels


def _first_rate_card(root):
    rate_list = next(control for control in _walk(root) if isinstance(control, ft.ListView))
    return next(control for control in rate_list.controls if isinstance(control, ft.Container))


def test_rate_card_uses_default_appearance(monkeypatch):
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    record = {**_rate_record(), "schedule_type": "dc", "published_year": 2025}
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [record])

    root = dc_rates.build_dc_rates_page(FakePage())
    card = _first_rate_card(root)
    heading = card.content.controls[0].controls[0]

    assert heading.value == "DC schedule · Test Mauza"
    assert heading.color == "#0F172A"
    assert card.bgcolor == "#FFFFFF"
    assert card.border is not None
    assert card.content.controls[1].color == "#475569"


def test_rate_card_uses_saved_heading_and_colours(monkeypatch):
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    record = {**_rate_record(), "schedule_type": "fbr", "published_year": 2025}
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [record])
    monkeypatch.setattr(
        dc_rates,
        "get_card_appearance",
        lambda: {
            "heading_template": "{schedule} - {location} ({year})",
            "background_color": "#FFF8E7",
            "border_color": "#C2410C",
            "heading_color": "#7C2D12",
            "body_color": "#1E293B",
        },
    )

    root = dc_rates.build_dc_rates_page(FakePage())
    card = _first_rate_card(root)
    heading = card.content.controls[0].controls[0]

    assert heading.value == "FBR - Test Mauza (2025)"
    assert heading.color == "#7C2D12"
    assert card.bgcolor == "#FFF8E7"
    assert card.content.controls[1].color == "#1E293B"


def test_rate_card_renders_chosen_fields_and_formula_lines(monkeypatch):
    """Custom card content replaces the built-in body with chosen fields."""
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    record = _rate_record()
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [record])
    monkeypatch.setattr(dc_rates, "get_card_fields", lambda: {
        "fields": ["classification", "rate", "mauza"],
        "formulas": [{"label": "Per 1 kanal", "expression": "{rate_value} * 20"}],
    })

    root = dc_rates.build_dc_rates_page(FakePage())
    card = _first_rate_card(root)
    body_values = [
        control.value for control in card.content.controls
        if isinstance(control, ft.Text)
    ]

    assert body_values[0] == (
        "Classification: Residential · Rate (value + unit): "
        "1 Per marla · Mauza: Test Mauza"
    )
    assert body_values[1] == "Per 1 kanal: 20"
    # The built-in body lines are replaced, not duplicated.
    assert not any("Floor:" in value for value in body_values if value)
    assert not any("Published year:" in value for value in body_values if value)


def test_rate_card_formula_without_a_number_shows_a_visible_gap(monkeypatch):
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    # A record without a numeric rate value cannot compute the formula.
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [{
        **_rate_record(), "rate_value": "",
    }])
    # A formula alone switches the card to custom rendering, because the
    # formula needs somewhere to render even with no chosen fields.
    monkeypatch.setattr(dc_rates, "get_card_fields", lambda: {
        "fields": [],
        "formulas": [{"label": "Per 1 kanal", "expression": "{rate_value} * 20"}],
    })

    root = dc_rates.build_dc_rates_page(FakePage())
    card = _first_rate_card(root)
    body_values = [
        control.value for control in card.content.controls
        if isinstance(control, ft.Text)
    ]
    assert "Per 1 kanal: —" in body_values


def _stub_empty_rate_page(monkeypatch):
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_: [])
    monkeypatch.setattr(dc_rates, "summarize_dc_rates", lambda **_: {
        "count": 0, "lowest": None, "highest": None,
    })


def _export_button(root, label="Export CSV"):
    return next(
        control for control in _walk(root)
        if isinstance(control, ft.OutlinedButton) and control.content == label
    )


def test_schedule_export_writes_to_the_default_folder(monkeypatch, tmp_path):
    _stub_empty_rate_page(monkeypatch)
    monkeypatch.setattr(dc_rates, "get_default_file_location", lambda: str(tmp_path))
    prompts = []

    async def choose_default(*_args, **kwargs):
        prompts.append(kwargs)
        return "default"

    monkeypatch.setattr(dc_rates, "ask_location_choice", choose_default)
    monkeypatch.setattr(
        dc_rates,
        "export_schedule_records",
        lambda records, file_format, reference_mode=None: b"exported",
    )
    picker_calls = []

    class FakePicker:
        async def save_file(self, **kwargs):
            picker_calls.append(kwargs)
            return "picked.csv"

    monkeypatch.setattr(ft, "FilePicker", FakePicker)
    page = FakePage()
    root = dc_rates.build_dc_rates_page(page)

    asyncio.run(_export_button(root).on_click())
    page.dialogs[-1].actions[-1].on_click(None)
    asyncio.run(page.background_task)

    target = tmp_path / f"valuation_schedules_{date.today().isoformat()}.csv"
    assert target.read_bytes() == b"exported"
    assert picker_calls == []
    assert prompts[-1]["action"] == "valuation schedule CSV export"
    assert prompts[-1]["default_location"] == str(tmp_path)
    assert any(
        isinstance(control, ft.Text)
        and f"Exported 0 record(s)" in (control.value or "")
        and str(target) in (control.value or "")
        for control in _walk(root)
    )


def test_schedule_export_can_choose_another_location(monkeypatch):
    _stub_empty_rate_page(monkeypatch)
    monkeypatch.setattr(dc_rates, "get_default_file_location", lambda: "D:\\default")
    prompts = []

    async def choose_other(page, *, action, default_location):
        prompts.append((action, default_location))
        return "choose"

    monkeypatch.setattr(dc_rates, "ask_location_choice", choose_other)
    monkeypatch.setattr(
        dc_rates,
        "export_schedule_records",
        lambda records, file_format, reference_mode=None: b"exported",
    )
    picker_calls = []

    class FakePicker:
        async def save_file(self, **kwargs):
            picker_calls.append(kwargs)
            return "picked.csv"

    monkeypatch.setattr(ft, "FilePicker", FakePicker)
    page = FakePage()
    root = dc_rates.build_dc_rates_page(page)

    asyncio.run(_export_button(root).on_click())
    page.dialogs[-1].actions[-1].on_click(None)
    asyncio.run(page.background_task)

    assert prompts[-1] == ("valuation schedule CSV export", "D:\\default")
    assert picker_calls[-1]["file_name"] == (
        f"valuation_schedules_{date.today().isoformat()}.csv"
    )
    assert picker_calls[-1]["src_bytes"] == b"exported"


def test_schedule_import_starts_in_the_default_folder(monkeypatch):
    _stub_empty_rate_page(monkeypatch)
    _mock_duplicate_profiles(monkeypatch)
    monkeypatch.setattr(dc_rates, "get_default_file_location", lambda: "D:\\default")

    async def choose_default(*_args, **_kwargs):
        return "default"

    monkeypatch.setattr(dc_rates, "ask_location_choice", choose_default)
    picker_calls = []

    class FakePicker:
        async def pick_files(self, **kwargs):
            picker_calls.append(kwargs)
            return []

    monkeypatch.setattr(ft, "FilePicker", FakePicker)
    page = FakePage()
    root = dc_rates.build_dc_rates_page(page)
    import_button = next(
        control for control in _walk(root)
        if isinstance(control, ft.OutlinedButton)
        and control.content == "Import CSV / Excel / ODS"
    )

    asyncio.run(import_button.on_click())

    assert picker_calls[-1]["initial_directory"] == "D:\\default"
    assert picker_calls[-1]["allowed_extensions"] == ["csv", "xlsx", "ods"]
    assert picker_calls[-1]["with_data"] is True
