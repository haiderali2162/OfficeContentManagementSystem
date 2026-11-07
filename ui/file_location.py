"""Shared prompts and helpers for default import/export folders."""

from __future__ import annotations

import asyncio
from pathlib import Path

import flet as ft


async def ask_location_choice(
    page: ft.Page,
    *,
    action: str,
    default_location: str,
) -> str | None:
    """Ask whether to use the saved default folder for one file action.

    Returns ``"default"`` to use the saved folder, ``"choose"`` to open the
    normal file picker, or ``None`` when the user cancels. With no saved
    default, the picker is used directly without an extra dialog.
    """
    if not default_location:
        return "choose"

    loop = asyncio.get_running_loop()
    result: asyncio.Future[str | None] = loop.create_future()

    def finish(choice: str | None, *, close_dialog: bool = True) -> None:
        if result.done():
            return
        result.set_result(choice)
        if close_dialog:
            try:
                page.pop_dialog()
            except Exception:
                # A minimal page adapter may not implement dialog stacking.
                pass
        page.update()

    dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Use the default location?"),
        content=ft.Column(
            controls=[
                ft.Text(
                    f"Default folder for {action}:",
                    size=12,
                    color="#475569",
                ),
                ft.Text(
                    default_location,
                    size=12,
                    weight=ft.FontWeight.BOLD,
                    selectable=True,
                ),
                ft.Text(
                    "Use this folder, or choose another location for this action?",
                    size=12,
                    color="#475569",
                ),
            ],
            width=520,
            tight=True,
            spacing=6,
        ),
        actions=[
            ft.TextButton("Cancel", on_click=lambda _: finish(None)),
            ft.OutlinedButton(
                "Choose another location",
                on_click=lambda _: finish("choose"),
            ),
            ft.Button(
                "Use default location",
                on_click=lambda _: finish("default"),
            ),
        ],
        on_dismiss=lambda _: finish(None, close_dialog=False),
    )
    page.show_dialog(dialog)
    page.update()
    return await result


def write_export_to_folder(
    folder: str,
    file_name: str,
    payload: bytes,
) -> str:
    """Write an export into ``folder`` without overwriting an existing file.

    The file name is reduced to its base name so a stored preference cannot be
    used to escape the selected folder. Collisions receive a numeric suffix.
    """
    directory = Path(folder).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    base_name = Path(file_name).name
    stem = Path(base_name).stem
    suffix = Path(base_name).suffix

    counter = 0
    while True:
        target = directory / (
            base_name if counter == 0 else f"{stem} ({counter}){suffix}"
        )
        try:
            with target.open("xb") as output:
                output.write(payload)
            return str(target)
        except FileExistsError:
            counter += 1
            if counter > 1000:
                raise OSError(
                    f"Could not choose an unused export file name in {directory}."
                )
