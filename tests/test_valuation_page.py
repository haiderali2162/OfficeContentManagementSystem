"""The valuation page control tree builds against the installed Flet API."""

import flet as ft
from types import SimpleNamespace

from ui.pages import dc_rates
from ui.pages import valuation


class FakePage:
    def __init__(self):
        self.services = []
        self.controls = []

    def update(self):
        return None

    def show_dialog(self, _dialog):
        return None

    def pop_dialog(self):
        return None


def test_valuation_page_builds_with_tabs_and_existing_rate_management(monkeypatch):
    monkeypatch.setattr(valuation, "list_locations", lambda: [])
    monkeypatch.setattr(valuation, "list_client_choices", lambda: [])
    monkeypatch.setattr(valuation, "list_tax_fee_rules", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_estimates", lambda **_kwargs: [])
    monkeypatch.setattr(valuation, "list_registry_fee_formulas", lambda: [])
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
    monkeypatch.setattr(dc_rates, "list_locations", lambda: [])
    monkeypatch.setattr(dc_rates, "list_dc_rates", lambda **_kwargs: [])

    page = TaskPage()
    root = valuation.build_valuation_page(page)
    tabs = root.controls[2]
    assert len(page.scheduled) == 1  # valuation choice/history loads only

    tabs.on_change(SimpleNamespace(control=SimpleNamespace(selected_index=2)))

    assert len(page.scheduled) == 2  # DC/FBR load is scheduled on demand

