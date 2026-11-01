"""The valuation page control tree builds against the installed Flet API."""

import flet as ft
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

    def update(self):
        return None

    def show_dialog(self, dialog):
        self.dialogs.append(dialog)
        return None

    def pop_dialog(self):
        return None


def test_valuation_page_builds_with_tabs_and_existing_rate_management(monkeypatch):
    monkeypatch.setattr(valuation, "list_locations", lambda: [])
    monkeypatch.setattr(valuation, "list_client_choices", lambda: [])
    monkeypatch.setattr(valuation, "list_tax_fee_rules", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_estimates", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_registry_fee_formulas", lambda: [])
    monkeypatch.setattr(valuation, "list_registry_fee_custom_fields", lambda: [])
    monkeypatch.setattr(dc_rates, "list_locations", lambda: [])
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

    monkeypatch.setattr(valuation, "list_locations", lambda: [])
    monkeypatch.setattr(valuation, "list_client_choices", lambda: [])
    monkeypatch.setattr(valuation, "list_tax_fee_rules", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_estimates", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_registry_fee_formulas", lambda: [])
    monkeypatch.setattr(valuation, "list_registry_fee_custom_fields", lambda: [])
    monkeypatch.setattr(dc_rates, "list_locations", lambda: [])
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
    monkeypatch.setattr(valuation, "list_locations", lambda: [])
    monkeypatch.setattr(valuation, "list_client_choices", lambda: [])
    monkeypatch.setattr(valuation, "list_tax_fee_rules", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_estimates", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_registry_fee_formulas", lambda: [])
    monkeypatch.setattr(valuation, "list_registry_fee_custom_fields", lambda: [])
    monkeypatch.setattr(dc_rates, "list_locations", lambda: [])
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
    monkeypatch.setattr(valuation, "list_locations", lambda: [])
    monkeypatch.setattr(valuation, "list_client_choices", lambda: [])
    monkeypatch.setattr(valuation, "list_tax_fee_rules", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_estimates", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_registry_fee_formulas", lambda: [])
    monkeypatch.setattr(valuation, "list_registry_fee_custom_fields", lambda: [])
    monkeypatch.setattr(dc_rates, "list_locations", lambda: [])
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

