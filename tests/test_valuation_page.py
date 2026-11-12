"""The valuation page control tree builds against the installed Flet API."""

import asyncio
import flet as ft
import json
import pytest
from decimal import Decimal
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
    # DataTables keep their content in rows/cells rather than `controls`.
    for row in getattr(control, "rows", ()) or ():
        for cell in getattr(row, "cells", ()) or ():
            yield from walk_controls(cell)


def _stub_control_updates(root):
    """Neutralize per-control update calls in the fake (unmounted) tree.

    Flet's ``Control.update()`` requires a mounted page; the fake page in these
    tests never mounts controls, so result panels and other containers that
    refresh themselves via ``control.update()`` would raise. Page-level
    ``page.update()`` calls already go through the FakePage no-op.
    """
    for control in walk_controls(root):
        if isinstance(control, ft.Control):
            control.update = lambda: None


@pytest.fixture(autouse=True)
def isolate_valuation_schedule_queries(monkeypatch):
    """Keep page tests independent from the live schedule database."""
    monkeypatch.setattr(valuation, "list_location_hint_values", lambda **_kwargs: {})
    monkeypatch.setattr(valuation, "find_schedule_location_matches", lambda **_kwargs: [])
    # The location draft is module-level cache shared by page rebuilds; clear
    # it around every test so restored values never leak between tests.
    valuation._LOCATION_DRAFT.clear()
    yield
    valuation._LOCATION_DRAFT.clear()


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


def test_hybrid_tax_rule_method_reveal_field_and_save(monkeypatch):
    """Choosing "Fixed or percentage" reveals rate, fixed, and threshold."""
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
    fields = {control.label: control for control in form_controls if isinstance(control, ft.TextField)}
    method = next(control for control in form_controls
                  if isinstance(control, ft.Dropdown) and control.label == "Calculation method")

    # The dialog supports all four calculation methods.
    option_values = [option.key for option in method.options]
    assert option_values == ["percentage", "fixed_amount", "per_unit", "fixed_or_percentage"]

    # On the default percentage method the threshold is hidden and cleared.
    threshold_label = "Use fixed below this value (% above)"
    threshold = fields[threshold_label]
    assert threshold.visible is False
    assert fields["Rate / percent"].visible is True

    # Selecting the hybrid method reveals all three interlocking fields.
    method.value = "fixed_or_percentage"
    method.on_select()  # same handler Flet invokes for a dropdown selection
    assert threshold.visible is True
    assert fields["Rate / percent"].visible is True
    assert fields["Fixed amount"].visible is True
    assert threshold.visible is True

    fields["Rule name"].value = "Hybrid example"
    fields["Category"].value = "hybrid"
    fields["Rate / percent"].value = "2"
    fields["Fixed amount"].value = "2000"
    threshold.value = "50000"
    save_button = form_dialog.actions[-1]
    save_button.on_click(SimpleNamespace(control=save_button))

    assert saved[0]["calculation_method"] == "fixed_or_percentage"
    assert saved[0]["percentage_threshold"] == "50000"
    assert saved[0]["fixed_amount"] == "2000"
    assert saved[0]["rate_value"] == "2"


def test_hybrid_rule_row_renders_fixed_and_percent_halves(monkeypatch):
    """The rules list spells out the hybrid rule so users can audit it."""
    rule_rows = [{
        "id": 1, "name": "Hybrid example", "jurisdiction": "Punjab",
        "category": "hybrid", "calculation_method": "fixed_or_percentage",
        "rate_value": "2", "fixed_amount": "2000", "percentage_threshold": "50000",
        "rate_unit": None, "currency_code": "PKR",
        "effective_from": valuation.date.today(), "effective_to": None,
        "verification_status": "unverified",
    }]
    monkeypatch.setattr(valuation, "list_client_choices", lambda: [])
    monkeypatch.setattr(valuation, "list_tax_fee_rules", lambda **_kwargs: list(rule_rows))
    monkeypatch.setattr(valuation, "list_estimates", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_registry_fee_formulas", lambda: [])
    monkeypatch.setattr(valuation, "list_registry_fee_custom_fields", lambda: [])
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_kwargs: [])

    class LoadingPage(FakePage):
        def run_task(self, handler):
            asyncio.run(handler())

    page = LoadingPage()
    root = valuation.build_valuation_page(page)
    controls = list(walk_controls(root))
    texts = [control.value for control in controls if isinstance(control, ft.Text)]
    assert any("Fixed or percentage" in value for value in texts)
    assert any("PKR 2,000.00" in value and "50000" in value and "2%" in value
               for value in texts)


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


def _rule_record(**overrides):
    record = {
        "id": 1, "name": "Example stamp", "jurisdiction": "Punjab",
        "category": "stamp", "calculation_method": "percentage",
        "rate_value": "2", "fixed_amount": None, "percentage_threshold": None,
        "rate_unit": None, "currency_code": "PKR",
        "effective_from": valuation.date.today(), "effective_to": None,
        "verification_status": "unverified",
    }
    record.update(overrides)
    return record


def test_remove_rule_asks_first_then_calls_delete_service(monkeypatch):
    """List rows offer Remove behind a confirmation; nothing is deleted silently."""
    rule_rows = [_rule_record()]
    monkeypatch.setattr(valuation, "list_client_choices", lambda: [])
    monkeypatch.setattr(valuation, "list_tax_fee_rules", lambda **_kwargs: list(rule_rows))
    monkeypatch.setattr(valuation, "list_estimates", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_registry_fee_formulas", lambda: [])
    monkeypatch.setattr(valuation, "list_registry_fee_custom_fields", lambda: [])
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_kwargs: [])
    deleted = []
    monkeypatch.setattr(valuation, "delete_tax_fee_rule", lambda rule_id: deleted.append(rule_id))

    class LoadingPage(FakePage):
        def run_task(self, handler):
            asyncio.run(handler())

    page = LoadingPage()
    root = valuation.build_valuation_page(page)
    controls = list(walk_controls(root))
    remove_button = next(control for control in controls
                         if isinstance(control, ft.TextButton) and control.content == "Remove")
    assert not deleted, "nothing may be removed before confirmation"

    remove_button.on_click(SimpleNamespace(control=remove_button))
    assert len(page.dialogs) == 1
    confirm = page.dialogs[0]
    assert isinstance(confirm, ft.AlertDialog)
    assert confirm.title.value == "Remove tax/fee rule?"
    assert not deleted, "the confirmation dialog alone must not delete anything"

    # The Confirm action is the last dialog action ("Remove").
    confirm.actions[-1].on_click(None)
    assert deleted == [1]
    texts = [control.value for control in walk_controls(root)
             if isinstance(control, ft.Text)]
    assert any("Removed rule 'Example stamp'" in value for value in texts)


