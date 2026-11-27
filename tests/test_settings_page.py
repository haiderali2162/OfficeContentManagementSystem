"""Settings-page tests for rate-card appearance customization."""

import asyncio
from pathlib import Path

import flet as ft
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base
from services import (
    backup_service,
    logging_service,
    settings_service,
    setup_warning_service,
)
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

    def run_task(self, handler, *args):
        """Run the page's background coroutine synchronously in tests."""
        return asyncio.run(handler(*args))


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
    # v0.2.30: the warning card reads the same temporary database during page
    # construction; never let it touch the live office database.
    monkeypatch.setattr(
        setup_warning_service, "SessionLocal", isolated_sessions
    )
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


def _checkbox_by_data(root, data):
    return next(
        control for control in _walk(root)
        if isinstance(control, ft.Checkbox) and control.data == data
    )


def _dropdown_by_label(root, label):
    return next(
        control for control in _walk(root)
        if isinstance(control, ft.Dropdown) and control.label == label
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
        "{schedule} · {land_type} schedule · {location}"
    )
    assert _field(root, "Card background").value == "#FFFFFF"
    assert _field(root, "Card border").value == "#E2E8F0"
    assert _field(root, "Heading text").value == "#0F172A"
    assert _field(root, "Body text").value == "#475569"
    assert _preview_heading(root).value == "DC · Urban schedule · Sample Mauza"


def test_settings_page_preview_updates_and_save_persists(settings_database):
    page = FakePage()
    root = settings.build_settings_page(page)
    heading = _field(root, "Card heading template")
    background = _field(root, "Card background")

    heading.value = "{schedule} · {land_type} - {location} ({year})"
    background.value = "#fff8e7"
    heading.on_change(None)
    assert _preview_heading(root).value == "DC · Urban - Sample Mauza (2025)"

    _button(root, "Save appearance").on_click(None)

    saved = settings_service.get_card_appearance()
    assert saved["heading_template"] == "{schedule} · {land_type} - {location} ({year})"
    assert saved["background_color"] == "#FFF8E7"
    assert any(
        "saved" in (control.value or "").lower()
        for control in _walk(root)
        if isinstance(control, ft.Text)
    )


def test_card_content_editor_saves_fields_and_formula_lines(settings_database):
    page = FakePage()
    root = settings.build_settings_page(page)
    checkboxes = [
        control
        for control in _walk(root)
        if isinstance(control, ft.Checkbox)
        and control.label in ("Rate (value + unit)", "Classification")
    ]
    assert checkboxes, "Card content checkboxes must be offered"
    for checkbox in checkboxes:
        checkbox.value = True

    _button(root, "Add formula line").on_click(None)
    _field(root, "Formula 1 label").value = "Per 1 kanal"
    _field(root, "Expression").value = "{rate_value} * 20"
    _field(root, "Expression").on_change(None)

    _button(root, "Save content").on_click(None)

    saved = settings_service.get_card_fields()
    assert saved["fields"] == [
        "classification", "rate",
    ] or saved["fields"] == ["rate", "classification"]
    assert saved["formulas"] == [
        {"label": "Per 1 kanal", "expression": "{rate_value} * 20"}
    ]
    # The preview must show a computed result, not just the stored formula.
    assert any(
        "Per 1 kanal: 250005" in (control.value or "")
        for control in _walk(root) if isinstance(control, ft.Text)
    )


def test_card_content_editor_blocks_invalid_formula(settings_database):
    root = settings.build_settings_page(FakePage())
    _button(root, "Add formula line").on_click(None)
    _field(root, "Formula 1 label").value = "Broken"
    _field(root, "Expression").value = "{rate_value} ** 2"

    _button(root, "Save content").on_click(None)

    assert settings_service.get_card_fields() == {"fields": [], "formulas": []}
    assert any(
        "Card formula problem" in (control.value or "")
        for control in _walk(root) if isinstance(control, ft.Text)
    )


def test_card_content_reset_restores_built_in_layout(settings_database):
    settings_service.set_card_fields({
        "fields": ["rate"],
        "formulas": [{"label": "X", "expression": "{rate_value}"}],
    })
    root = settings.build_settings_page(FakePage())

    _button(root, "Reset to built-in layout").on_click(None)

    assert settings_service.get_card_fields() == {"fields": [], "formulas": []}
    card_field_labels = set(settings_service.CARD_BODY_FIELD_LABELS.values())
    assert all(
        checkbox.value is False
        for checkbox in _walk(root)
        if isinstance(checkbox, ft.Checkbox)
        and checkbox.label in card_field_labels
    )
    assert any(
        "built-in layout" in (control.value or "")
        for control in _walk(root) if isinstance(control, ft.Text)
    )


