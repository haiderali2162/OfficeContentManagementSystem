"""The valuation page control tree builds against the installed Flet API."""

import asyncio
import flet as ft
import pytest
from types import SimpleNamespace

from ui.pages import dc_rates
from ui.pages import valuation
from ui.app_shell import AppShell
from ui.navigation import NavigationItem


class FakePage:
    def __init__(self):
        self.services = []
        self.controls = []
        self.dialogs = []
        self.popped_dialogs = 0

    def update(self):
        return None

    def show_dialog(self, dialog):
        self.dialogs.append(dialog)
        return None

    def pop_dialog(self):
        self.popped_dialogs += 1


def walk_controls(control):
    """Walk nested Flet controls for focused UI interaction assertions."""
    yield control
    for child in getattr(control, "controls", ()) or ():
        yield from walk_controls(child)
    content = getattr(control, "content", None)
    if isinstance(content, ft.Control):
        yield from walk_controls(content)


@pytest.fixture(autouse=True)
def isolate_valuation_schedule_queries(monkeypatch):
    """Keep page tests independent from the live schedule database."""
    monkeypatch.setattr(valuation, "list_location_hint_values", lambda **_kwargs: {})
    monkeypatch.setattr(valuation, "find_schedule_location_matches", lambda **_kwargs: [])


def test_valuation_page_builds_with_tabs_and_existing_rate_management(monkeypatch):
    monkeypatch.setattr(valuation, "list_client_choices", lambda: [])
    monkeypatch.setattr(valuation, "list_tax_fee_rules", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_estimates", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_registry_fee_formulas", lambda: [])
    monkeypatch.setattr(valuation, "list_registry_fee_custom_fields", lambda: [])
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_kwargs: [])

    root = valuation.build_valuation_page(FakePage())

    assert isinstance(root, ft.Column)
    tabs = root.controls[2]
    assert isinstance(tabs, ft.Tabs)
    assert tabs.length == 3
    tab_bar = tabs.content.controls[0]
    assert [tab.label for tab in tab_bar.tabs] == ["Calculate", "Tax / fee rules", "DC / FBR rates"]


def test_dc_fbr_rates_load_is_scheduled_only_when_tab_is_selected(monkeypatch):
    class TaskPage(FakePage):
        def __init__(self):
            super().__init__()
            self.scheduled = []

        def run_task(self, handler):
            # Record scheduled handlers without running database work in this
            # synchronous control-tree test.
            self.scheduled.append(handler.__code__.co_firstlineno)
            handler().close()

    monkeypatch.setattr(valuation, "list_client_choices", lambda: [])
    monkeypatch.setattr(valuation, "list_tax_fee_rules", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_estimates", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_registry_fee_formulas", lambda: [])
    monkeypatch.setattr(valuation, "list_registry_fee_custom_fields", lambda: [])
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_kwargs: [])

    page = TaskPage()
    tab_states = []
    root = valuation.build_valuation_page(page, on_rates_tab_change=tab_states.append)
    tabs = root.controls[2]
    assert len(page.scheduled) == 1  # valuation choice/history loads only
    assert root.controls[0].visible is True
    assert root.controls[1].visible is True

    tabs.on_change(SimpleNamespace(control=SimpleNamespace(selected_index=2)))

    assert len(page.scheduled) == 2  # DC/FBR load is scheduled on demand
    assert tab_states == [True]
    assert root.controls[0].visible is False
    assert root.controls[1].visible is False

    tabs.on_change(SimpleNamespace(control=SimpleNamespace(selected_index=0)))
    assert tab_states == [True, False]
    assert root.controls[0].visible is True
    assert root.controls[1].visible is True


def test_app_shell_hides_shared_header_only_on_rates_tab(monkeypatch):
    monkeypatch.setattr(valuation, "list_client_choices", lambda: [])
    monkeypatch.setattr(valuation, "list_tax_fee_rules", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_estimates", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_registry_fee_formulas", lambda: [])
    monkeypatch.setattr(valuation, "list_registry_fee_custom_fields", lambda: [])
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_kwargs: [])

    page = FakePage()
    page.add = lambda control: page.controls.append(control)
    page.run_task = lambda handler: handler().close()
    shell = AppShell(page)
    shell.items = [NavigationItem(
        key="valuation", title="Valuation & Taxes", description="Valuation workspace",
        icon=None, page_builder=valuation.build_valuation_page,
    )]
    shell.render()

    layout = page.controls[0]
    main_column = layout.controls[1].content
    shared_header = main_column.controls[0]
    valuation_root = main_column.controls[1].content
    tabs = valuation_root.controls[2]

    tabs.on_change(SimpleNamespace(control=SimpleNamespace(selected_index=2)))
    assert shared_header.visible is False
    assert valuation_root.controls[0].visible is False

    tabs.on_change(SimpleNamespace(control=SimpleNamespace(selected_index=1)))
    assert shared_header.visible is True
    assert valuation_root.controls[0].visible is True


