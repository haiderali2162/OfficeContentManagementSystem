"""Application settings pages.

This module exposes the DC/FBR rate-card appearance editor and the default
import/export folder preference. Values are persisted through
``services.settings_service`` and applied the next time the relevant page is
built.

It also exposes the database-connection editor (``build_database_section``):
the user can point the running application at SQLite, PostgreSQL,
MySQL/MariaDB, Microsoft SQL Server, or a custom SQLAlchemy URL. The choice
is stored in ``data/database.json`` (outside the database) through
``core.database_config``; a successful "Save & connect" swaps the live engine
without restarting the app.
"""

from __future__ import annotations

import asyncio

import flet as ft

from core import database_config
from core.database import initialize_database, reconfigure_database
from services.settings_service import (
    CARD_BODY_FIELD_LABELS,
    CARD_COLOR_FIELDS,
    CARD_COLOR_LABELS,
    CARD_HEADING_FIELDS,
    DEFAULT_CARD_APPEARANCE,
    FORMULA_FIELDS,
    SettingsError,
    format_card_body_lines,
    format_card_heading,
    get_card_appearance,
    get_card_fields,
    get_default_file_location,
    normalize_card_appearance,
    normalize_card_fields,
    reset_card_appearance,
    reset_card_fields,
    set_card_appearance,
    set_card_fields,
    set_default_file_location,
)


#: Used by the live previews; the rate keys give the formula editor a
#: realistic per-marla value to compute from.
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
    "rate_value": "12500.25",
    "rate_unit": "per_marla",
    "serial_no": "45",
    "hadbast_number": "123",
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


