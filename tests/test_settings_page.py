"""Settings-page tests for rate-card appearance customization."""

import asyncio

import flet as ft
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base
from services import settings_service
from ui.pages import settings


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


@pytest.fixture
def settings_database(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'settings_page.sqlite3').as_posix()}"
    )
    Base.metadata.create_all(engine)
    isolated_sessions = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False
    )
    monkeypatch.setattr(settings_service, "SessionLocal", isolated_sessions)
    try:
        yield isolated_sessions
    finally:
        engine.dispose()


def _field(root, label):
    return next(
        control for control in _walk(root)
        if isinstance(control, ft.TextField) and control.label == label
    )


def _button(root, text):
    return next(
        control for control in _walk(root)
        if isinstance(control, (ft.Button, ft.OutlinedButton))
        and control.content == text
    )


def _preview_heading(root):
    for control in _walk(root):
        value = (control.value or "") if isinstance(control, ft.Text) else ""
        if (
            "Sample Mauza" in value
            and not value.startswith(("urban", "Residential"))
        ):
            return control
    raise StopIteration("Preview heading not found")


def test_settings_page_renders_defaults_and_preview(settings_database):
    root = settings.build_settings_page(FakePage())

    assert _field(root, "Card heading template").value == (
        "{schedule} schedule · {location}"
    )
    assert _field(root, "Card background").value == "#FFFFFF"
    assert _field(root, "Card border").value == "#E2E8F0"
    assert _field(root, "Heading text").value == "#0F172A"
    assert _field(root, "Body text").value == "#475569"
    assert _preview_heading(root).value == "DC schedule · Sample Mauza"


def test_settings_page_preview_updates_and_save_persists(settings_database):
    page = FakePage()
    root = settings.build_settings_page(page)
    heading = _field(root, "Card heading template")
    background = _field(root, "Card background")

    heading.value = "{schedule} - {location} ({year})"
    background.value = "#fff8e7"
    heading.on_change(None)
    assert _preview_heading(root).value == "DC - Sample Mauza (2025)"

    _button(root, "Save appearance").on_click(None)

    saved = settings_service.get_card_appearance()
    assert saved["heading_template"] == "{schedule} - {location} ({year})"
    assert saved["background_color"] == "#FFF8E7"
    assert any(
        "saved" in (control.value or "").lower()
        for control in _walk(root)
        if isinstance(control, ft.Text)
    )


def test_settings_page_validation_blocks_invalid_colour(settings_database):
    root = settings.build_settings_page(FakePage())
    background = _field(root, "Card background")
    background.value = "orange"

    _button(root, "Save appearance").on_click(None)

    assert settings_service.get_card_appearance() == settings_service.DEFAULT_CARD_APPEARANCE
    status = next(
        control for control in _walk(root)
        if isinstance(control, ft.Text)
        and "hex colour" in (control.value or "")
    )
    assert status.color == "#B91C1C"


def test_settings_page_reset_restores_defaults(settings_database):
    settings_service.set_card_appearance({
        "heading_template": "{schedule} - {location}",
        "background_color": "#000000",
    })
    root = settings.build_settings_page(FakePage())
    assert _field(root, "Card background").value == "#000000"

    _button(root, "Reset to defaults").on_click(None)

    assert _field(root, "Card background").value == "#FFFFFF"
    assert _field(root, "Card heading template").value == (
        "{schedule} schedule · {location}"
    )
    assert settings_service.get_card_appearance() == settings_service.DEFAULT_CARD_APPEARANCE


def test_settings_page_renders_saved_default_folder(settings_database, tmp_path):
    folder = tmp_path / "exports"
    folder.mkdir()
    settings_service.set_default_file_location(str(folder))

    root = settings.build_settings_page(FakePage())

    assert _field(root, "Default import/export folder").value == str(
        folder.resolve()
    )


def test_settings_page_saves_and_clears_default_folder(settings_database, tmp_path):
    folder = tmp_path / "exports"
    folder.mkdir()
    root = settings.build_settings_page(FakePage())
    field = _field(root, "Default import/export folder")
    assert field.value == ""

    field.value = str(folder)
    _button(root, "Save folder").on_click(None)

    assert settings_service.get_default_file_location() == str(folder.resolve())
    assert any(
        "Default folder saved" in (control.value or "")
        for control in _walk(root)
        if isinstance(control, ft.Text)
    )

    _button(root, "Clear default").on_click(None)

    assert settings_service.get_default_file_location() == ""
    assert field.value == ""


def test_settings_page_rejects_a_missing_default_folder(settings_database, tmp_path):
    root = settings.build_settings_page(FakePage())
    field = _field(root, "Default import/export folder")
    field.value = str(tmp_path / "missing")

    _button(root, "Save folder").on_click(None)

    assert settings_service.get_default_file_location() == ""
    assert any(
        "does not exist" in (control.value or "")
        for control in _walk(root)
        if isinstance(control, ft.Text)
    )


def test_settings_page_browse_uses_the_folder_picker(
    settings_database, tmp_path, monkeypatch
):
    calls = []

    class FakePicker:
        async def get_directory_path(self, **kwargs):
            calls.append(kwargs)
            return str(tmp_path)

    monkeypatch.setattr(ft, "FilePicker", FakePicker)
    page = FakePage()
    root = settings.build_settings_page(page)

    asyncio.run(_button(root, "Browse folder").on_click())

    assert _field(root, "Default import/export folder").value == str(tmp_path)
    assert calls[0]["initial_directory"] is None
    assert any(
        "Folder selected" in (control.value or "")
        for control in _walk(root)
        if isinstance(control, ft.Text)
    )
