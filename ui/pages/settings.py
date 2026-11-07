"""Application settings pages.

This module exposes the DC/FBR rate-card appearance editor and the default
import/export folder preference. Values are persisted through
``services.settings_service`` and applied the next time the relevant page is
built.
"""

from __future__ import annotations

import flet as ft

from services.settings_service import (
    CARD_COLOR_FIELDS,
    CARD_COLOR_LABELS,
    CARD_HEADING_FIELDS,
    DEFAULT_CARD_APPEARANCE,
    SettingsError,
    format_card_heading,
    get_card_appearance,
    get_default_file_location,
    normalize_card_appearance,
    reset_card_appearance,
    set_card_appearance,
    set_default_file_location,
)


_PREVIEW_RECORD = {
    "schedule_type": "dc",
    "location_name": "Sample Mauza",
    "classification": "Residential",
    "property_type": "Residential",
    "district": "Lahore",
    "tehsil": "Model Town",
    "mauza": "Sample Mauza",
    "land_type": "urban",
    "published_year": 2025,
}


def _placeholder_help() -> ft.Text:
    placeholders = ", ".join(
        f"{{{name}}}" for name in sorted(CARD_HEADING_FIELDS)
    )
    return ft.Text(
        f"Available placeholders: {placeholders}",
        size=11,
        color="#64748B",
        selectable=True,
    )