def build_database_section(page: ft.Page, file_picker: ft.FilePicker) -> ft.Container:
    """Build the database-connection editor card.

    The card lets the user choose a backend and its connection details,
    verify them with "Test connection", and then apply them live with
    "Save & connect". Details:

    * Options are persisted by :func:`core.database_config.save_options` into
      ``data/database.json`` — deliberately outside the database itself,
      because the file decides *which* database to open.
    * An empty password field keeps the previously saved (encrypted)
      password, so editing other fields cannot silently drop it.
    * "Save & connect" tests first, saves only on success, swaps the live
      engine via :func:`core.database.reconfigure_database`, and creates the
      application tables in an empty database via
      :func:`core.database.initialize_database`.
    * **No data is copied** between databases; the previous database stays
      untouched and the card states this next to the buttons.
    """
    saved_options = database_config.load_options()

    backend_dropdown = ft.Dropdown(
        label="Database type",
        value=saved_options["backend"],
        options=[
            ft.dropdown.Option(
                key, text=database_config.backend_label(key)
            )
            for key in database_config.backend_keys()
        ],
    )
    db_status = ft.Text("", size=12, color="#475569", selectable=True)

    # One editor control per stored option. The password field starts empty
    # on purpose: stored passwords are encrypted on disk and are never shown.
    db_fields: dict[str, ft.TextField] = {
        "host": ft.TextField(
            label="Server host", value=saved_options["host"], expand=True
        ),
        "port": ft.TextField(
            label="Port (optional)", value=saved_options["port"], width=160
        ),
        "database": ft.TextField(
            label="Database name", value=saved_options["database"], expand=True
        ),
        "user": ft.TextField(
            label="User", value=saved_options["user"], expand=True
        ),
        "password": ft.TextField(
            label="Password (leave empty to keep the saved one)",
            value="",
            password=True,
            can_reveal_password=True,
            expand=True,
        ),
        "driver": ft.TextField(
            label="Driver (leave empty to auto-select)",
            value=saved_options["driver"],
            expand=True,
        ),
        "url": ft.TextField(
            label="SQLAlchemy URL",
            value=saved_options["url"],
            hint_text="postgresql+psycopg2://user:pass@host:5432/landoffice",
            expand=True,
        ),
    }
    path_field = ft.TextField(
        label="SQLite database file",
        value=saved_options["path"],
        expand=True,
    )
    odbc_driver_field = ft.TextField(
        label="ODBC driver (for example: ODBC Driver 17 for SQL Server)",
        expand=True,
    )

    async def browse_database_file(_=None):
        """Fill the SQLite path field from the native file picker.

        ``save_file`` is used because a new database file may not exist yet;
        if the user picks an existing name it is confirmed through the
        ``Test connection`` button before anything is saved.
        """
        if file_picker is None:
            return
        try:
            picked = await file_picker.save_file(
                dialog_title="Choose or name the SQLite database file",
                file_name="land_office.sqlite3",
                initial_directory=path_field.value or None,
            )
        except Exception as exc:
            db_status.value = f"Could not open the file picker: {exc}"
            db_status.color = "#B91C1C"
            page.update()
            return
        if picked:
            path_field.value = picked
            db_status.value = (
                "Database file chosen. Use Test connection or Save & "
                "connect to apply it."
            )
            db_status.color = "#475569"
            page.update()

    def collect_options() -> dict:
        """Gather the edited fields on top of the currently saved options.

        Starting from the saved options preserves anything the editor does
        not show (most importantly the encrypted password) and keeps the
        request self-contained for validation, testing, saving, and
        connecting.
        """
        options = dict(database_config.load_options())
        options["backend"] = backend_dropdown.value
        for field_name, field in db_fields.items():
            options[field_name] = field.value or ""
        options["path"] = path_field.value or ""
        options["odbc_driver"] = odbc_driver_field.value or ""
        # An empty password field means "keep the saved encrypted password";
        # only a typed value replaces it.
        if not db_fields["password"].value:
            options.pop("password", None)
        return options

    def sync_backend_visibility(_=None) -> None:
        """Show only the detail fields the selected backend actually uses."""
        backend = backend_dropdown.value
        path_row.visible = backend == "sqlite"
        sqlite_hint.visible = backend == "sqlite"
        url_row.visible = backend == "custom"
        server_fields.visible = backend in ("postgres", "mysql", "mssql")
        driver_row.visible = backend in ("postgres", "mysql", "mssql")
        odbc_row.visible = backend == "mssql"
        db_status.value = ""
        page.update()

    async def test_connection_click(_=None) -> None:
        """Run a connection attempt with the edited options and report."""
        db_status.value = "Testing connection…"
        db_status.color = "#475569"
        page.update()
        try:
            ok, message = await asyncio.to_thread(
                database_config.test_connection, collect_options()
            )
        except Exception as exc:  # defensive: message must always surface
            ok, message = False, f"Test failed unexpectedly: {exc}"
        db_status.value = message
        db_status.color = "#15803D" if ok else "#B91C1C"
        page.update()

    async def save_and_connect_click(_=None) -> None:
        """Validate, save, and switch the running app to the new database."""
        options = collect_options()
        db_status.value = "Validating and connecting…"
        db_status.color = "#475569"
        page.update()
        try:
            # Validation first so obviously incomplete data never reaches a
            # connect attempt or the saved configuration file.
            database_config.normalize_options(options)
            ok, message = await asyncio.to_thread(
                database_config.test_connection, options
            )
            if not ok:
                raise database_config.DatabaseConfigError(message)
            await asyncio.to_thread(database_config.save_options, options)
            url = await asyncio.to_thread(database_config.build_url, options)
            await asyncio.to_thread(reconfigure_database, url)
            # Create any application tables missing from an empty database.
            await asyncio.to_thread(initialize_database)
        except database_config.DatabaseConfigError as exc:
            db_status.value = f"Not connected: {exc}"
            db_status.color = "#B91C1C"
        except Exception as exc:
            db_status.value = f"Could not switch to the new database: {exc}"
            db_status.color = "#B91C1C"
        else:
            db_status.value = (
                f"Connected to {database_config.backend_label(options['backend'])}. "
                "The previous database was not changed and no data was copied "
                "between databases."
            )
            db_status.color = "#15803D"
        page.update()

    backend_dropdown.on_change = sync_backend_visibility

    #: Hint shown only for SQLite; the app ships with a SQLite database file.
    sqlite_hint = ft.Text(
        "The bundled SQLite database is data/land_office.sqlite3. Choosing "
        "another file switches to an empty database (tables are created "
        "automatically); the existing one stays unchanged.",
        size=11,
        color="#64748B",
        selectable=True,
    )
    path_row = ft.Row(
        controls=[
            path_field,
            ft.OutlinedButton("Browse file", on_click=browse_database_file),
        ],
        spacing=8,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )
    url_row = ft.Row(
        controls=[db_fields["url"]],
        spacing=8,
    )
    server_fields = ft.Row(
        controls=[
            db_fields["host"],
            db_fields["port"],
            db_fields["database"],
        ],
        spacing=8,
        wrap=True,
    )
    credentials_row = ft.Row(
        controls=[
            db_fields["user"],
            db_fields["password"],
        ],
        spacing=8,
        wrap=True,
    )
    driver_row = ft.Row(
        controls=[db_fields["driver"]],
        spacing=8,
        wrap=True,
    )
    odbc_row = ft.Row(
        controls=[odbc_driver_field],
        spacing=8,
        wrap=True,
    )

    card = ft.Container(
        padding=18,
        bgcolor="#FFFFFF",
        border=ft.Border.all(1, "#E2E8F0"),
        border_radius=12,
        content=ft.Column(
            controls=[
                ft.Text("Database", size=18, weight=ft.FontWeight.BOLD),
                ft.Text(
                    "Choose which database the application uses. SQLite "
                    "works out of the box; other servers need their Python "
                    "driver installed (pip install psycopg2-binary, pymysql, "
                    "or pyodbc). The choice is saved in data/database.json "
                    "and works the same on Windows, macOS, and Linux.",
                    size=12,
                    color="#64748B",
                ),
                ft.Divider(height=1, color="#E2E8F0"),
                backend_dropdown,
                path_row,
                sqlite_hint,
                url_row,
                server_fields,
                credentials_row,
                driver_row,
                odbc_row,
                ft.Text(
                    "Saving does not copy any data: the new database must "
                    "already hold what you need (an empty database gets the "
                    "application tables created automatically).",
                    size=11,
                    color="#92400E",
                    selectable=True,
                ),
                ft.Row(
                    controls=[
                        ft.Button("Save & connect", on_click=save_and_connect_click),
                        ft.OutlinedButton(
                            "Test connection", on_click=test_connection_click
                        ),
                    ],
                    spacing=8,
                    wrap=True,
                ),
                db_status,
            ],
            spacing=10,
            tight=True,
        ),
    )
    sync_backend_visibility(None)
    return card