def test_add_tax_rule_opens_dialog_with_supported_flet_controls(monkeypatch):
    monkeypatch.setattr(valuation, "list_client_choices", lambda: [])
    monkeypatch.setattr(valuation, "list_tax_fee_rules", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_estimates", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_registry_fee_formulas", lambda: [])
    monkeypatch.setattr(valuation, "list_registry_fee_custom_fields", lambda: [])
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_kwargs: [])

    page = FakePage()
    root = valuation.build_valuation_page(page)
    tabs = root.controls[2]
    rule_page = tabs.content.controls[1].controls[1]
    add_rule = rule_page.controls[2].controls[0]

    add_rule.on_click(SimpleNamespace(control=add_rule))

    assert len(page.dialogs) == 1
    assert isinstance(page.dialogs[0], ft.AlertDialog)
    assert page.dialogs[0].title.value == "Add tax/fee rule"


def test_saving_tax_rule_hides_metadata_clears_form_and_shows_snackbar(monkeypatch):
    monkeypatch.setattr(valuation, "list_client_choices", lambda: [])
    monkeypatch.setattr(valuation, "list_tax_fee_rules", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_estimates", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_registry_fee_formulas", lambda: [])
    monkeypatch.setattr(valuation, "list_registry_fee_custom_fields", lambda: [])
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_kwargs: [])
    saved = []
    monkeypatch.setattr(valuation, "create_tax_fee_rule", lambda **values: saved.append(values) or 42)

    page = FakePage()
    root = valuation.build_valuation_page(page)
    rule_page = root.controls[2].content.controls[1].controls[1]
    add_rule = rule_page.controls[2].controls[0]
    add_rule.on_click(SimpleNamespace(control=add_rule))
    form_dialog = page.dialogs[0]
    form_controls = list(walk_controls(form_dialog.content))
    labels = [control.label for control in form_controls if isinstance(control, ft.TextField)]
    assert not any(label in {"Source reference", "Source date", "Effective from",
                             "Effective to (optional)", "Verified by"} for label in labels)
    assert not any(isinstance(control, ft.Dropdown) and control.label == "Verification"
                   for control in form_controls)

    fields = {control.label: control for control in form_controls if isinstance(control, ft.TextField)}
    fields["Rule name"].value = "Example stamp"
    fields["Category"].value = "stamp"
    fields["Rate / percent"].value = "2"
    save_button = form_dialog.actions[-1]
    save_button.on_click(SimpleNamespace(control=save_button))

    assert saved[0]["verification_status"] == "unverified"
    assert saved[0]["effective_from"] is not None
    assert fields["Rule name"].value == ""
    assert fields["Category"].value == ""
    assert fields["Rate / percent"].value == ""
    assert page.popped_dialogs == 0  # Add dialog remains ready for the next rule.
    assert isinstance(page.dialogs[-1], ft.SnackBar)