def build_settings_page(page: ft.Page) -> ft.Control:
    """Build the settings page with rate-card and file-location preferences."""
    appearance = get_card_appearance()

    file_picker = ft.FilePicker()
    existing_picker = next(
        (service for service in page.services if isinstance(service, ft.FilePicker)),
        None,
    )
    if existing_picker is None:
        page.services.append(file_picker)
    else:
        file_picker = existing_picker

    default_location = get_default_file_location()
    location_field = ft.TextField(
        label="Default import/export folder",
        value=default_location,
        hint_text="No default folder set; choose a location for every file action.",
        expand=True,
    )
    location_status = ft.Text("", size=12, color="#475569", selectable=True)

    async def browse_for_folder(_=None):
        """Open the native folder picker and place the result in the field."""
        try:
            selected = await file_picker.get_directory_path(
                dialog_title="Choose the default import/export folder",
                initial_directory=location_field.value or None,
            )
        except Exception as exc:
            location_status.value = f"Could not open the folder picker: {exc}"
            location_status.color = "#B91C1C"
        else:
            if selected:
                location_field.value = selected
                location_status.value = (
                    "Folder selected. Save the folder to make it the default."
                )
                location_status.color = "#475569"
        page.update()

    def save_default_location(_=None):
        """Validate and persist the folder shown in the field."""
        try:
            saved = set_default_file_location(location_field.value or "")
        except SettingsError as exc:
            location_status.value = str(exc)
            location_status.color = "#B91C1C"
        except Exception as exc:
            location_status.value = f"Could not save the default folder: {exc}"
            location_status.color = "#B91C1C"
        else:
            location_field.value = saved
            location_status.value = (
                "Default folder saved. Import and export actions will ask "
                "whether to use it."
                if saved
                else "Default folder cleared. Each import and export will ask "
                "for a location."
            )
            location_status.color = "#15803D"
        page.update()

    def clear_default_location(_=None):
        """Remove the saved preference and restore per-action prompts."""
        try:
            set_default_file_location("")
        except Exception as exc:
            location_status.value = f"Could not clear the default folder: {exc}"
            location_status.color = "#B91C1C"
        else:
            location_field.value = ""
            location_status.value = (
                "Default folder cleared. Each import and export will ask for "
                "a location."
            )
            location_status.color = "#15803D"
        page.update()

    location_card = ft.Container(
        padding=18,
        bgcolor="#FFFFFF",
        border=ft.Border.all(1, "#E2E8F0"),
        border_radius=12,
        content=ft.Column(
            controls=[
                ft.Text(
                    "Default import / export folder",
                    size=18,
                    weight=ft.FontWeight.BOLD,
                ),
                ft.Text(
                    "When a default folder is set, the app asks before every "
                    "import and export whether to use it or choose another "
                    "location. Leave it empty to always choose a location.",
                    size=12,
                    color="#64748B",
                ),
                ft.Divider(height=1, color="#E2E8F0"),
                ft.Row(
                    controls=[
                        location_field,
                        ft.OutlinedButton(
                            "Browse folder", on_click=browse_for_folder
                        ),
                    ],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Row(
                    controls=[
                        ft.Button(
                            "Save folder", on_click=save_default_location
                        ),
                        ft.OutlinedButton(
                            "Clear default", on_click=clear_default_location
                        ),
                    ],
                    spacing=8,
                    wrap=True,
                ),
                location_status,
            ],
            spacing=10,
            tight=True,
        ),
    )

    heading_field = ft.TextField(
        label="Card heading template",
        value=appearance["heading_template"],
        hint_text=DEFAULT_CARD_APPEARANCE["heading_template"],
        on_change=lambda _: update_preview(),
    )
    color_fields = {
        field_name: ft.TextField(
            label=CARD_COLOR_LABELS[field_name],
            value=appearance[field_name],
            width=210,
            max_length=9,
            on_change=lambda _: update_preview(),
        )
        for field_name in CARD_COLOR_FIELDS
    }

    preview_heading = ft.Text(
        weight=ft.FontWeight.BOLD,
        size=16,
        selectable=True,
    )
    preview_body = ft.Text(size=13, selectable=True)
    preview_meta = ft.Text(size=12, selectable=True)
    preview_card = ft.Container(
        padding=14,
        border_radius=10,
        content=ft.Column(
            controls=[preview_heading, preview_body, preview_meta],
            spacing=5,
            tight=True,
        ),
    )

    status_text = ft.Text("", size=12, color="#475569", selectable=True)

    def current_values() -> dict[str, str]:
        return {
            "heading_template": heading_field.value or "",
            **{
                field_name: field.value or ""
                for field_name, field in color_fields.items()
            },
        }

    def apply_to_fields(values: dict[str, str]) -> None:
        heading_field.value = values["heading_template"]
        for field_name, field in color_fields.items():
            field.value = values[field_name]

    def apply_to_preview(values: dict[str, str]) -> None:
        preview_card.bgcolor = values["background_color"]
        preview_card.border = ft.Border.all(1, values["border_color"])
        preview_heading.value = format_card_heading(
            values["heading_template"], _PREVIEW_RECORD
        )
        preview_heading.color = values["heading_color"]
        preview_body.value = (
            "Residential · Rate: 12,500 Per marla · "
            "Floor: All / not specified"
        )
        preview_body.color = values["body_color"]
        preview_meta.value = "urban · Lahore · Model Town · Sample Mauza"
        preview_meta.color = values["body_color"]

    def update_preview() -> None:
        try:
            values = normalize_card_appearance(current_values())
        except SettingsError as exc:
            status_text.value = str(exc)
            status_text.color = "#B91C1C"
        else:
            apply_to_preview(values)
            status_text.value = "Preview updated. Save to apply it to the rate list."
            status_text.color = "#475569"
        page.update()

    def save(_=None) -> None:
        try:
            saved = set_card_appearance(current_values())
        except SettingsError as exc:
            status_text.value = str(exc)
            status_text.color = "#B91C1C"
        except Exception as exc:  # The page must explain a persistence failure.
            status_text.value = f"Could not save rate-card appearance: {exc}"
            status_text.color = "#B91C1C"
        else:
            apply_to_fields(saved)
            apply_to_preview(saved)
            status_text.value = (
                "Rate-card appearance saved. Open Valuation → DC Rates "
                "to see it on the cards."
            )
            status_text.color = "#15803D"
        page.update()

    def restore_defaults(_=None) -> None:
        try:
            saved = reset_card_appearance()
        except Exception as exc:
            status_text.value = f"Could not reset rate-card appearance: {exc}"
            status_text.color = "#B91C1C"
        else:
            apply_to_fields(saved)
            apply_to_preview(saved)
            status_text.value = "Rate-card appearance reset to defaults."
            status_text.color = "#15803D"
        page.update()

    apply_to_preview(appearance)

    colour_fields_row = ft.ResponsiveRow(
        controls=[
            ft.Container(color_fields[field_name], col={"xs": 12, "sm": 6, "md": 3})
            for field_name in CARD_COLOR_FIELDS
        ],
        run_spacing=8,
    )

    editor_card = ft.Container(
        padding=18,
        bgcolor="#FFFFFF",
        border=ft.Border.all(1, "#E2E8F0"),
        border_radius=12,
        content=ft.Column(
            controls=[
                ft.Text(
                    "DC / FBR rate-card appearance",
                    size=18,
                    weight=ft.FontWeight.BOLD,
                ),
                ft.Text(
                    "Choose the card heading and colours used by the DC "
                    "Valuation Rates list. Defaults preserve the current cards.",
                    size=12,
                    color="#64748B",
                ),
                ft.Divider(height=1, color="#E2E8F0"),
                heading_field,
                _placeholder_help(),
                ft.Text(
                    "Example: {schedule} schedule · {location}",
                    size=11,
                    color="#64748B",
                    selectable=True,
                ),
                ft.Text("Colors", size=13, weight=ft.FontWeight.BOLD),
                colour_fields_row,
                ft.Text("Preview", size=13, weight=ft.FontWeight.BOLD),
                preview_card,
                ft.Row(
                    controls=[
                        ft.Button("Save appearance", on_click=save),
                        ft.OutlinedButton(
                            "Reset to defaults", on_click=restore_defaults
                        ),
                    ],
                    spacing=8,
                    wrap=True,
                ),
                status_text,
            ],
            spacing=10,
            tight=True,
        ),
    )

    return ft.Column(
        controls=[
            ft.Text("Settings", size=25, weight=ft.FontWeight.BOLD),
            ft.Text(
                "Application preferences. Settings are stored in this "
                "application's database and persist between launches.",
                color="#64748B",
            ),
            location_card,
            editor_card,
        ],
        spacing=12,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
    )