def test_settings_page_offers_restore_from_backup_list(settings_database, tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    backup_name = "land_office_backup_20260101_120000.sqlite3"
    (folder / backup_name).write_bytes(b"not opened by the UI test")

    backup_service.save_backup_settings(
        enabled=False, frequency_hours=24, location=str(folder), keep_count=5,
    )
    root = settings.build_settings_page(FakePage())

    assert _button(root, "Restore selected backup")
    dropdown = next(
        control for control in _walk(root)
        if isinstance(control, ft.Dropdown)
        and control.label == "Restore from backup"
    )
    assert [option.text for option in dropdown.options] == [backup_name]
    assert dropdown.value == str(folder / backup_name)


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
        "{schedule} · {land_type} schedule · {location}"
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


def test_settings_page_renders_custom_formulas_guide(settings_database):
    root = settings.build_settings_page(FakePage())
    titles = [
        control.title.value
        for control in _walk(root)
        if isinstance(control, ft.ExpansionTile)
    ]
    assert "Custom registry formulas — user guide" in titles


def test_report_settings_save_persists_template_sections_and_columns(settings_database):
    root = settings.build_settings_page(FakePage())
    _dropdown_by_label(root, "Table template").value = "slate"
    _checkbox_by_data(root, "report_section:warnings").value = False
    _checkbox_by_data(root, "report_charge_column:source").value = False
    _checkbox_by_data(root, "report_comparison_column:difference").value = False

    _button(root, "Save report settings").on_click(None)

    template = settings_service.get_valuation_table_template()
    options = settings_service.get_valuation_report_options()
    assert template["id"] == "slate"
    assert "warnings" not in options["sections"]
    assert "source" not in options["charge_columns"]
    assert "difference" not in options["comparison_columns"]
    assert any(
        "Report settings saved" in (control.value or "")
        for control in _walk(root) if isinstance(control, ft.Text)
    )
    assert any(
        control.data == "valuation_report_preview"
        for control in _walk(root)
    )


def test_report_settings_reset_restores_template_and_all_options(settings_database):
    settings_service.set_valuation_table_template("compact")
    settings_service.set_valuation_report_options({
        "sections": ["charges"],
        "charge_columns": ["amount", "charge"],
        "comparison_columns": ["written"],
    })
    root = settings.build_settings_page(FakePage())

    _button(root, "Reset report settings").on_click(None)

    assert settings_service.get_valuation_table_template()["id"] == (
        settings_service.DEFAULT_VALUATION_TABLE_TEMPLATE
    )
    assert settings_service.get_valuation_report_options() == (
        settings_service.DEFAULT_VALUATION_REPORT_OPTIONS
    )
    assert _dropdown_by_label(root, "Table template").value == (
        settings_service.DEFAULT_VALUATION_TABLE_TEMPLATE
    )
    assert _checkbox_by_data(root, "report_section:warnings").value is True
    assert _checkbox_by_data(root, "report_charge_column:source").value is True
    assert _checkbox_by_data(root, "report_comparison_column:difference").value is True


def test_report_settings_blocks_an_empty_section_selection(settings_database):
    root = settings.build_settings_page(FakePage())
    for key in settings_service.REPORT_SECTION_LABELS:
        _checkbox_by_data(root, f"report_section:{key}").value = False

    _button(root, "Save report settings").on_click(None)

    assert settings_service.get_valuation_report_options() == (
        settings_service.DEFAULT_VALUATION_REPORT_OPTIONS
    )
    assert any(
        "at least one section" in (control.value or "").lower()
        for control in _walk(root) if isinstance(control, ft.Text)
    )


def test_settings_page_has_logs_category(settings_database):
    root = settings.build_settings_page(FakePage())
    titles = [
        control.title.value
        for control in _walk(root)
        if isinstance(control, ft.ExpansionTile)
    ]
    assert "Logs — backend errors and crash reports" in titles
    assert _button(root, "Save log settings")
    assert _button(root, "Open log folder")


def test_manual_backup_button_creates_backup_and_reports_success(
    settings_database, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        settings, "create_backup_now",
        lambda: calls.append(True) or {
            "target": "D:/backups/land_office_backup_test.sqlite3",
            "removed_old": [],
        },
    )
    monkeypatch.setattr(settings, "list_backups", lambda _location: [])
    root = settings.build_settings_page(FakePage())

    _button(root, "Back up now").on_click(None)

    assert calls == [True]
    assert any(
        "Backup created: land_office_backup_test.sqlite3" in (control.value or "")
        for control in _walk(root) if isinstance(control, ft.Text)
    )


def test_open_backup_folder_button_reports_success(
    settings_database, monkeypatch
):
    monkeypatch.setattr(
        settings, "open_backup_folder", lambda: Path("D:/backups")
    )
    root = settings.build_settings_page(FakePage())

    _button(root, "Open backup folder").on_click(None)

    assert any(
        "Opened backup folder" in (control.value or "")
        for control in _walk(root) if isinstance(control, ft.Text)
    )


def test_estimate_naming_category_saves_strict_mode_and_template(settings_database):
    """v0.2.28 naming controls plus v0.2.29 identity choices persist."""
    root = settings.build_settings_page(FakePage())
    titles = [
        control.title.value
        for control in _walk(root)
        if isinstance(control, ft.ExpansionTile)
    ]
    assert "Valuation estimates — task numbers and naming" in titles

    _checkbox_by_data(root, "naming_strict_mode").value = True
    _checkbox_by_data(root, "naming_auto_task_no").value = False
    # v0.2.29: Settings can require any combination of identity fields.
    _checkbox_by_data(root, "naming_required_waseeka_name").value = False
    _checkbox_by_data(root, "naming_required_registry_area").value = True
    _checkbox_by_data(root, "naming_required_client").value = True
    _dropdown_by_label(root, "Estimate name preset").value = "custom_date_time"
    _dropdown_by_label(root, "Estimate name preset").on_change(None)
    _field(root, "Custom naming text").value = "Deed"
    _field(root, "Custom naming text").on_change(None)

    _button(root, "Save estimate naming").on_click(None)

    saved = settings_service.get_estimate_naming()
    assert saved["strict_mode"] is True
    assert saved["auto_task_no"] is False
    assert saved["required_identity_fields"] == [
        "task_no", "registry_area", "client",
    ]
    assert saved["preset"] == "custom_date_time"
    assert saved["template"] == "{custom}_{date}_{time}"
    assert saved["custom_text"] == "Deed"
    assert any(
        "Preview: Deed_" in (control.value or "")
        for control in _walk(root)
        if isinstance(control, ft.Text)
    )


def test_estimate_naming_preset_updates_template_and_reset_restores_defaults(settings_database):
    settings_service.set_estimate_naming({
        "strict_mode": True, "auto_task_no": False,
        "preset": "task_only", "template": "{task_no}",
    })
    root = settings.build_settings_page(FakePage())
    assert _field(root, "Estimate naming template").value == "{task_no}"

    preset = _dropdown_by_label(root, "Estimate name preset")
    preset.value = "task_date"
    preset.on_change(None)
    assert _field(root, "Estimate naming template").value == "{task_no}_{date}"

    _button(root, "Reset estimate naming").on_click(None)

    assert settings_service.get_estimate_naming() == (
        settings_service.DEFAULT_ESTIMATE_NAMING
    )
    assert _field(root, "Estimate naming template").value == (
        settings_service.DEFAULT_ESTIMATE_NAMING["template"]
    )
    assert _checkbox_by_data(root, "naming_strict_mode").value is True
    assert _checkbox_by_data(root, "naming_required_waseeka_name").value is True
    assert _checkbox_by_data(root, "naming_required_client").value is False


def test_estimate_naming_blocks_unknown_token_without_saving(settings_database):
    root = settings.build_settings_page(FakePage())
    _field(root, "Estimate naming template").value = "{task_no}_{unknown}"

    _button(root, "Save estimate naming").on_click(None)

    assert settings_service.get_estimate_naming() == (
        settings_service.DEFAULT_ESTIMATE_NAMING
    )
    assert any(
        "Unknown naming token" in (control.value or "")
        for control in _walk(root) if isinstance(control, ft.Text)
    )


def test_log_settings_save_and_open_folder(settings_database, monkeypatch, tmp_path):
    folder = tmp_path / "logs"
    folder.mkdir()
    reconfigured = []
    opened = []
    monkeypatch.setattr(
        logging_service, "reconfigure_logging",
        lambda: reconfigured.append(True),
    )
    monkeypatch.setattr(
        logging_service, "open_log_folder",
        lambda: opened.append(True) or folder,
    )
    root = settings.build_settings_page(FakePage())
    _field(root, "Log folder (blank = Documents)").value = str(folder)

    _button(root, "Save log settings").on_click(None)

    assert logging_service.get_log_settings() == {
        "enabled": True, "location": str(folder.resolve())
    }
    assert reconfigured

    _button(root, "Open log folder").on_click(None)

    assert opened
    assert any(
        "Opened log folder" in (control.value or "")
        for control in _walk(root) if isinstance(control, ft.Text)
    )


def test_warning_settings_category_saves_toggles_and_ignored_code(
    settings_database
):
    root = settings.build_settings_page(FakePage())
    titles = [
        control.title.value
        for control in _walk(root)
        if isinstance(control, ft.ExpansionTile)
    ]
    assert "Warnings — missing data and setup checks" in titles

    _checkbox_by_data(root, "warning_show_info").value = False
    _checkbox_by_data(root, "warning_check_clients").value = False
    _checkbox_by_data(root, "warning_ignore_no_clients").value = True

    _button(root, "Save warning settings").on_click(None)

    saved = settings_service.get_setup_warning_settings()
    assert saved["enabled"] is True
    assert saved["show_info"] is False
    assert saved["checks"]["clients"] is False
    assert saved["ignored_codes"] == ["no_clients"]
    assert any(
        "Warning settings saved" in (control.value or "")
        for control in _walk(root) if isinstance(control, ft.Text)
    )


def test_warning_settings_reset_restores_all_checks(settings_database):
    settings_service.set_setup_warning_settings({
        "enabled": False,
        "show_info": False,
        "checks": {
            key: False for key in settings_service.SETUP_WARNING_CHECK_LABELS
        },
        "ignored_codes": ["no_clients", "no_dc_rates"],
    })
    root = settings.build_settings_page(FakePage())

    _button(root, "Reset warning settings").on_click(None)

    assert settings_service.get_setup_warning_settings() == (
        settings_service.DEFAULT_SETUP_WARNING_SETTINGS
    )
    assert _checkbox_by_data(root, "warning_enabled").value is True
    assert _checkbox_by_data(root, "warning_show_info").value is True
    assert _checkbox_by_data(root, "warning_check_clients").value is True
    assert _checkbox_by_data(root, "warning_ignore_no_clients").value is False
    assert any(
        "Warning settings reset" in (control.value or "")
        for control in _walk(root) if isinstance(control, ft.Text)
    )


def test_hidden_warning_remains_reviewable_in_settings(settings_database):
    settings_service.set_setup_warning_settings({
        "enabled": False,
        "ignored_codes": ["no_dc_rates"],
    })
    root = settings.build_settings_page(FakePage())

    assert any(
        "No DC schedule rates" in (control.value or "")
        for control in _walk(root) if isinstance(control, ft.Text)
    )
    assert _checkbox_by_data(root, "warning_ignore_no_dc_rates").value is True
    assert setup_warning_service.visible_setup_warnings() == []


def test_client_copy_fields_category_offers_and_saves_all_builtin_fields(
    settings_database
):
    root = settings.build_settings_page(FakePage())
    titles = [
        control.title.value
        for control in _walk(root)
        if isinstance(control, ft.ExpansionTile)
    ]
    assert "FBR Clients — direct copy fields" in titles
    assert {
        control.data.removeprefix("client_copy_field:")
        for control in _walk(root)
        if isinstance(control, ft.Checkbox)
        and str(control.data).startswith("client_copy_field:")
    } == set(settings_service.CLIENT_COPY_FIELD_LABELS)

    for key in settings_service.DEFAULT_CLIENT_COPY_FIELDS:
        assert _checkbox_by_data(root, f"client_copy_field:{key}").value is True

    _checkbox_by_data(root, "client_copy_field:password").value = False
    _checkbox_by_data(root, "client_copy_field:name").value = True

    _button(root, "Save copy fields").on_click(None)

    assert settings_service.get_client_copy_fields() == [
        "name", "cnic", "phone_number", "email", "address",
    ]
    assert any(
        "Copy fields saved" in (control.value or "")
        for control in _walk(root) if isinstance(control, ft.Text)
    )


def test_client_copy_fields_reset_restores_defaults(settings_database):
    settings_service.set_client_copy_fields(["name"])
    root = settings.build_settings_page(FakePage())

    _button(root, "Reset copy fields").on_click(None)

    assert settings_service.get_client_copy_fields() == (
        settings_service.DEFAULT_CLIENT_COPY_FIELDS
    )
    assert _checkbox_by_data(root, "client_copy_field:email").value is True
    assert _checkbox_by_data(root, "client_copy_field:phone_number").value is True
    assert _checkbox_by_data(root, "client_copy_field:name").value is False
    assert any(
        "Copy fields reset" in (control.value or "")
        for control in _walk(root) if isinstance(control, ft.Text)
    )
