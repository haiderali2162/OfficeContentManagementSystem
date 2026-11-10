"""Application settings pages.

The settings page is organised into collapsible categories (Flet
``ExpansionTile``): Database, Files, Rate-card appearance, and Rate-card
content. Values are persisted through ``services.settings_service`` and,
for the database choice, ``core.database_config``.

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
# Imported as a module (not ``from ... import engine``) so the live engine is
# looked up at save time: ``reconfigure_database`` reassigns the module's
# ``engine`` global after a successful switch to another database.
from core import database as core_database
from core.database import initialize_database, reconfigure_database
from core.database_copy import DatabaseCopyError, copy_sqlite_database
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


def _category_tile(
    title: str,
    summary: str,
    content: ft.Control,
    *,
    expanded: bool = False,
) -> ft.Control:
    """Wrap one settings category in a collapsible white tile.

    Categories use Flet's :class:`ft.ExpansionTile`: a collapsed tile shows
    only the title and one-line summary, keeping the page short, and the
    category's controls appear only while it is expanded. The white card
    styling (rounded corners, hairline border) matches the rest of the app
    so one settings page looks consistent.

    Args:
        title: short category name shown in the tile header.
        summary: one-line explanation shown under the title when collapsed.
        content: the section's controls (usually a tight ``ft.Column``).
        expanded: initial state; used to open the most-used category.
    """
    return ft.Container(
        bgcolor="#FFFFFF",
        border=ft.Border.all(1, "#E2E8F0"),
        border_radius=12,
        content=ft.ExpansionTile(
            title=ft.Text(
                title, size=16, weight=ft.FontWeight.BOLD, color="#172033"
            ),
            subtitle=ft.Text(summary, size=12, color="#64748B"),
            controls=[content],
            expanded=expanded,
            # Padding around the clickable header and, once expanded, the
            # section body. Horizontal alignment START keeps inner controls
            # anchored to the left instead of Flet's default centering.
            tile_padding=ft.Padding(left=16, right=16, top=6, bottom=10),
            controls_padding=10,
            expanded_cross_axis_alignment=ft.CrossAxisAlignment.START,
            bgcolor="#FFFFFF",
            collapsed_bgcolor="#FFFFFF",
            icon_color="#334155",
            collapsed_icon_color="#334155",
        ),
    )


def build_database_section(page: ft.Page, file_picker: ft.FilePicker) -> ft.Column:
    """Build the database-connection editor controls.

    Returns the section body as a plain :class:`ft.Column`; the caller wraps
    it in a collapsible category tile (see :func:`_category_tile`).

    The section lets the user choose a backend and its connection details,
    verify them with "Test connection", and then apply them live with
    "Save & connect". Details:

    * Options are persisted by :func:`core.database_config.save_options` into
      ``data/database.json`` — deliberately outside the database itself,
      because the file decides *which* database to open.
    * An empty password field keeps the previously saved (encrypted)
      password, so editing other fields cannot silently drop it.
    * **Opt-in data copy (SQLite only):** when the "Copy current application
      data" checkbox is ticked, the currently connected SQLite database is
      snapshotted to the chosen file *before* the switch (see
      :mod:`core.database_copy`). That file's previous content, if any, is
      replaced. Unticked, nothing is copied and the new database starts
      empty.
    * "Save & connect" tests first, saves only on success, swaps the live
      engine via :func:`core.database.reconfigure_database`, and creates the
      application tables in an empty database via
      :func:`core.database.initialize_database`.
    * A failed copy blocks the switch: the previous database stays connected
      and the saved configuration is untouched.
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
    # Server fields get explicit widths instead of expand=True: expand inside
    # a wrapped row is a known Flet layout hazard (fields can grow to huge
    # empty areas), so widths keep the row layout predictable on any OS.
    db_fields: dict[str, ft.TextField] = {
        "host": ft.TextField(
            label="Server host", value=saved_options["host"], width=280
        ),
        "port": ft.TextField(
            label="Port (optional)", value=saved_options["port"], width=130
        ),
        "database": ft.TextField(
            label="Database name", value=saved_options["database"], width=280
        ),
        "user": ft.TextField(
            label="User", value=saved_options["user"], width=280
        ),
        "password": ft.TextField(
            label="Password (leave empty to keep the saved one)",
            value="",
            password=True,
            can_reveal_password=True,
            width=300,
        ),
        "driver": ft.TextField(
            label="Driver (leave empty to auto-select)",
            value=saved_options["driver"],
            width=380,
        ),
        "url": ft.TextField(
            label="SQLAlchemy URL",
            value=saved_options["url"],
            hint_text="postgresql+psycopg2://user:pass@host:5432/landoffice",
            expand=True,
        ),
    }
    # Separate from the field dict: clearing is an explicit user decision, so
    # an untouched password never accidentally disappears from the config.
    clear_password_checkbox = ft.Checkbox(
        label="Clear the saved password",
        value=False,
    )
    # Opt-in whole-file copy for SQLite switches. Unticked (the default) the
    # new database starts empty; ticked, the currently connected SQLite file
    # is snapshotted into the chosen file before the app switches to it.
    copy_data_checkbox = ft.Checkbox(
        label="Copy current application data to this SQLite file",
        value=False,
    )
    path_field = ft.TextField(
        label="SQLite database file",
        value=saved_options["path"],
        expand=True,
    )
    odbc_driver_field = ft.TextField(
        label="ODBC driver (for example: ODBC Driver 17 for SQL Server)",
        width=460,
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
            # The password is special-cased below: putting the empty field
            # value here would wipe the saved encrypted token.
            if field_name == "password":
                continue
            options[field_name] = field.value or ""
        if db_fields["password"].value:
            options["password"] = db_fields["password"].value
        options["path"] = path_field.value or ""
        options["odbc_driver"] = odbc_driver_field.value or ""
        # The explicit checkbox is the only way to clear a saved password;
        # a field left empty always keeps the previously stored token.
        if clear_password_checkbox.value:
            options["password"] = ""
        return options

    def sync_backend_visibility(_=None) -> None:
        """Show only the detail fields the selected backend actually uses."""
        backend = backend_dropdown.value
        path_row.visible = backend == "sqlite"
        sqlite_hint.visible = backend == "sqlite"
        # Only whole-file SQLite switches can copy data; server databases
        # start empty and the checkbox would be a misleading promise.
        copy_data_checkbox.visible = backend == "sqlite"
        copy_hint.visible = backend == "sqlite"
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
        """Validate, optionally copy data, save, and switch the database."""
        options = collect_options()
        db_status.value = "Validating and connecting…"
        db_status.color = "#475569"
        page.update()
        # Tick the checkbox (SQLite only) and the currently connected SQLite
        # database is snapshotted into the chosen file before the switch.
        copy_requested = (
            options["backend"] == "sqlite" and copy_data_checkbox.value
        )
        try:
            # Validation first so obviously incomplete data never reaches a
            # connect attempt or the saved configuration file.
            database_config.normalize_options(options)
            if copy_requested:
                db_status.value = "Copying current data to the new file…"
                page.update()
                await asyncio.to_thread(
                    copy_sqlite_database, core_database.engine, options["path"]
                )
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
        except DatabaseCopyError as exc:
            db_status.value = f"Data was not copied: {exc}"
            db_status.color = "#B91C1C"
        except database_config.DatabaseConfigError as exc:
            db_status.value = f"Not connected: {exc}"
            db_status.color = "#B91C1C"
        except Exception as exc:
            db_status.value = f"Could not switch to the new database: {exc}"
            db_status.color = "#B91C1C"
        else:
            if copy_requested:
                db_status.value = (
                    f"Connected to {database_config.backend_label(options['backend'])}. "
                    "The data from the previous database was copied into "
                    "this file before the switch."
                )
            else:
                db_status.value = (
                    f"Connected to {database_config.backend_label(options['backend'])}. "
                    "The previous database was not changed and no data was "
                    "copied between databases."
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
            clear_password_checkbox,
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
    # Explains exactly what the copy checkbox does and warns that a chosen
    # file's previous content is replaced. Visible only for SQLite together
    # with the checkbox itself.
    copy_hint = ft.Text(
        "Ticked: the currently connected SQLite database is fully copied "
        "into the chosen file before the switch; anything already in that "
        "file is replaced. Unticked: the new file starts empty.",
        size=11,
        color="#92400E",
        selectable=True,
    )

    card = ft.Column(
        controls=[
            ft.Text(
                "Choose which database the application uses. SQLite "
                "works out of the box; other servers need their Python "
                "driver installed (pip install psycopg2-binary, pymysql, "
                "or pyodbc). The choice is saved in data/database.json "
                "and works the same on Windows, macOS, and Linux.",
                size=12,
                color="#64748B",
            ),
            backend_dropdown,
            path_row,
            sqlite_hint,
            copy_data_checkbox,
            copy_hint,
            url_row,
            server_fields,
            credentials_row,
            driver_row,
            odbc_row,
            ft.Text(
                "By default the new database starts empty (tables are "
                "created automatically). For SQLite you can choose to copy "
                "the current data with the checkbox above; server "
                "databases always start empty.",
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
    """Build the settings page as collapsible categories.

    Categories (see :func:`_category_tile`):

    * **Database** — backend choice and live connection (open by default).
    * **Files** — the default import/export folder preference.
    * **Rate cards — appearance** — heading template and colours.
    * **Rate cards — shown fields and formulas** — card content editor.

    Each category owns its own status messages: opening one never disturbs
    another, and collapsed categories stay collapsed while other controls
    repaint themselves.
    """
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

    location_card = ft.Column(
        controls=[
            ft.Text(
                "When a default folder is set, the app asks before every "
                "import and export whether to use it or choose another "
                "location. Leave it empty to always choose a location.",
                size=12,
                color="#64748B",
            ),
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

    editor_card = ft.Column(
        controls=[
            ft.Text(
                "Choose the card heading and colours used by the DC "
                "Valuation Rates list. Defaults preserve the current cards.",
                size=12,
                color="#64748B",
            ),
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
    content_card = ft.Column(
        controls=[
            ft.Text(
                "Choose what each schedule card shows. With nothing "
                "selected the card keeps its built-in layout. Formula "
                "lines calculate values from the schedule numbers, for "
                "example a per-kanal equivalent of a per-marla rate.",
                size=12,
                color="#64748B",
            ),
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
    )
    apply_content_to_controls(saved_content)
    update_content_preview()

    guide_card = ft.Column(
        controls=[
            ft.Text(
                "Guide & Reference: How to write and use Custom Registry Fee Formulas in the Land Office application.",
                size=13,
                weight=ft.FontWeight.BOLD,
                color="#1E293B",
            ),
            ft.Text(
                "Custom formulas allow calculating dynamic registry expenses, stamp duties, PLRA fees, and mutation charges "
                "based on property valuation and land area.",
                size=12,
                color="#64748B",
            ),
            ft.Container(
                ft.Column([
                    ft.Text("1. Available Variables", size=13, weight=ft.FontWeight.BOLD, color="#334155"),
                    ft.Text("• area: Area in Marlas", size=12),
                    ft.Text("• area_marla: Explicit area in Marlas", size=12),
                    ft.Text("• area_kanal: Area in Kanals (1 Kanal = 20 Marlas)", size=12),
                    ft.Text("• area_acre: Area in Acres (1 Acre = 8 Kanals / 160 Marlas)", size=12),
                    ft.Text("• area_square_feet: Total land area in square feet", size=12),
                    ft.Text("• dc_value: Calculated DC Valuation (PKR)", size=12),
                    ft.Text("• fbr_value: Calculated FBR Valuation (PKR)", size=12),
                    ft.Text("• property_value: Full calculated property value (PKR)", size=12),
                    ft.Text("• transferred_value: Transferred consideration value (PKR)", size=12),
                    ft.Text("• registry_written_value: Declared/written deed amount (PKR)", size=12),
                    ft.Text("• building_material_value: Constructed structure value (PKR)", size=12),
                    ft.Text("• Custom fields: Any custom numeric field defined under Manage custom fields (e.g. plot_count, surcharge)", size=12, italic=True),
                ], spacing=4),
                padding=10, bgcolor="#F8FAFC", border=ft.Border.all(1, "#E2E8F0"), border_radius=8,
            ),
            ft.Container(
                ft.Column([
                    ft.Text("2. Operators & Syntax", size=13, weight=ft.FontWeight.BOLD, color="#334155"),
                    ft.Text("• Arithmetic: + (addition), - (subtraction), * (multiplication), / (division)", size=12),
                    ft.Text("• Grouping: Parentheses ( ) to group operations, e.g. (dc_value + 1000) * 2%", size=12),
                    ft.Text("• Percentages: Use % sign directly (e.g. 2.5%, 0.1%, 1%), equivalent to multiplying by rate / 100", size=12),
                    ft.Text("• Slabs / Conditionals: Python ternary syntax is supported:", size=12),
                    ft.Text("    fixed_amount if condition else rate_or_formula", size=12, color="#0369A1"),
                    ft.Text("    Example: 3300 if dc_value < 3000000 else dc_value * 0.1%", size=12, color="#0369A1"),
                    ft.Text("• Capping: min(value, cap) or max(value, floor)", size=12),
                ], spacing=4),
                padding=10, bgcolor="#F8FAFC", border=ft.Border.all(1, "#E2E8F0"), border_radius=8,
            ),
            ft.Container(
                ft.Column([
                    ft.Text("3. Step-by-Step Practical Examples", size=13, weight=ft.FontWeight.BOLD, color="#334155"),
                    ft.Text("Example A — Percentage with Base Surcharge:", weight=ft.FontWeight.W_500, size=12),
                    ft.Text("    transferred_value * 2% + 500", size=12, color="#0F766E"),
                    ft.Text("Example B — Per-Kanal Rate with Flat Registration Fee:", weight=ft.FontWeight.W_500, size=12),
                    ft.Text("    area_kanal * 1200 + 2000", size=12, color="#0F766E"),
                    ft.Text("Example C — Surcharge with Maximum Cap (e.g. Capped at 50,000 PKR):", weight=ft.FontWeight.W_500, size=12),
                    ft.Text("    min(transferred_value * 1%, 50000)", size=12, color="#0F766E"),
                    ft.Text("Example D — PLRA Official Fee Slab:", weight=ft.FontWeight.W_500, size=12),
                    ft.Text("    3300 if dc_value < 3000000 else dc_value * 0.1%", size=12, color="#0F766E"),
                ], spacing=4),
                padding=10, bgcolor="#F8FAFC", border=ft.Border.all(1, "#E2E8F0"), border_radius=8,
            ),
            ft.Container(
                ft.Column([
                    ft.Text("4. Eligibility Conditions (Optional JSON)", size=13, weight=ft.FontWeight.BOLD, color="#334155"),
                    ft.Text("Formulas can be limited to specific locations or transactions by setting conditions in JSON:", size=12),
                    ft.Text("    {\"when\": {\"mauza\": \"Chak 1\", \"transaction_type\": \"sale\"}}", size=12, color="#B45309"),
                    ft.Text("Leave blank to apply to all calculations in the jurisdiction.", size=12, color="#64748B"),
                ], spacing=4),
                padding=10, bgcolor="#F8FAFC", border=ft.Border.all(1, "#E2E8F0"), border_radius=8,
            ),
        ],
        spacing=10,
        tight=True,
    )

    return ft.Column(
        controls=[
            ft.Text("Settings", size=25, weight=ft.FontWeight.BOLD),
            ft.Text(
                "Application preferences in collapsible categories. Open a "
                "category, change what you need; saved preferences keep "
                "between launches.",
                color="#64748B",
            ),
            _category_tile(
                "Database",
                "SQLite file, PostgreSQL, MySQL, SQL Server, or a custom "
                "URL — applied live with Save & connect.",
                database_card,
                # Opened by default so the newest settings area is visible.
                expanded=True,
            ),
            _category_tile(
                "Files — default import / export folder",
                "Where exports go by default, and whether the app asks "
                "before every import/export action.",
                location_card,
            ),
            _category_tile(
                "Rate cards — appearance",
                "Card heading text and the colours used by the DC/FBR "
                "rate list.",
                editor_card,
            ),
            _category_tile(
                "Rate cards — shown fields and formulas",
                "Which schedule fields appear on each card, plus formula "
                "lines such as a per-kanal equivalent.",
                content_card,
            ),
            _category_tile(
                "Custom registry formulas — user guide",
                "How to write formula expressions, available variables (area, DC/FBR values), percentage syntax, and conditional slabs.",
                guide_card,
            ),
        ],
        spacing=12,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
    )