def test_236_preset_asks_filer_case_and_saves_condition(monkeypatch):
    """236C/236K first choose the filer case, then fill name/category/condition."""
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

    preset_236c = next(control for control in walk_controls(form_dialog.content)
                       if isinstance(control, ft.OutlinedButton) and control.content == "236C")
    preset_236c.on_click(SimpleNamespace(control=preset_236c))
    # The chooser is a separate dialog above the rule form.
    chooser = page.dialogs[-1]
    assert chooser.title.value == "236C case rate"
    case_buttons = [control for control in walk_controls(chooser.content)
                    if isinstance(control, ft.OutlinedButton)]
    assert [button.content for button in case_buttons] == [
        "Active filer", "In-active case", "Late filer (previous years)",
    ]

    case_buttons[1].on_click(SimpleNamespace(control=case_buttons[1]))
    assert page.popped_dialogs == 1  # Only the chooser closed.

    form_controls = list(walk_controls(form_dialog.content))
    fields = {control.label: control for control in form_controls if isinstance(control, ft.TextField)}
    assert fields["Rule name"].value == "236C (In-active case)"
    assert fields["Category"].value == "236c"

    save_button = form_dialog.actions[-1]
    save_button.on_click(SimpleNamespace(control=save_button))
    conditions = json.loads(saved[0]["eligibility_conditions_json"])
    assert conditions["when"]["seller_filer_status"] == "non_ATL"
    assert conditions["payer"] == "seller"
    assert saved[0]["name"] == "236C (In-active case)"
    assert saved[0]["category"] == "236c"

    # Also verify 236K (buyer tax)
    preset_236k = next(control for control in walk_controls(form_dialog.content)
                       if isinstance(control, ft.OutlinedButton) and control.content == "236K")
    preset_236k.on_click(SimpleNamespace(control=preset_236k))
    chooser_236k = page.dialogs[-1]
    assert chooser_236k.title.value == "236K case rate"
    k_buttons = [control for control in walk_controls(chooser_236k.content)
                 if isinstance(control, ft.OutlinedButton)]
    k_buttons[0].on_click(SimpleNamespace(control=k_buttons[0]))
    assert fields["Rule name"].value == "236K (Active filer)"
    assert fields["Category"].value == "236k"
    save_button.on_click(SimpleNamespace(control=save_button))
    k_conditions = json.loads(saved[1]["eligibility_conditions_json"])
    assert k_conditions["when"]["buyer_filer_status"] == "ATL"
    assert k_conditions["payer"] == "buyer"


def test_remove_rule_from_edit_dialog_asks_first_then_deletes(monkeypatch):
    """Edit dialog has a Remove rule action which confirms and deletes."""
    rule_rows = [_rule_record()]
    monkeypatch.setattr(valuation, "list_client_choices", lambda: [])
    monkeypatch.setattr(valuation, "list_tax_fee_rules", lambda **_kwargs: list(rule_rows))
    monkeypatch.setattr(valuation, "list_estimates", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_registry_fee_formulas", lambda: [])
    monkeypatch.setattr(valuation, "list_registry_fee_custom_fields", lambda: [])
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_kwargs: [])
    deleted = []
    monkeypatch.setattr(valuation, "delete_tax_fee_rule", lambda rule_id: deleted.append(rule_id))

    class LoadingPage(FakePage):
        def run_task(self, handler):
            asyncio.run(handler())

    page = LoadingPage()
    root = valuation.build_valuation_page(page)
    controls = list(walk_controls(root))
    edit_button = next(control for control in controls
                       if isinstance(control, ft.TextButton) and control.content == "Edit")
    edit_button.on_click(SimpleNamespace(control=edit_button))
    edit_dialog = page.dialogs[0]
    remove_action = next(control for control in edit_dialog.actions
                         if isinstance(control, ft.TextButton) and control.content == "Remove rule")
    remove_action.on_click(SimpleNamespace(control=remove_action))
    confirm_dialog = page.dialogs[-1]
    assert confirm_dialog.title.value == "Remove tax/fee rule?"
    confirm_dialog.actions[-1].on_click(None)
    assert deleted == [1]


def test_calculate_tab_offers_qanoongoi_location_filter(monkeypatch):
    """Rural schedule search accepts an optional Qanoongoi level like the DC page."""
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
    controls = list(walk_controls(root))
    qanoongoi = next(control for control in controls
                     if isinstance(control, ft.AutoComplete) and control.data == "qanoongoi")
    district = next(control for control in controls
                    if isinstance(control, ft.AutoComplete) and control.data == "district")
    assert district.visible is True  # Rural default: qanoongoi sits beside district.

    # Typing a Qanoongoi value feeds the same schedule-backed hint pipeline
    # used by the other location fields, so both filters narrow Mauza results.
    hint_calls = []

    def current_hints(**kwargs):
        hint_calls.append(kwargs)
        return {"qanoongoi": ["Qanoongoi A"], "mauza": ["Chak 1"]}

    monkeypatch.setattr(valuation, "list_location_hint_values", current_hints)
    qanoongoi.value = "Qanoong"
    qanoongoi.on_change(SimpleNamespace(control=qanoongoi))
    assert hint_calls[-1]["context"]["qanoongoi"] == "Qanoong"
    assert "qanoongoi" in hint_calls[-1]["fields"]
    assert {suggestion.value for suggestion in qanoongoi.suggestions} == {"Qanoongoi A"}


def test_remove_formula_asks_first_then_deletes(monkeypatch):
    """Formula row Remove button confirms before deleting."""
    formula_row = {
        "id": 99, "name": "Custom stamp", "category": "stamp",
        "formula_expression": "area * 10", "verification_status": "unverified",
    }
    monkeypatch.setattr(valuation, "list_client_choices", lambda: [])
    monkeypatch.setattr(valuation, "list_tax_fee_rules", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_estimates", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_registry_fee_formulas", lambda: [formula_row])
    monkeypatch.setattr(valuation, "list_registry_fee_custom_fields", lambda: [])
    monkeypatch.setattr(dc_rates, "list_location_picker", lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_kwargs: [])
    deleted = []
    monkeypatch.setattr(valuation, "delete_registry_fee_formula", lambda f_id: deleted.append(f_id))

    class LoadingPage(FakePage):
        def run_task(self, handler):
            asyncio.run(handler())

    page = LoadingPage()
    root = valuation.build_valuation_page(page)
    # The formula row is on Tab 1 (Tax / fee rules)
    rule_page = root.controls[2].content.controls[1].controls[1]
    formula_list_ctrl = rule_page.controls[9]
    remove_btn = next(c for c in walk_controls(formula_list_ctrl)
                      if isinstance(c, ft.TextButton) and c.content == "Remove")
    remove_btn.on_click(SimpleNamespace(control=remove_btn))
    confirm_dialog = page.dialogs[-1]
    assert confirm_dialog.title.value == "Remove registry formula?"
    confirm_dialog.actions[-1].on_click(None)
    assert deleted == [99]