def test_square_and_kila_suggestions_follow_selected_mauza_and_square(monkeypatch):
    location_row = {
        "id": 7, "name": "Chak 1", "district": "District A", "tehsil": "Tehsil A",
        "land_type": "rural", "mauza": "Chak 1", "locality": "", "square_no": "15, 16",
        "kila_no": "1, 2", "committee_town": "", "revenue_circle": "",
        "property_area": "", "khasra_no": "",
    }
    rates = {
        "dc": [{"id": 11, "location_id": 7, "schedule_type": "dc", "rate_unit": "per_kanal",
                "rate_value": "100", "classification": "Residential", "property_type": "Residential",
                "mauza": "Chak 1", "effective_from": valuation.date(2020, 1, 1), "effective_to": None}],
        "fbr": [],
    }
    monkeypatch.setattr(valuation, "find_schedule_location_matches", lambda **_: [location_row])
    monkeypatch.setattr(valuation, "list_location_hint_values", lambda **_: {
        "district": ["District A"], "tehsil": ["Tehsil A"], "mauza": ["Chak 1"],
        "locality": [], "square_no": ["15", "16"], "kila_no": ["1", "2"],
        "committee_town": [], "revenue_circle": [], "property_area": [], "khasra_no": [],
    })
    monkeypatch.setattr(valuation, "list_client_choices", lambda: [])
    monkeypatch.setattr(valuation, "list_tax_fee_rules", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_estimates", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_registry_fee_formulas", lambda: [])
    monkeypatch.setattr(valuation, "list_registry_fee_custom_fields", lambda: [])
    monkeypatch.setattr(valuation, "list_dc_rates", lambda *, location_ids=None, schedule_type, **_kwargs: rates[schedule_type])
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_kwargs: [])

    class LoadingPage(FakePage):
        def run_task(self, handler):
            asyncio.run(handler())

    root = valuation.build_valuation_page(LoadingPage())
    controls = list(walk_controls(root))
    # Use control metadata so the test exercises the same handlers as user input.
    square = next(control for control in controls if isinstance(control, ft.AutoComplete) and control.data == "square_no")
    kila = next(control for control in controls if isinstance(control, ft.AutoComplete) and control.data == "kila_no")
    mauza = next(control for control in controls if isinstance(control, ft.AutoComplete) and control.data == "mauza")
    district = next(control for control in controls if isinstance(control, ft.AutoComplete) and control.data == "district")
    tehsil = next(control for control in controls if isinstance(control, ft.AutoComplete) and control.data == "tehsil")
    district.value, tehsil.value = "District A", "Tehsil A"
    mauza.value = "Chak 1"
    mauza.on_change(SimpleNamespace(control=mauza))

    square_hints = {suggestion.value for suggestion in square.suggestions}
    assert {"15", "16"}.issubset(square_hints)
    unit = next(control for control in controls if isinstance(control, ft.Dropdown)
                and control.label == "DC rate unit")
    rate = next(control for control in controls if isinstance(control, ft.Dropdown)
                and control.label == "Matched DC rate")
    assert [option.key for option in unit.options] == ["per_kanal"]
    assert len(rate.options) == 1

    square.value = "15"
    square.on_change(SimpleNamespace(control=square))
    assert {suggestion.value for suggestion in kila.suggestions} >= {"1", "2"}


def test_valuation_mauza_hints_query_current_database(monkeypatch):
    hint_calls = []

    def current_hints(**kwargs):
        hint_calls.append(kwargs)
        return {"mauza": ["Newly Imported Mauza"]}

    monkeypatch.setattr(valuation, "list_location_hint_values", current_hints)
    monkeypatch.setattr(valuation, "list_client_choices", lambda: [])
    monkeypatch.setattr(valuation, "list_tax_fee_rules", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_estimates", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_registry_fee_formulas", lambda: [])
    monkeypatch.setattr(valuation, "list_registry_fee_custom_fields", lambda: [])
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_kwargs: [])

    class LoadingPage(FakePage):
        def run_task(self, handler):
            asyncio.run(handler())

    root = valuation.build_valuation_page(LoadingPage())
    mauza = next(
        control for control in walk_controls(root)
        if isinstance(control, ft.AutoComplete) and control.data == "mauza"
    )
    mauza.value = "New"
    mauza.on_change(SimpleNamespace(control=mauza))

    assert hint_calls[-1]["context"]["mauza"] == "New"
    assert {suggestion.value for suggestion in mauza.suggestions} == {"Newly Imported Mauza"}


def test_empty_rule_list_collapses_and_grows_only_for_saved_rules(monkeypatch):
    rule_rows = []
    monkeypatch.setattr(valuation, "list_client_choices", lambda: [])
    monkeypatch.setattr(valuation, "list_tax_fee_rules", lambda **_kwargs: rule_rows)
    monkeypatch.setattr(valuation, "list_estimates", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_registry_fee_formulas", lambda: [])
    monkeypatch.setattr(valuation, "list_registry_fee_custom_fields", lambda: [])
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_kwargs: [])

    class LoadingPage(FakePage):
        def run_task(self, handler):
            asyncio.run(handler())

    root = valuation.build_valuation_page(LoadingPage())
    controls = list(walk_controls(root))
    rules_list = next(control for control in controls
                      if isinstance(control, ft.ListView) and control.data == "tax_fee_rules")
    empty_state = next(control for control in controls
                       if isinstance(control, ft.Text) and control.value == "No tax or fee rules saved yet.")
    assert rules_list.visible is False
    assert empty_state.visible is True

    rule_rows.append({
        "id": 1, "name": "Example", "jurisdiction": "Punjab", "category": "fee",
        "calculation_method": "fixed_amount", "fixed_amount": "100", "currency_code": "PKR",
        "effective_from": valuation.date.today(), "effective_to": None,
        "verification_status": "unverified",
    })
    rule_page = root.controls[2].content.controls[1].controls[1]
    refresh_button = rule_page.controls[2].controls[1]
    refresh_button.on_click(SimpleNamespace(control=refresh_button))
    assert rules_list.visible is True
    assert rules_list.height == 112
    assert empty_state.visible is False

