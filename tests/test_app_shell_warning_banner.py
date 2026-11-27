"""App-wide setup warning banner tests (v0.2.30)."""

import flet as ft
import pytest

from ui import app_shell
from ui.app_shell import AppShell
from ui.navigation import NavigationItem


class FakePage:
    def __init__(self):
        self.controls = []
        self.updates = 0
        self.dialogs = []

    def add(self, control):
        self.controls.append(control)

    def update(self):
        self.updates += 1

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


def _find(root, data):
    return next(
        control for control in _walk(root)
        if getattr(control, "data", None) == data
    )


@pytest.fixture
def shell_page():
    return FakePage()


def _make_shell(page: FakePage) -> AppShell:
    shell = AppShell(page)
    shell.items = [
        NavigationItem(
            key="dashboard",
            title="Dashboard",
            description="Overview",
            icon=None,
            page_builder=lambda _page: ft.Text("Dashboard page"),
        ),
        NavigationItem(
            key="settings",
            title="Settings",
            description="Preferences",
            icon=None,
            page_builder=lambda _page: ft.Text("Settings page"),
        ),
    ]
    return shell


def test_banner_summarizes_warnings_and_navigates_to_settings(
    shell_page, monkeypatch
):
    monkeypatch.setattr(app_shell, "visible_setup_warnings", lambda: [
        {
            "severity": "warning",
            "title": "No DC schedule rates",
        },
        {
            "severity": "info",
            "title": "No clients saved",
        },
        {
            "severity": "warning",
            "title": "Automatic backups are off",
        },
    ])
    shell = _make_shell(shell_page)

    shell.render()

    main_column = shell_page.controls[0].controls[1].content
    assert [
        getattr(control, "data", None) for control in main_column.controls[:3]
    ] == [
        "shell_topbar",
        "setup_warning_banner",
        "shell_page_content",
    ]
    banner = _find(main_column, "setup_warning_banner")
    text_values = [
        control.value for control in _walk(banner)
        if isinstance(control, ft.Text)
    ]
    assert "Setup check: 3 item(s) need attention" in text_values
    assert "No DC schedule rates; No clients saved; +1 more" in text_values

    button = next(
        control for control in _walk(banner)
        if isinstance(control, ft.TextButton)
        and control.content == "Review in Settings"
    )
    button.on_click(None)

    assert shell.current_key == "settings"


def test_no_banner_when_all_setup_warnings_are_hidden(
    shell_page, monkeypatch
):
    monkeypatch.setattr(app_shell, "visible_setup_warnings", lambda: [])
    shell = _make_shell(shell_page)

    shell.render()

    main_column = shell_page.controls[0].controls[1].content
    assert [
        getattr(control, "data", None) for control in main_column.controls
    ] == ["shell_topbar", "shell_page_content"]
    assert _find(main_column, "shell_page_content").content.value == (
        "Dashboard page"
    )


def test_view_details_dialog_lists_every_counted_warning_with_fixes(
    shell_page, monkeypatch
):
    """v0.2.31: the dialog matches the banner count, with complete details."""
    warnings = [
        {
            "severity": "warning",
            "title": "No DC schedule rates",
            "code": "no_dc_rates",
            "message": "DC valuation cannot calculate a rate yet.",
            "fix": "Add or import the DC schedule for your area.",
            "visible": True,
        },
        {
            "severity": "info",
            "title": "No clients saved",
            "code": "no_clients",
            "message": "The client dropdown is empty.",
            "fix": "Add or import clients in FBR Clients.",
            "visible": True,
        },
        {
            "severity": "warning",
            "title": "Hidden warning",
            "code": "backups_disabled",
            "message": "This should not appear.",
            "fix": "This should not appear.",
            "visible": False,
        },
    ]
    visible = [item for item in warnings if item["visible"]]
    monkeypatch.setattr(
        app_shell, "visible_setup_warnings", lambda: visible
    )
    shell = _make_shell(shell_page)

    shell.render()
    main_column = shell_page.controls[0].controls[1].content
    _find(main_column, "setup_warning_view_details").on_click(None)

    assert shell_page.dialogs
    dialog = shell_page.dialogs[-1]
    assert dialog.data == "setup_warning_details_dialog"
    assert dialog.title.value == "Setup check details"
    texts = [
        control.value for control in _walk(dialog)
        if isinstance(control, ft.Text)
    ]
    for item in visible:
        assert any(item["title"] in text for text in texts)
        assert item["message"] in texts
        assert f"Fix: {item['fix']}" in texts
        assert f"Code: {item['code']}" in texts
    assert not any("This should not appear." in text for text in texts)

    close_button = next(
        control for control in dialog.actions
        if isinstance(control, ft.TextButton)
    )
    close_button.on_click(None)
    assert shell_page.dialogs == []