def test_location_option_labels_show_square_kila_and_dedupe_village(monkeypatch):
    """Dropdown labels list the village once and append Square/Kila tokens."""
    def fake_matches(**_kwargs):
        return [
            {"id": 1, "name": "Chak 1", "mauza": "Chak 1", "district": "District A",
             "tehsil": "Tehsil A", "land_type": "rural", "locality": "",
             "square_no": "2", "kila_no": "12", "khasra_no": "",
             "committee_town": "", "revenue_circle": "", "property_area": ""},
            {"id": 2, "name": "Chak 1", "mauza": "Chak 1", "district": "District A",
             "tehsil": "Tehsil A", "land_type": "rural", "locality": "",
             "square_no": "2", "kila_no": "13", "khasra_no": "",
             "committee_town": "", "revenue_circle": "", "property_area": ""},
        ]

    monkeypatch.setattr(valuation, "find_schedule_location_matches", fake_matches)
    monkeypatch.setattr(valuation, "list_dc_rates", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_client_choices", lambda: [])
    monkeypatch.setattr(valuation, "list_tax_fee_rules", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_estimates", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_registry_fee_formulas", lambda: [])
    monkeypatch.setattr(valuation, "list_registry_fee_custom_fields", lambda: [])

    class LoadingPage(FakePage):
        def run_task(self, handler):
            asyncio.run(handler())

    root = valuation.build_valuation_page(LoadingPage())
    controls = list(walk_controls(root))
    mauza = next(control for control in controls
                 if isinstance(control, ft.AutoComplete) and control.data == "mauza")
    mauza.value = "Chak 1"
    mauza.on_change(SimpleNamespace(control=mauza))

    dropdown = next(control for control in walk_controls(root)
                    if isinstance(control, ft.Dropdown)
                    and control.label == "Choose matching schedule location")
    labels = [option.text for option in dropdown.options]
    assert len(labels) == 2
    assert all(label.count("Chak 1") == 1 for label in labels)
    assert labels[0] == "Chak 1 · District A · Tehsil A · Square 2 · Kila 12"
    assert labels[1] == "Chak 1 · District A · Tehsil A · Square 2 · Kila 13"


def test_location_search_parses_square_kila_pair_and_kila_letters(monkeypatch):
    """A typed "2/12" fills Square and Kila and re-runs the schedule match."""
    matcher_calls = []

    def fake_matches(**kwargs):
        matcher_calls.append(kwargs)
        return [{
            "id": 5, "name": "Chak 2", "mauza": "Chak 2", "district": "District A",
            "tehsil": "Tehsil A", "land_type": "rural", "locality": "",
            "square_no": "2", "kila_no": "12", "khasra_no": "",
            "committee_town": "", "revenue_circle": "", "property_area": "",
        }]

    monkeypatch.setattr(valuation, "find_schedule_location_matches", fake_matches)
    monkeypatch.setattr(valuation, "list_dc_rates", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_client_choices", lambda: [])
    monkeypatch.setattr(valuation, "list_tax_fee_rules", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_estimates", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_registry_fee_formulas", lambda: [])
    monkeypatch.setattr(valuation, "list_registry_fee_custom_fields", lambda: [])

    class LoadingPage(FakePage):
        def run_task(self, handler):
            asyncio.run(handler())

    root = valuation.build_valuation_page(LoadingPage())
    controls = list(walk_controls(root))
    mauza = next(control for control in controls
                 if isinstance(control, ft.AutoComplete) and control.data == "mauza")
    square = next(control for control in controls
                  if isinstance(control, ft.AutoComplete) and control.data == "square_no")
    kila = next(control for control in controls
                if isinstance(control, ft.AutoComplete) and control.data == "kila_no")
    search = next(control for control in walk_controls(root)
                  if isinstance(control, ft.TextField)
                  and control.label and "Square/Kila" in control.label)
    assert search is not None

    mauza.value = "Chak 2"
    mauza.on_change(SimpleNamespace(control=mauza))

    search.value = "2/12"
    search.on_change(SimpleNamespace(control=search))
    # Both cadastral inputs are filled from the typed pair, and the matcher
    # ran with Square and Kila set (Mauza + square + kila rural fetch).
    assert square.value == "2"
    assert kila.value == "12"
    assert matcher_calls[-1]["context"]["square_no"] == "2"
    assert matcher_calls[-1]["context"]["kila_no"] == "12"

    # Dash form and alphanumeric Kila values parse the same way.
    search.value = "3 - 3B"
    search.on_change(SimpleNamespace(control=search))
    assert square.value == "3"
    assert kila.value == "3B"

    # Clearing the search restores the unfiltered candidates.
    search.value = ""
    search.on_change(SimpleNamespace(control=search))
    assert square.value == ""
    assert kila.value == ""

def test_export_current_result_row_has_all_formats_and_guards_empty(monkeypatch):
    """CSV/Excel/PDF/ODS buttons exist; exporting before calculating shows a hint."""
    monkeypatch.setattr(valuation, "list_client_choices", lambda: [])
    monkeypatch.setattr(valuation, "list_tax_fee_rules", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_estimates", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_registry_fee_formulas", lambda: [])
    monkeypatch.setattr(valuation, "list_registry_fee_custom_fields", lambda: [])

    class LoadingPage(FakePage):
        def run_task(self, handler):
            asyncio.run(handler())

    root = valuation.build_valuation_page(LoadingPage())
    buttons = {control.content: control for control in walk_controls(root)
               if isinstance(control, ft.OutlinedButton)}
    for label in ("CSV", "Excel", "PDF", "ODS"):
        assert label in buttons, f"Missing current-result export button: {label}"

    # No calculation yet: the CSV button must show the guidance message and
    # never touch the report service.
    asyncio.run(buttons["CSV"].on_click(None))
    messages = [control.value for control in walk_controls(root)
                if isinstance(control, ft.Text)
                and control.value == "Calculate an estimate before exporting it."]
    assert messages, "Empty-calculation export guard message was not shown."




def _patch_estimate_services(monkeypatch, *, matches=None, rates=None):
    """Patch the schedule services the calculator tab queries during tests."""
    monkeypatch.setattr(valuation, "list_client_choices", lambda: [])
    monkeypatch.setattr(valuation, "list_tax_fee_rules", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_estimates", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_registry_fee_formulas", lambda: [])
    monkeypatch.setattr(valuation, "list_registry_fee_custom_fields", lambda: [])
    # The dimension preload reads the real schedule database; tests patch it
    # out so page builds stay instant and never touch live data.
    monkeypatch.setattr(valuation, "preload_location_dimensions", lambda: None)
    monkeypatch.setattr(dc_rates, "list_location_picker",
                        lambda **_: {"locations": [], "qanoongoi": [], "has_more": False})
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_kwargs: [])
    if matches is not None:
        monkeypatch.setattr(valuation, "find_schedule_location_matches",
                            lambda **_kwargs: matches)
    if rates is not None:
        monkeypatch.setattr(valuation, "list_dc_rates",
                            lambda *, schedule_type, **_kwargs: list(rates.get(schedule_type, [])))


def _location_row(row_id, **overrides):
    row = {
        "id": row_id, "name": "Chak 1", "district": "District A", "tehsil": "Tehsil A",
        "land_type": "rural", "mauza": "Chak 1", "locality": "", "square_no": "",
        "kila_no": "", "committee_town": "", "revenue_circle": "",
        "property_area": "", "khasra_no": "", "qanoongoi": "",
    }
    row.update(overrides)
    return row


def _dc_rate(rate_id, location_id, classification="Residential"):
    return {
        "id": rate_id, "location_id": location_id, "schedule_type": "dc",
        "rate_unit": "per_kanal", "rate_value": "100",
        "classification": classification, "property_type": classification,
        "mauza": "Chak 1", "effective_from": valuation.date(2020, 1, 1),
        "effective_to": None,
    }


def _auto(controls, data):
    return next(control for control in controls
                if isinstance(control, ft.AutoComplete) and control.data == data)


def _dropdown(controls, label):
    return next(control for control in controls
                if isinstance(control, ft.Dropdown) and control.label == label)


def _schedule_display_radio(controls):
    return next(control for control in controls
                if isinstance(control, ft.RadioGroup)
                and any(isinstance(radio, ft.Radio) and radio.value == "individual"
                        for radio in walk_controls(control)))


def _form_error(root):
    return next(control for control in walk_controls(root)
                if isinstance(control, ft.Text) and getattr(control, "color", "") == "#B91C1C")


class LoadingPage(FakePage):
    def run_task(self, handler):
        asyncio.run(handler())


def test_district_tehsil_qanoongoi_changes_clear_children_and_hints_use_hierarchy(monkeypatch):
    hint_calls = []

    def record_hints(**kwargs):
        hint_calls.append(kwargs)
        return {}

    monkeypatch.setattr(valuation, "list_location_hint_values", record_hints)
    _patch_estimate_services(monkeypatch)

    root = valuation.build_valuation_page(LoadingPage())
    controls = list(walk_controls(root))
    district = _auto(controls, "district")
    tehsil = _auto(controls, "tehsil")
    qanoongoi = _auto(controls, "qanoongoi")
    mauza = _auto(controls, "mauza")
    square = _auto(controls, "square_no")
    kila = _auto(controls, "kila_no")
    location_search = next(control for control in controls
                           if isinstance(control, ft.TextField)
                           and (control.label or "").startswith("Search by Square/Kila"))

    tehsil.value = "Tehsil A"
    qanoongoi.value = "Q A"
    mauza.value = "Chak 1"
    square.value = "15"
    kila.value = "2"
    location_search.value = "15/2"

    # Selecting a District resets every level below it, so stale child values
    # cannot filter the next level's schedule lookups.
    district.value = "District A"
    district.on_select(SimpleNamespace(control=district))
    assert tehsil.value == ""
    assert qanoongoi.value == ""
    assert mauza.value == ""
    assert square.value == ""
    assert kila.value == ""
    assert location_search.value == ""
    assert hint_calls and hint_calls[-1]["hierarchy"] is True
    context = hint_calls[-1]["context"]
    assert context["district"] == "District A"
    assert context["mauza"] == ""
    assert context["square_no"] == ""

    # Tehsil and Qanoongoi selections also reset the levels below them.
    mauza.value = "Chak 1"
    square.value = "15"
    tehsil.on_select(SimpleNamespace(control=tehsil))
    assert mauza.value == "" and square.value == "" and kila.value == ""
    mauza.value = "Chak 1"
    qanoongoi.on_select(SimpleNamespace(control=qanoongoi))
    assert mauza.value == "" and square.value == "" and kila.value == ""


def test_location_draft_is_restored_after_page_rebuild(monkeypatch):
    _patch_estimate_services(monkeypatch)

    first = valuation.build_valuation_page(LoadingPage())
    controls = list(walk_controls(first))
    district = _auto(controls, "district")
    mauza = _auto(controls, "mauza")
    square = _auto(controls, "square_no")
    district.value = "District A"
    mauza.value = "Chak 1"
    mauza.on_change(SimpleNamespace(control=mauza))
    square.value = "15"
    square.on_change(SimpleNamespace(control=square))

    # Navigating away and back rebuilds the page from scratch; the draft must
    # bring the typed location values back.
    second = valuation.build_valuation_page(LoadingPage())
    controls = list(walk_controls(second))
    assert _auto(controls, "district").value == "District A"
    assert _auto(controls, "mauza").value == "Chak 1"
    assert _auto(controls, "square_no").value == "15"


def test_schedule_display_radio_expands_pairs_into_individual_choices(monkeypatch):
    matches = [_location_row(7, square_no="1, 2-3", kila_no="1, 2")]
    rates = {"dc": [_dc_rate(11, 7)]}
    _patch_estimate_services(monkeypatch, matches=matches, rates=rates)

    root = valuation.build_valuation_page(LoadingPage())
    controls = list(walk_controls(root))
    mauza = _auto(controls, "mauza")
    mauza.value = "Chak 1"
    mauza.on_change(SimpleNamespace(control=mauza))

    location_match = _dropdown(controls, "Choose matching schedule location")
    radio = _schedule_display_radio(controls)
    assert radio.visible is True
    # Individual values are now the default display mode (developer request).
    assert [option.key for option in location_match.options] == [
        "7:1:1", "7:1:2", "7:2:1", "7:2:2", "7:3:1", "7:3:2",
    ]

    radio.value = "imported"
    radio.on_change(SimpleNamespace(control=radio))
    assert [option.key for option in location_match.options] == ["7"]
    assert location_match.value == "7"

    radio.value = "individual"
    radio.on_change(SimpleNamespace(control=radio))
    keys = [option.key for option in location_match.options]
    # Stored "1, 2-3" squares and "1, 2" kilas expand into one choice per
    # pair, following the import expansion rules. Stored records are not
    # rewritten; this is a display-only view.
    assert keys == ["7:1:1", "7:1:2", "7:2:1", "7:2:2", "7:3:1", "7:3:2"]
    texts = [getattr(option, "text", "") for option in location_match.options]
    assert any("Square 2" in text and "Kila 1" in text for text in texts)
    # The pair detail must not repeat the imported Square/Kila summary; each
    # option shows its own pair only ("Square 2 · Kila 1").
    pair_option = next(text for text in texts if "Square 2 · Kila 1" in text)
    assert "Square 1, 2-3" not in pair_option
    assert "Kila 1, 2" not in pair_option
    # The previously selected row id is no longer a valid pair key.
    assert location_match.value is None

    radio.value = "imported"
    radio.on_change(SimpleNamespace(control=radio))
    assert [option.key for option in location_match.options] == ["7"]


def test_individual_mode_narrows_to_typed_pair_and_autoselects_exact(monkeypatch):
    matches = [_location_row(7, square_no="1, 2-3", kila_no="1, 2")]
    rates = {"dc": [_dc_rate(11, 7)]}
    _patch_estimate_services(monkeypatch, matches=matches, rates=rates)

    root = valuation.build_valuation_page(LoadingPage())
    controls = list(walk_controls(root))
    mauza = _auto(controls, "mauza")
    mauza.value = "Chak 1"
    mauza.on_change(SimpleNamespace(control=mauza))
    radio = _schedule_display_radio(controls)
    radio.value = "individual"
    radio.on_change(SimpleNamespace(control=radio))

    location_match = _dropdown(controls, "Choose matching schedule location")
    square = _auto(controls, "square_no")
    kila = _auto(controls, "kila_no")
    square.value = "2"
    kila.value = "1"
    location_match.on_change(SimpleNamespace(control=location_match))
    # Only the expanded pair covering the typed Square and Kila remains, and
    # the single exact schedule is selected automatically.
    assert [option.key for option in location_match.options] == ["7:2:1"]
    assert location_match.value == "7:2:1"


def test_imported_mode_ranks_exact_pair_first_and_autoselects_single_exact(monkeypatch):
    matches = [
        _location_row(7, square_no="2, 5", kila_no="1, 2"),
        _location_row(8, name="Chak 2", mauza="Chak 2", square_no="2", kila_no="1"),
    ]
    rates = {"dc": [_dc_rate(11, 7), _dc_rate(12, 8)]}
    _patch_estimate_services(monkeypatch, matches=matches, rates=rates)

    root = valuation.build_valuation_page(LoadingPage())
    controls = list(walk_controls(root))
    # The ranking assertions below describe the imported view; individual
    # values are now the default, so switch explicitly.
    radio = _schedule_display_radio(controls)
    radio.value = "imported"
    radio.on_change(SimpleNamespace(control=radio))
    mauza = _auto(controls, "mauza")
    mauza.value = "Chak"
    mauza.on_change(SimpleNamespace(control=mauza))
    location_match = _dropdown(controls, "Choose matching schedule location")
    assert [option.key for option in location_match.options] == ["7", "8"]

    square = _auto(controls, "square_no")
    kila = _auto(controls, "kila_no")
    square.value = "2"
    kila.value = "1"
    location_match.on_change(SimpleNamespace(control=location_match))
    keys = [option.key for option in location_match.options]
    # Row 8 holds exactly the typed pair (one Square, one Kila) and is ranked
    # first; with a single exact match it is selected automatically.
    assert keys == ["8", "7"]
    assert location_match.value == "8"


def test_calculate_errors_name_the_visible_controls(monkeypatch):
    class NS(SimpleNamespace):
        pass

    def build():
        root = valuation.build_valuation_page(LoadingPage())
        controls = list(walk_controls(root))
        button = next(control for control in controls
                      if isinstance(control, ft.Button)
                      and getattr(control, "content", None) == "Calculate estimate")
        return root, controls, button

    # No location typed at all: the message points at the location section.
    _patch_estimate_services(monkeypatch)
    root, controls, button = build()
    button.on_click(SimpleNamespace(control=button))
    message = _form_error(root).value
    assert "Choose a location first" in message
    assert "Property Profile & Location" in message

    # Location matched but no active DC rate: the message says so instead of
    # asking for hidden fields.
    matches = [_location_row(7)]
    _patch_estimate_services(monkeypatch, matches=matches, rates={"dc": []})
    root, controls, button = build()
    mauza = _auto(controls, "mauza")
    mauza.value = "Chak 1"
    mauza.on_change(SimpleNamespace(control=mauza))
    button.on_click(SimpleNamespace(control=button))
    message = _form_error(root).value
    assert "no active DC rate" in message

    # One location, several DC rates: the first rate is preselected
    # automatically now (developer rule: if more than one unit/rate appears,
    # select any one of them), so the calculation no longer stalls asking
    # for a rate.
    matches = [_location_row(7)]
    rates = {"dc": [_dc_rate(11, 7), _dc_rate(12, 7)]}
    _patch_estimate_services(monkeypatch, matches=matches, rates=rates)
    root, controls, button = build()
    mauza = _auto(controls, "mauza")
    mauza.value = "Chak 1"
    mauza.on_change(SimpleNamespace(control=mauza))
    rate = _dropdown(controls, "Matched DC rate")
    assert rate.value == "11"
    button.on_click(SimpleNamespace(control=button))
    message = _form_error(root).value
    assert "Matched DC rate" not in message

    # Several locations, nothing chosen: the message names the location
    # dropdown.
    matches = [_location_row(7), _location_row(8, name="Chak 2", mauza="Chak 2")]
    rates = {"dc": [_dc_rate(11, 7), _dc_rate(12, 8)]}
    _patch_estimate_services(monkeypatch, matches=matches, rates=rates)
    root, controls, button = build()
    mauza = _auto(controls, "mauza")
    mauza.value = "Chak"
    mauza.on_change(SimpleNamespace(control=mauza))
    button.on_click(SimpleNamespace(control=button))
    message = _form_error(root).value
    assert "Choose matching schedule location" in message


def test_dropdown_selections_wired_to_on_select_handler(monkeypatch):
    """Flet 1.0.4 Dropdown fires on_select when an option is picked.

    Dropdown has no on_change event in this installed version, so the
    classification, rate-unit and schedule-location dropdowns must have
    on_select wired, otherwise picking an option silently does nothing in
    the running app (regression from the live verification).
    """
    _patch_estimate_services(monkeypatch)

    root = valuation.build_valuation_page(LoadingPage())
    controls = list(walk_controls(root))
    for label in ("Classification (optional)", "DC rate unit",
                  "FBR rate unit (if available)",
                  "Choose matching schedule location"):
        dropdown = _dropdown(controls, label)
        assert dropdown.on_select is not None, label
        assert dropdown.on_change is not None, label
        # Both events point at the same refresh handler.
        assert dropdown.on_select == dropdown.on_change, label

    # The rate-choice dropdowns outside the wiring loop keep their own
    # on_select handlers too.
    dc_rate_choice = _dropdown(controls, "Matched DC rate")
    assert dc_rate_choice.on_select is not None


def _party_role_dropdowns(controls):
    return [control for control in controls
            if isinstance(control, ft.Dropdown) and control.label == "Role"]


def test_deed_parties_start_with_buyer_and_seller_and_support_add_remove(monkeypatch):
    _patch_estimate_services(monkeypatch)

    root = valuation.build_valuation_page(LoadingPage())
    controls = list(walk_controls(root))
    assert len(_party_role_dropdowns(controls)) == 2

    # Add a third party (extra buyer) via the + Add party button.
    add_button = next(control for control in controls
                      if isinstance(control, ft.OutlinedButton)
                      and getattr(control, "content", None) == "+ Add party")
    add_button.on_click(SimpleNamespace(control=add_button))
    controls = list(walk_controls(root))
    assert len(_party_role_dropdowns(controls)) == 3

    # Removing the first row leaves the remaining two.
    remove_button = next(control for control in controls
                         if isinstance(control, ft.OutlinedButton)
                         and getattr(control, "content", None) == "Remove")
    remove_button.on_click(SimpleNamespace(control=remove_button))
    controls = list(walk_controls(root))
    assert len(_party_role_dropdowns(controls)) == 2


def test_calculate_splits_filer_taxes_per_party(monkeypatch):
    matches = [_location_row(7)]
    rates = {"dc": [_dc_rate(11, 7)]}
    _patch_estimate_services(monkeypatch, matches=matches, rates=rates)

    captured: dict[str, object] = {}

    def fake_party_charges(*, rules, transaction_date, context, parties, jurisdiction):
        captured["parties"] = parties
        captured["context"] = context
        return {"charges": [], "totals_by_currency": {}, "total_charges": Decimal("0"),
                "total_currency": "PKR", "warnings": [], "skipped_rules": []}

    monkeypatch.setattr(valuation, "calculate_charges_for_parties", fake_party_charges)

    root = valuation.build_valuation_page(LoadingPage())
    _stub_control_updates(root)
    controls = list(walk_controls(root))
    mauza = _auto(controls, "mauza")
    mauza.value = "Chak 1"
    mauza.on_change(SimpleNamespace(control=mauza))
    area = next(control for control in walk_controls(root)
                if isinstance(control, ft.TextField) and control.label == "Area (K-M-F or 10k5m)")
    area.value = "0-20-0"
    roles = _party_role_dropdowns(walk_controls(root))
    buyer = roles[0]
    buyer.value = "buyer"
    areas = [control for control in walk_controls(root)
             if isinstance(control, ft.TextField) and control.label == "Area share (K-M-F or 10k5m)"]
    areas[0].value = "12m"
    button = next(control for control in walk_controls(root)
                  if isinstance(control, ft.Button)
                  and getattr(control, "content", None) == "Calculate estimate")
    button.on_click(SimpleNamespace(control=button))

    parties = captured["parties"]
    # "12m" parses to exactly 12 marla; the stored share is the parsed value.
    assert parties[0] == {"role": "buyer", "name": None, "area_marla": "12",
                          "filer_status": "unknown"}
    assert len(parties) == 2
    # A single buyer keeps his status in the global context too.
    assert captured["context"]["buyer_filer_status"] == "unknown"
    assert _form_error(root).value == ""


def test_calculate_error_is_truthful_when_no_location_matched(monkeypatch):
    _patch_estimate_services(monkeypatch, matches=[], rates={"dc": []})

    root = valuation.build_valuation_page(LoadingPage())
    controls = list(walk_controls(root))
    mauza = _auto(controls, "mauza")
    mauza.value = "Wrong Mauza"
    mauza.on_change(SimpleNamespace(control=mauza))
    button = next(control for control in controls
                  if isinstance(control, ft.Button)
                  and getattr(control, "content", None) == "Calculate estimate")
    button.on_click(SimpleNamespace(control=button))
    message = _form_error(root).value
    assert "No schedule location matched the details you typed" in message


def test_deficient_amount_is_shown_when_written_value_is_below_valuation(monkeypatch):
    matches = [_location_row(7)]
    rates = {"dc": [_dc_rate(11, 7)]}
    _patch_estimate_services(monkeypatch, matches=matches, rates=rates)

    root = valuation.build_valuation_page(LoadingPage())
    _stub_control_updates(root)
    controls = list(walk_controls(root))
    mauza = _auto(controls, "mauza")
    mauza.value = "Chak 1"
    mauza.on_change(SimpleNamespace(control=mauza))
    written = next(control for control in walk_controls(root)
                   if isinstance(control, ft.TextField)
                   and control.label == "Registry written value (PKR)")
    # 20 marla (1 kanal) at PKR 100 per kanal values the deed at PKR 100.00;
    # a written value of 50 is below it, so 50.00 is deficient.
    written.value = "50"
    area = next(control for control in walk_controls(root)
                if isinstance(control, ft.TextField) and control.label == "Area (K-M-F or 10k5m)")
    area.value = "0-20-0"
    button = next(control for control in walk_controls(root)
                  if isinstance(control, ft.Button)
                  and getattr(control, "content", None) == "Calculate estimate")
    button.on_click(SimpleNamespace(control=button))

    texts = [control.value for control in walk_controls(root)
             if isinstance(control, ft.Text) and control.value]
    deficient_lines = [text for text in texts if "Deficient amount" in text]
    assert deficient_lines, "The deficient amount line was not shown."
    assert any("50.00" in text for text in deficient_lines)


def test_display_radio_stays_visible_but_disabled_without_candidates(monkeypatch):
    _patch_estimate_services(monkeypatch, matches=[], rates={"dc": []})

    root = valuation.build_valuation_page(LoadingPage())
    controls = list(walk_controls(root))
    radio = _schedule_display_radio(controls)
    # The radio is part of the rural layout even before any Mauza matches, so
    # the user can always find it; without candidates it is inactive.
    assert radio.visible is True
    assert radio.disabled is True


def test_tehsil_change_clears_stale_child_suggestions(monkeypatch):
    _patch_estimate_services(monkeypatch, matches=[], rates={"dc": []})

    root = valuation.build_valuation_page(LoadingPage())
    controls = list(walk_controls(root))
    tehsil = _auto(controls, "tehsil")
    qanoongoi = _auto(controls, "qanoongoi")
    # A previously loaded list must vanish the moment the parent changes;
    # otherwise the async hint reload shows other tehsils' values.
    qanoongoi.suggestions = [ft.AutoCompleteSuggestion(key="X", value="X")]
    tehsil.value = "Tehsil A"
    tehsil.on_change(SimpleNamespace(control=tehsil))
    assert qanoongoi.suggestions == []


def test_deed_party_areas_accept_kmf_and_compact_formats(monkeypatch):
    """Party area shares parse 1k2m, 3-0-0, and friends into exact marla."""
    matches = [_location_row(7)]
    rates = {"dc": [_dc_rate(11, 7)]}
    _patch_estimate_services(monkeypatch, matches=matches, rates=rates)

    captured: dict[str, object] = {}

    def fake_party_charges(*, rules, transaction_date, context, parties, jurisdiction):
        captured["parties"] = parties
        return {"charges": [], "totals_by_currency": {}, "total_charges": Decimal("0"),
                "total_currency": "PKR", "warnings": [], "skipped_rules": []}

    monkeypatch.setattr(valuation, "calculate_charges_for_parties", fake_party_charges)

    root = valuation.build_valuation_page(LoadingPage())
    _stub_control_updates(root)
    controls = list(walk_controls(root))
    mauza = _auto(controls, "mauza")
    mauza.value = "Chak 1"
    mauza.on_change(SimpleNamespace(control=mauza))
    areas = [control for control in walk_controls(root)
             if isinstance(control, ft.TextField) and control.label == "Area share (K-M-F or 10k5m)"]
    # "1k2m" is 1 kanal 2 marla = 22 marla; "3-0-0" is 3 kanal = 60 marla.
    areas[0].value = "1k2m"
    areas[1].value = "3-0-0"
    deed_area = next(control for control in walk_controls(root)
                     if isinstance(control, ft.TextField) and control.label == "Area (K-M-F or 10k5m)")
    deed_area.value = "0-20-0"
    button = next(control for control in walk_controls(root)
                  if isinstance(control, ft.Button)
                  and getattr(control, "content", None) == "Calculate estimate")
    button.on_click(SimpleNamespace(control=button))

    parties = captured["parties"]
    assert [party["area_marla"] for party in parties] == ["22", "60"]
    assert _form_error(root).value == ""


def test_deed_party_area_rejects_ambiguous_text_with_party_name(monkeypatch):
    matches = [_location_row(7)]
    rates = {"dc": [_dc_rate(11, 7)]}
    _patch_estimate_services(monkeypatch, matches=matches, rates=rates)

    def failing_party_charges(*, rules, transaction_date, context, parties, jurisdiction):
        raise AssertionError("Charges must not run with an unparsed area.")

    monkeypatch.setattr(valuation, "calculate_charges_for_parties", failing_party_charges)

    root = valuation.build_valuation_page(LoadingPage())
    _stub_control_updates(root)
    controls = list(walk_controls(root))
    mauza = _auto(controls, "mauza")
    mauza.value = "Chak 1"
    mauza.on_change(SimpleNamespace(control=mauza))
    areas = [control for control in walk_controls(root)
             if isinstance(control, ft.TextField) and control.label == "Area share (K-M-F or 10k5m)"]
    buyer_name = next(control for control in walk_controls(root)
                      if isinstance(control, ft.TextField) and control.label == "Name (optional)")
    buyer_name.value = "Ali"
    areas[0].value = "5m10"
    deed_area = next(control for control in walk_controls(root)
                     if isinstance(control, ft.TextField) and control.label == "Area (K-M-F or 10k5m)")
    deed_area.value = "0-20-0"
    button = next(control for control in walk_controls(root)
                  if isinstance(control, ft.Button)
                  and getattr(control, "content", None) == "Calculate estimate")
    button.on_click(SimpleNamespace(control=button))

    message = _form_error(root).value
    assert "Ali" in message
    assert "Attach a unit" in message


def test_identical_exact_pairs_auto_select_location_unit_and_rate(monkeypatch):
    """Mauza + Square + Kila fetch the exact schedule without dropdown picks.

    Two candidate rows can carry the same square/kila pair (duplicate
    schedule rows for road-side variants). When their DC rates are identical,
    the first pair is selected automatically and the single remaining rate
    plus its unit are preselected too.
    """
    matches = [
        _location_row(7, square_no="2", kila_no="12"),
        _location_row(8, square_no="2", kila_no="12"),
    ]
    rates = {"dc": [_dc_rate(11, 7), _dc_rate(12, 8)]}
    _patch_estimate_services(monkeypatch, matches=matches, rates=rates)

    root = valuation.build_valuation_page(LoadingPage())
    _stub_control_updates(root)
    controls = list(walk_controls(root))
    mauza = _auto(controls, "mauza")
    mauza.value = "Chak 1"
    mauza.on_change(SimpleNamespace(control=mauza))
    square = _auto(controls, "square_no")
    square.value = "2"
    square.on_change(SimpleNamespace(control=square))
    kila = _auto(controls, "kila_no")
    kila.value = "12"
    kila.on_change(SimpleNamespace(control=kila))

    location_match = _dropdown(controls, "Choose matching schedule location")
    unit = _dropdown(controls, "DC rate unit")
    rate = _dropdown(controls, "Matched DC rate")
    # Individual values are the default, so the auto-picked key is a pair.
    assert location_match.value == "7:2:12"
    assert unit.value == "per_kanal"
    assert rate.value == "11"
    status = next(control for control in walk_controls(root)
                  if isinstance(control, ft.Text)
                  and "DC rate found" in (control.value or ""))
    assert "Rs. 100.00" in status.value


def test_dc_rate_dropdowns_stay_visible_and_searchable_when_nothing_matches(monkeypatch):
    _patch_estimate_services(monkeypatch, matches=[], rates={"dc": []})

    root = valuation.build_valuation_page(LoadingPage())
    controls = list(walk_controls(root))
    mauza = _auto(controls, "mauza")
    mauza.value = "Wrong Mauza"
    mauza.on_change(SimpleNamespace(control=mauza))

    unit = _dropdown(controls, "DC rate unit")
    rate = _dropdown(controls, "Matched DC rate")
    assert unit.visible is True
    assert rate.visible is True
    assert rate.enable_filter is True
    assert rate.editable is True
    status = next(control for control in walk_controls(root)
                  if isinstance(control, ft.Text)
                  and "No schedule location matches" in (control.value or ""))


def test_refresh_lists_button_clears_the_hint_cache(monkeypatch):
    cleared = []
    monkeypatch.setattr(valuation, "clear_location_hint_cache",
                        lambda: cleared.append(True))
    _patch_estimate_services(monkeypatch, matches=[_location_row(7)],
                             rates={"dc": [_dc_rate(11, 7)]})

    root = valuation.build_valuation_page(LoadingPage())
    button = next(control for control in walk_controls(root)
                  if isinstance(control, ft.OutlinedButton)
                  and getattr(control, "content", None) == "Refresh lists")
    button.on_click(SimpleNamespace(control=button))
    assert cleared == [True]
    status_texts = [control.value for control in walk_controls(root)
                    if isinstance(control, ft.Text) and control.value]
    assert any("Location suggestion lists refreshed" in text for text in status_texts)


def test_schedule_display_radio_sits_beside_the_profile_selector(monkeypatch):
    _patch_estimate_services(monkeypatch, matches=[_location_row(7)],
                             rates={"dc": [_dc_rate(11, 7)]})

    root = valuation.build_valuation_page(LoadingPage())
    _stub_control_updates(root)
    controls = list(walk_controls(root))
    mauza = _auto(controls, "mauza")
    mauza.value = "Chak 1"
    mauza.on_change(SimpleNamespace(control=mauza))

    profile = next(control for control in walk_controls(root)
                   if isinstance(control, ft.RadioGroup)
                   and any(isinstance(radio, ft.Radio) and radio.value == "rural"
                           for radio in walk_controls(control)))
    holder = next(control for control in walk_controls(root)
                  if isinstance(control, ft.Row) and profile in control.controls)
    schedule_radio = _schedule_display_radio(controls)
    assert schedule_radio in holder.controls
    assert any(isinstance(item, ft.VerticalDivider) for item in holder.controls)
    assert schedule_radio.visible is True


def test_rural_locations_show_fbr_rate_menus_when_rows_exist(monkeypatch):
    """Rural FBR schedules drive FBR taxes, so their menus appear for rural."""
    matches = [_location_row(7)]
    rates = {"dc": [_dc_rate(11, 7)],
             "fbr": [{"id": 21, "location_id": 7, "schedule_type": "fbr",
                      "rate_unit": "per_square_foot", "rate_value": "300",
                      "classification": "Residential", "property_type": "Residential",
                      "mauza": "Chak 1", "effective_from": valuation.date(2020, 1, 1),
                      "effective_to": None}]}
    _patch_estimate_services(monkeypatch, matches=matches, rates=rates)

    root = valuation.build_valuation_page(LoadingPage())
    controls = list(walk_controls(root))
    mauza = _auto(controls, "mauza")
    mauza.value = "Chak 1"
    mauza.on_change(SimpleNamespace(control=mauza))

    fbr_unit = _dropdown(controls, "FBR rate unit (if available)")
    fbr_rate = _dropdown(controls, "Matched FBR rate (editable)")
    assert fbr_unit.visible is True
    assert fbr_rate.visible is True
    assert fbr_unit.value == "per_square_foot"
    assert fbr_rate.value == "21"


def test_rural_locations_without_fbr_rows_keep_fbr_menus_hidden(monkeypatch):
    _patch_estimate_services(monkeypatch, matches=[_location_row(7)],
                             rates={"dc": [_dc_rate(11, 7)], "fbr": []})
    root = valuation.build_valuation_page(LoadingPage())
    controls = list(walk_controls(root))
    mauza = _auto(controls, "mauza")
    mauza.value = "Chak 1"
    mauza.on_change(SimpleNamespace(control=mauza))
    fbr_rate = _dropdown(controls, "Matched FBR rate (editable)")
    assert fbr_rate.visible is False


def test_main_area_field_accepts_compact_notation(monkeypatch):
    """The deed area accepts 10k5m / plain kanal alongside 2-5-0."""
    matches = [_location_row(7)]
    rates = {"dc": [_dc_rate(11, 7)]}
    _patch_estimate_services(monkeypatch, matches=matches, rates=rates)

    captured: dict[str, object] = {}

    def fake_dual_valuation(**kwargs):
        captured["area_value"] = kwargs["area_value"]
        return {
            "area_square_feet": Decimal("12240"),
            "property_value": Decimal("100"),
            "transferred_value": Decimal("100"),
            "dc_value": Decimal("100"),
            "fbr_value": None,
            "registry_written_value": None,
            "declared_consideration": None,
            "comparison": None,
            "explanation": ["Area: 1-2-0."],
            "warnings": [],
            "rate_quantity": Decimal("45"),
            "rate_unit": "per_marla",
            "dc_rate": {}, "fbr_rate": None, "rate_id": 11, "rate": {},
            "fbr_valuation": None, "rate_snapshot": {"dc": {}, "fbr": None},
            "fbr_warning": None,
        }

    monkeypatch.setattr(valuation, "calculate_dual_valuation", fake_dual_valuation)

    root = valuation.build_valuation_page(LoadingPage())
    _stub_control_updates(root)
    controls = list(walk_controls(root))
    mauza = _auto(controls, "mauza")
    mauza.value = "Chak 1"
    mauza.on_change(SimpleNamespace(control=mauza))
    area = next(control for control in walk_controls(root)
                if isinstance(control, ft.TextField)
                and control.label == "Area (K-M-F or 10k5m)")
    area.value = "1k2m"   # 1 kanal 2 marla = 1-2-0
    button = next(control for control in walk_controls(root)
                  if isinstance(control, ft.Button)
                  and getattr(control, "content", None) == "Calculate estimate")
    button.on_click(SimpleNamespace(control=button))

    assert captured["area_value"] == "1-2-0"
    assert _form_error(root).value == ""


def test_location_choices_show_the_measurement_unit(monkeypatch):
    matches = [_location_row(7)]
    rates = {"dc": [dict(_dc_rate(11, 7), rate_unit="per_marla", rate_value="66650")]}
    _patch_estimate_services(monkeypatch, matches=matches, rates=rates)

    root = valuation.build_valuation_page(LoadingPage())
    controls = list(walk_controls(root))
    mauza = _auto(controls, "mauza")
    mauza.value = "Chak 1"
    mauza.on_change(SimpleNamespace(control=mauza))

    location_match = _dropdown(controls, "Choose matching schedule location")
    labels = [option.text for option in location_match.options]
    assert labels and all("Per marla" in label for label in labels)


def test_entered_valuation_produces_fee_comparison(monkeypatch):
    """An entered valuation computes the same fees against it side by side."""
    matches = [_location_row(7)]
    rates = {"dc": [_dc_rate(11, 7)]}
    _patch_estimate_services(monkeypatch, matches=matches, rates=rates)

    captured_contexts = []

    def fake_party_charges(*, rules, transaction_date, context, parties, jurisdiction):
        captured_contexts.append(context)
        base = context["dc_value"]
        return {
            "charges": [{
                "name": "Stamp fee", "category": "stamp_fee", "payer": "buyer",
                "amount": base / Decimal("100"), "currency_code": "PKR",
            }],
            "totals_by_currency": {"PKR": base / Decimal("100")},
            "total_currency": "PKR", "warnings": [], "skipped_rules": [],
        }

    monkeypatch.setattr(valuation, "calculate_charges_for_parties", fake_party_charges)

    root = valuation.build_valuation_page(LoadingPage())
    _stub_control_updates(root)
    controls = list(walk_controls(root))
    mauza = _auto(controls, "mauza")
    mauza.value = "Chak 1"
    mauza.on_change(SimpleNamespace(control=mauza))
    area = next(control for control in walk_controls(root)
                if isinstance(control, ft.TextField)
                and control.label == "Area (K-M-F or 10k5m)")
    area.value = "0-20-0"   # 1 kanal at PKR 100/kanal -> DC valuation 100.00
    written = next(control for control in walk_controls(root)
                   if isinstance(control, ft.TextField)
                   and control.label == "Registry written value (PKR)")
    written.value = "50"
    entered = next(control for control in walk_controls(root)
                   if isinstance(control, ft.TextField)
                   and control.label == "Entered valuation (PKR, optional)")
    entered.value = "100000"
    button = next(control for control in walk_controls(root)
                  if isinstance(control, ft.Button)
                  and getattr(control, "content", None) == "Calculate estimate")
    button.on_click(SimpleNamespace(control=button))

    # The charges ran twice: once on the calculated valuation, once on the
    # entered one (same rules, same parties, only the valuation base moved).
    assert len(captured_contexts) == 2
    assert captured_contexts[1]["dc_value"] == Decimal("100000")
    assert captured_contexts[1]["transferred_value"] == Decimal("100000")

    texts = [control.value for control in walk_controls(root)
             if isinstance(control, ft.Text) and control.value]
    assert any("Fee comparison" in text for text in texts)
    assert any("On entered valuation" in text for text in texts)
    # PKR 1.00 on the calculated base vs PKR 1,000.00 on the entered base.
    assert any("1,000.00" in text for text in texts)
