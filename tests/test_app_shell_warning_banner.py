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

    def add(self, control):
        self.controls.append(control)

    def update(self):
        self.updates += 1


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
