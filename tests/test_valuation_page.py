"""The valuation page control tree builds against the installed Flet API."""

import asyncio
import flet as ft
import json
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


