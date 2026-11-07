"""Tests for the shared default import/export folder helpers."""

import asyncio
from pathlib import Path

import pytest

from ui import file_location


class FakePage:
    def __init__(self):
        self.dialogs = []
        self.updates = 0

    def show_dialog(self, dialog):
        self.dialogs.append(dialog)

    def pop_dialog(self):
        if self.dialogs:
            self.dialogs.pop()

    def update(self):
        self.updates += 1


def _choice_button(dialog, label):
    return next(button for button in dialog.actions if button.content == label)


def test_no_default_skips_the_prompt():
    page = FakePage()

    result = asyncio.run(
        file_location.ask_location_choice(
            page, action="export", default_location=""
        )
    )

    assert result == "choose"
    assert page.dialogs == []
    assert page.updates == 0


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Use default location", "default"),
        ("Choose another location", "choose"),
        ("Cancel", None),
    ],
)
def test_prompt_returns_the_selected_choice(label, expected, tmp_path):
    async def run():
        page = FakePage()
        task = asyncio.create_task(
            file_location.ask_location_choice(
                page,
                action="schedule export",
                default_location=str(tmp_path),
            )
        )
        await asyncio.sleep(0)
        dialog = page.dialogs[-1]
        assert dialog.modal is True
        assert str(tmp_path) in " ".join(
            control.value or ""
            for control in dialog.content.controls
            if hasattr(control, "value")
        )
        _choice_button(dialog, label).on_click(None)
        return await task, page

    result, page = asyncio.run(run())

    assert result == expected
    assert page.dialogs == []


def test_dismissing_the_prompt_is_treated_as_cancel(tmp_path):
    async def run():
        page = FakePage()
        task = asyncio.create_task(
            file_location.ask_location_choice(
                page,
                action="import",
                default_location=str(tmp_path),
            )
        )
        await asyncio.sleep(0)
        dialog = page.dialogs[-1]
        # Flet removes a dismissed dialog before running the user's
        # ``on_dismiss`` handler.
        page.pop_dialog()
        dialog.on_dismiss(None)
        return await task, page

    result, page = asyncio.run(run())

    assert result is None
    assert page.dialogs == []


def test_write_export_creates_the_folder_and_writes_the_payload(tmp_path):
    folder = tmp_path / "nested" / "exports"

    destination = Path(
        file_location.write_export_to_folder(
            str(folder), "report.csv", b"a,b\n1,2"
        )
    )

    assert destination == folder / "report.csv"
    assert destination.read_bytes() == b"a,b\n1,2"


def test_write_export_does_not_overwrite_an_existing_file(tmp_path):
    first = Path(
        file_location.write_export_to_folder(
            str(tmp_path), "report.csv", b"first"
        )
    )
    second = Path(
        file_location.write_export_to_folder(
            str(tmp_path), "report.csv", b"second"
        )
    )

    assert first.name == "report.csv"
    assert second.name == "report (1).csv"
    assert first.read_bytes() == b"first"
    assert second.read_bytes() == b"second"


def test_write_export_reduces_the_file_name_to_its_base_name(tmp_path):
    destination = Path(
        file_location.write_export_to_folder(
            str(tmp_path), "../escape.csv", b"safe"
        )
    )

    assert destination.parent == tmp_path
    assert destination.name == "escape.csv"
    assert destination.read_bytes() == b"safe"