def _adopted_file_picker(page: ft.Page) -> ft.FilePicker:
    """Return the page's shared FilePicker, creating one when absent.

    Every settings section that opens the native folder picker reuses the
    same control, because Flet registers one picker per service instance.
    """
    file_picker = ft.FilePicker()
    existing_picker = next(
        (service for service in page.services if isinstance(service, ft.FilePicker)),
        None,
    )
    if existing_picker is None:
        page.services.append(file_picker)
        return file_picker
    return existing_picker


def build_settings_page(page: ft.Page) -> ft.Control:
    """Build the settings page with rate-card and file-location preferences."""
    appearance = get_card_appearance()
    database_card = build_database_section(
        page, _adopted_file_picker(page)
    )

    file_picker = ft.FilePicker()
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

    # -------------------------------------------------------------------
    # Rate-card CONTENT editor: which body fields are shown and optional
    # formula lines (e.g. "{rate_value} * 20" for a per-kanal equivalent).
    # An empty configuration keeps the built-in card layout, matching
    # services.settings_service.CARD_FIELDS_KEY documentation.
    # -------------------------------------------------------------------
    saved_content = get_card_fields()
    content_checkboxes = {
        field_name: ft.Checkbox(
            label=label,
            value=field_name in saved_content["fields"],
        )
        for field_name, label in CARD_BODY_FIELD_LABELS.items()
    }
    # Formula rows are plain Python state; delete/add reuses the saved values.
    content_formulas = [
        dict(formula) for formula in saved_content["formulas"]
    ]
    content_status = ft.Text("", size=12, color="#475569", selectable=True)

    def content_formula_rows() -> list[ft.Row]:
        """Create one row of controls per formula line (label + expression)."""
        rows = []
        for index, formula in enumerate(content_formulas):
            label_field = ft.TextField(
                label=f"Formula {index + 1} label",
                value=formula.get("label", ""),
                expand=True,
            )
            expression_field = ft.TextField(
                label="Expression",
                value=formula.get("expression", ""),
                hint_text="{rate_value} * 20",
                expand=True,
            )

            def watch(_=None):
                update_content_preview()

            label_field.on_change = watch
            expression_field.on_change = watch

            def remove(_=None, _index=index):
                content_formulas.pop(_index)
                rebuild_content_formula_rows()

            rows.append(ft.Row(
                controls=[
                    label_field,
                    expression_field,
                    ft.TextButton("Remove", on_click=remove),
                ],
                spacing=8,
                wrap=True,
            ))
        return rows

    def rebuild_content_formula_rows() -> None:
        """Re-render the formula editor rows after add/remove."""
        formula_editor.controls = content_formula_rows() or [
            ft.Text(
                "No formula lines. Formulas turn schedule numbers into "
                "derived values, for example {rate_value} * 20 for a "
                "per-kanal equivalent.",
                size=12,
                color="#64748B",
            ),
        ]
        page.update()

    def add_content_formula(_=None) -> None:
        content_formulas.append({"label": "", "expression": ""})
        rebuild_content_formula_rows()

    def apply_content_to_controls(values: dict) -> None:
        """Load a saved configuration back into the editor controls."""
        for field_name, checkbox in content_checkboxes.items():
            checkbox.value = field_name in values["fields"]
        content_formulas.clear()
        content_formulas.extend(dict(f) for f in values["formulas"])
        rebuild_content_formula_rows()

    def current_content_values() -> dict:
        return {
            "fields": [
                field_name
                for field_name, checkbox in content_checkboxes.items()
                if checkbox.value
            ],
            "formulas": [
                {
                    "label": row.controls[0].value or "",
                    "expression": row.controls[1].value or "",
                }
                for row in formula_editor.controls
                if isinstance(row, ft.Row)
            ],
        }

    def update_content_preview() -> None:
        """Validate the editor contents and refresh the mini preview card."""
        try:
            values = normalize_card_fields(current_content_values())
        except SettingsError as exc:
            content_status.value = str(exc)
            content_status.color = "#B91C1C"
            page.update()
            return
        preview_lines = format_card_body_lines(_PREVIEW_RECORD, values)
        content_preview.controls = (
            [
                ft.Text(
                    format_card_heading(
                        appearance["heading_template"], _PREVIEW_RECORD
                    ),
                    weight=ft.FontWeight.BOLD,
                    size=14,
                    color=appearance["heading_color"],
                ),
                *[ft.Text(line, size=12, color=appearance["body_color"],
                          selectable=True) for line in preview_lines],
            ]
            or [ft.Text(
                "Empty configuration: cards keep the built-in layout.",
                size=12, color="#64748B",
            )]
        )
        content_status.value = "Preview updated. Save to apply it to the rate list."
        content_status.color = "#475569"
        page.update()

    def save_content(_=None) -> None:
        try:
            saved = set_card_fields(current_content_values())
        except SettingsError as exc:
            content_status.value = str(exc)
            content_status.color = "#B91C1C"
        except Exception as exc:  # The page must explain a persistence failure.
            content_status.value = f"Could not save card content: {exc}"
            content_status.color = "#B91C1C"
        else:
            apply_content_to_controls(saved)
            content_status.value = (
                "Card content saved. Open Valuation → DC Rates to see it on "
                "the cards."
            )
            content_status.color = "#15803D"
        page.update()

    def reset_content(_=None) -> None:
        try:
            saved = reset_card_fields()
        except Exception as exc:
            content_status.value = f"Could not reset card content: {exc}"
            content_status.color = "#B91C1C"
        else:
            apply_content_to_controls(saved)
            content_status.value = "Card content reset to the built-in layout."
            content_status.color = "#15803D"
        page.update()

    def _checkbox_groups(group_size: int = 6):
        """Split the offered card fields into wrap-friendly checkbox rows."""
        names = list(CARD_BODY_FIELD_LABELS)
        return [
            names[start:start + group_size]
            for start in range(0, len(names), group_size)
        ]

    field_checkboxes_row = ft.Column(
        controls=[
            ft.Row(
                controls=[
                    content_checkboxes[field_name]
                    for field_name in group
                ],
                wrap=True,
                spacing=12,
            )
            for group in _checkbox_groups()
        ],
        spacing=4,
        tight=True,
    )
    formula_editor = ft.Column(
        controls=content_formula_rows() or [],
        spacing=8,
        tight=True,
    )
    content_preview = ft.Column(
        controls=[],
        spacing=4,
        tight=True,
    )
    formula_help = ft.Text(
        "Formula placeholders: " + ", ".join(
            f"{{{name}}}" for name in FORMULA_FIELDS
        ) + " · operations: + - * / ( ) · results show up to 4 decimals.",
        size=11,
        color="#64748B",
        selectable=True,
    )
    content_card = ft.Container(
        padding=18,
        bgcolor="#FFFFFF",
        border=ft.Border.all(1, "#E2E8F0"),
        border_radius=12,
        content=ft.Column(
            controls=[
                ft.Text(
                    "DC / FBR rate-card content",
                    size=18,
                    weight=ft.FontWeight.BOLD,
                ),
                ft.Text(
                    "Choose what each schedule card shows. With nothing "
                    "selected the card keeps its built-in layout. Formula "
                    "lines calculate values from the schedule numbers, for "
                    "example a per-kanal equivalent of a per-marla rate.",
                    size=12,
                    color="#64748B",
                ),
                ft.Divider(height=1, color="#E2E8F0"),
                ft.Text("Shown information", size=13, weight=ft.FontWeight.BOLD),
                field_checkboxes_row,
                ft.Text("Formula lines", size=13, weight=ft.FontWeight.BOLD),
                formula_help,
                formula_editor,
                ft.OutlinedButton("Add formula line", on_click=add_content_formula),
                ft.Text("Preview", size=13, weight=ft.FontWeight.BOLD),
                content_preview,
                ft.Row(
                    controls=[
                        ft.Button("Save content", on_click=save_content),
                        ft.OutlinedButton(
                            "Reset to built-in layout", on_click=reset_content
                        ),
                    ],
                    spacing=8,
                    wrap=True,
                ),
                content_status,
            ],
            spacing=10,
            tight=True,
        ),
    )
    apply_content_to_controls(saved_content)
    update_content_preview()

    return ft.Column(
        controls=[
            ft.Text("Settings", size=25, weight=ft.FontWeight.BOLD),
            ft.Text(
                "Application preferences. Settings are stored in this "
                "application's database and persist between launches.",
                color="#64748B",
            ),
            database_card,
            location_card,
            editor_card,
            content_card,
        ],
        spacing=12,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
    )
