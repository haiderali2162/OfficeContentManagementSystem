"""DC valuation rate management UI and Excel import workflow."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import flet as ft

from core.models import DC_RATE_UNITS
from services.dc_rate_service import (
    DCRateServiceError,
    create_dc_rate,
    create_location,
    export_schedule_records,
    get_dc_rate,
    import_dc_rate_records,
    list_dc_rates,
    list_locations,
    preview_dc_rate_workbook,
    preview_mapped_schedule,
    read_schedule_spreadsheet,
    suggested_column_mapping,
    update_dc_rate,
)


UNIT_LABELS = {
    "per_square_foot": "Per square foot",
    "per_square_yard": "Per square yard",
    "per_square_meter": "Per square meter",
    "per_marla": "Per marla",
    "per_kanal": "Per kanal",
    "per_acre": "Per acre",
    "per_kila": "Per kila",
    "per_plot": "Per plot",
}


def _format_date(value: date | None, *, empty: str = "Not supplied") -> str:
    return value.isoformat() if value else empty


def build_dc_rates_page(page: ft.Page, *, defer_initial_load: bool = False) -> ft.Control:
    """Build rate controls and connect UI events to the rate service."""
    file_picker = ft.FilePicker()
    existing_picker = next(
        (service for service in page.services if isinstance(service, ft.FilePicker)),
        None,
    )
    if existing_picker is None:
        page.services.append(file_picker)
    else:
        file_picker = existing_picker
    import_in_progress = False
    visible_rate_limit = 200
    visible_location_limit = 500
    location_picker_truncated = False

    mauza_filter = ft.TextField(label="Mauza", width=190)
    square_filter = ft.TextField(label="Square no.", width=130)
    kila_filter = ft.TextField(label="Kila no.", width=130)
    search_field = ft.TextField(
        label="Search location, property, classification, or source",
        prefix_icon=getattr(ft.Icons, "SEARCH", None),
        width=300,
    )
    location_filter = ft.Dropdown(
        label="Location",
        width=220,
        value="all",
        options=[ft.dropdown.Option("all", "All locations")],
    )
    unit_filter = ft.Dropdown(
        label="Rate unit",
        width=190,
        value="all",
        options=[
            ft.dropdown.Option("all", "All units"),
            *[
                ft.dropdown.Option(unit, UNIT_LABELS[unit])
                for unit in DC_RATE_UNITS
            ],
        ],
    )
    schedule_filter = ft.Dropdown(
        label="Schedule", width=145, value="all",
        options=[ft.dropdown.Option("dc", "DC"),
                 ft.dropdown.Option("fbr", "FBR"),
                 ft.dropdown.Option("all", "Both")],
    )
    published_year_filter = ft.TextField(
        label="Published year (optional)",
        value="",
        width=165,
    )
    status_text = ft.Text("", size=12, color="#475569")
    result_count = ft.Text("", size=12, color="#64748B")
    rate_list = ft.ListView(expand=True, spacing=8, padding=4)

    def close_dialog():
        page.pop_dialog()

    def refresh_locations(locations=None):
        """Reload location choices used by the filter and rate editor."""
        nonlocal location_picker_truncated
        locations = list_locations() if locations is None else locations
        visible_locations = locations[:visible_location_limit]
        location_picker_truncated = len(locations) > visible_location_limit
        location_filter.options = [
            ft.dropdown.Option("all", "All locations"),
            *[
                ft.dropdown.Option(
                    str(location["id"]),
                    " · ".join(
                        part
                        for part in (
                            location.get("name"),
                            location.get("district"),
                            location.get("tehsil"),
                            location.get("land_type"),
                            location.get("mauza"),
                            location.get("committee_town"),
                            location.get("revenue_circle"),
                        )
                        if part
                    ),
                )
                for location in visible_locations
            ],
        ]
        if location_filter.value != "all" and not any(
            str(location["id"]) == location_filter.value for location in locations
        ):
            location_filter.value = "all"
        return locations

    def refresh_rates(e=None, *, update_page: bool = True, records=None):
        """Apply all visible filters and replace the scrollable rate cards."""
        rate_list.controls.clear()
        try:
            raw_year = (published_year_filter.value or "").strip()
            published_year = int(raw_year) if raw_year else None
            if published_year is not None and not 1900 <= published_year <= 2200:
                raise ValueError("Published year must be between 1900 and 2200.")
            selected_location = location_filter.value or "all"
            if records is None:
                records = list_dc_rates(
                    search=search_field.value or "", mauza=mauza_filter.value or "",
                    square_no=square_filter.value or "", kila_no=kila_filter.value or "",
                    schedule_type=schedule_filter.value if schedule_filter.value != "all" else None,
                    location_id=int(selected_location) if selected_location != "all" else None,
                    rate_unit=unit_filter.value if unit_filter.value != "all" else None,
                    published_year=published_year, limit=visible_rate_limit,
                )
            displayed_records = records[:visible_rate_limit]
            result_count.value = (
                f"Showing {len(displayed_records)} of {len(records)} matching rate record(s)"
                if len(records) > visible_rate_limit else f"{len(records)} rate record(s)"
            )
            status_text.value = ""
            if not records:
                rate_list.controls.append(
                    ft.Container(
                        padding=18,
                        bgcolor="#FFFFFF",
                        border=ft.Border.all(1, "#E2E8F0"),
                        border_radius=10,
                        content=ft.Column(
                            controls=[
                                ft.Text("No schedule rates match these filters.",
                                        weight=ft.FontWeight.BOLD, color="#334155"),
                                ft.Text("Clear the search or filters, or add/import a rate to see it here.",
                                        color="#64748B", size=13),
                            ],
                            tight=True,
                            spacing=4,
                        ),
                    )
                )
            for record in displayed_records:
                add_rate_card(record)
            if len(records) > visible_rate_limit:
                status_text.value = (
                    f"Showing the latest {visible_rate_limit} matching records. "
                    "Use search or filters to narrow the list."
                )
            if location_picker_truncated:
                result_count.value += (
                    f" · location quick-filter shows {visible_location_limit}; "
                    "search covers all locations"
                )
        except ValueError as exc:
            result_count.value = ""
            status_text.value = f"Check the published year: {exc}"
        except Exception as exc:
            result_count.value = ""
            status_text.value = f"Could not load DC rates: {exc}"
        if update_page:
            page.update()

    def show_rate_details(rate_id: int):
        record = get_dc_rate(rate_id)
        if record is None:
            status_text.value = "This rate record no longer exists."
            page.update()
            return
        details = [
            f"Record: #{record['id']}",
            f"Schedule: {record.get('schedule_type', 'dc').upper()}",
            f"District / Tehsil: {record.get('district') or '—'} / {record.get('tehsil') or '—'}",
            f"Rural / Urban: {record.get('land_type') or '—'}",
            f"Committee / Town: {record.get('committee_town') or '—'}",
            f"Revenue circle: {record.get('revenue_circle') or '—'}",
            f"Floor: {record.get('floor') or '—'}",
            f"Property area / Mauza: {record.get('property_area') or '—'} / {record.get('mauza') or '—'}",
            f"Location: {record.get('locality') or record['location_name']}",
            f"Classification: {record.get('classification') or '—'}",
            f"Rate: {record['rate_value']} {UNIT_LABELS[record['rate_unit']]}",
            f"Published year: {record.get('published_year') or 'Not supplied'}",
            f"Source reference: {record.get('source_reference') or 'Not supplied'}",
            f"Created: {record.get('created_at') or '—'}",
            f"Last modified: {record.get('updated_at') or '—'}",
        ]
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"{record.get('schedule_type', 'dc').upper()} rate #{rate_id} details"),
            content=ft.Column(
                controls=[ft.Text(line, selectable=True) for line in details],
                tight=True,
                scroll=ft.ScrollMode.AUTO,
                width=560,
            ),
            actions=[ft.TextButton("Close", on_click=lambda _: close_dialog())],
        )
        page.show_dialog(dialog)

    def add_rate_card(record: dict):
        """Render one queried schedule record as a readable, responsive card."""
        rate_text = (
            f"{record['rate_value']} {UNIT_LABELS.get(record['rate_unit'], record['rate_unit'])}"
        )
        published_year = record.get("published_year") or "Not supplied"
        location_summary = " · ".join(
            value for value in (
                record.get("land_type"),
                record.get("district"),
                record.get("tehsil"),
                record.get("committee_town"),
                record.get("revenue_circle"),
                record.get("mauza"),
            ) if value
        ) or "Location details not supplied"

        def open_details(_):
            show_rate_details(record["id"])

        def open_edit(_):
            open_rate_dialog(rate_id=record["id"])

        rate_list.controls.append(
            ft.Container(
                padding=14,
                bgcolor="#FFFFFF",
                border=ft.Border.all(1, "#E2E8F0"),
                border_radius=10,
                content=ft.Column(
                    controls=[
                        ft.Row(
                            controls=[
                                ft.Text(
                                    f"{record.get('schedule_type', 'dc').upper()} schedule · {record['location_name']}",
                                    weight=ft.FontWeight.BOLD,
                                    size=16,
                                    selectable=True,
                                ),
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            wrap=True,
                            spacing=10,
                            run_spacing=4,
                        ),
                        ft.Text(
                            f"{record.get('classification') or record['property_type']}"
                            f" · Rate: {rate_text}"
                            f" · Floor: {record.get('floor') or 'All / not specified'}",
                            selectable=True,
                        ),
                        ft.Text(location_summary, size=12, color="#475569", selectable=True),
                        ft.Text(
                            f"Published year: {published_year} · Source: {record.get('source_reference') or 'Not supplied'}",
                            size=12,
                            color="#64748B",
                            selectable=True,
                        ),
                        ft.Row(
                            controls=[
                                ft.TextButton("Details", on_click=open_details),
                                ft.TextButton("Edit", on_click=open_edit),
                            ],
                            spacing=4,
                            wrap=True,
                        ),
                    ],
                    spacing=5,
                    tight=True,
                ),
            )
        )

    def open_location_dialog(_=None):
        fields = {
            "province": ft.TextField(label="Province", value="Punjab", width=240),
            "district": ft.TextField(label="District"),
            "tehsil": ft.TextField(label="Tehsil"),
            "land_type": ft.RadioGroup(
                content=ft.Row(
                    controls=[
                        ft.Radio(label="Urban", value="urban"),
                        ft.Radio(label="Rural", value="rural"),
                    ],
                    wrap=True,
                    spacing=16,
                ),
                value="urban",
            ),
            "qanoongoi": ft.TextField(label="Qanoongoi"),
            "patwar_circle": ft.TextField(label="Patwar circle (optional)"),
            "mauza": ft.TextField(label="Mauza"),
            "committee_town": ft.TextField(label="Town / city"),
            "revenue_circle": ft.TextField(label="Revenue circle"),
            "property_area": ft.TextField(label="Property area"),
            "khasra_no": ft.TextField(label="Khasra no (optional)"),
            "square_no": ft.TextField(label="Square no"),
            "kila_no": ft.TextField(label="Kila no"),
            "locality": ft.TextField(label="Location", autofocus=True, width=500),
        }
        rural_fields = [fields[key] for key in ("qanoongoi", "patwar_circle", "mauza")]
        urban_fields = [fields[key] for key in ("committee_town", "revenue_circle", "property_area", "khasra_no")]
        rural_section = ft.Column(
            controls=[ft.Text("Rural schedule details", weight=ft.FontWeight.BOLD), *rural_fields],
            tight=True,
            spacing=6,
        )
        urban_section = ft.Column(
            controls=[ft.Text("Urban schedule details", weight=ft.FontWeight.BOLD), *urban_fields],
            tight=True,
            spacing=6,
        )
        urban_section.visible = fields["land_type"].value == "urban"
        rural_section.visible = not urban_section.visible

        def toggle_location_fields(_=None):
            urban_section.visible = fields["land_type"].value == "urban"
            rural_section.visible = not urban_section.visible
            page.update()

        fields["land_type"].on_change = toggle_location_fields

        def save_location(_):
            try:
                location_values = {key: field.value or "" for key, field in fields.items()}
                # Use the user's location label as the internal display name;
                # fall back to a cadastral/place name for legacy schedules.
                location_values["name"] = (
                    location_values.get("locality")
                    or location_values.get("mauza")
                    or location_values.get("property_area")
                    or location_values.get("committee_town")
                )
                location_id, created = create_location(**location_values)
                close_dialog()
                refresh_locations()
                location_filter.value = str(location_id)
                refresh_rates()
                status_text.value = (
                    "Location added." if created else "Matching location already existed; selected it."
                )
                page.update()
            except DCRateServiceError as exc:
                status_text.value = str(exc)
                page.update()

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Add property location"),
            content=ft.Column(
                controls=[
                    fields["locality"],
                    ft.Row([fields["district"], fields["tehsil"]], wrap=True, spacing=8),
                    ft.Text("Land type", weight=ft.FontWeight.BOLD),
                    fields["land_type"],
                    rural_section,
                    urban_section,
                    ft.Text("Cadastral identifiers", weight=ft.FontWeight.BOLD),
                    ft.Row([fields["square_no"], fields["kila_no"]], wrap=True, spacing=8),
                ],
                tight=False,
                scroll=ft.ScrollMode.AUTO,
                width=560,
                height=520,
                spacing=8,
            ),
            actions=[
                ft.TextButton("Cancel", on_click=lambda _: close_dialog()),
                ft.Button("Save location", on_click=save_location),
            ],
        )
        page.show_dialog(dialog)

    def open_rate_dialog(e=None, *, rate_id: int | None = None, schedule_type: str = "dc"):
        """Open the add form from a Flet event or edit a specific rate ID."""
        # The selected location stores the administrative dimensions; this
        # form stores one effective, source-traceable DC or FBR schedule row.
        locations = refresh_locations()
        existing = get_dc_rate(rate_id) if rate_id is not None else None
        if rate_id is not None and existing is None:
            status_text.value = "This rate record no longer exists."
            page.update()
            return

        location_field = ft.Dropdown(
            label="Property location",
            value=str(existing["location_id"]) if existing and existing["location_id"] else None,
            options=[
                ft.dropdown.Option(
                    str(location["id"]),
                    " · ".join(
                        part
                        for part in (
                            location.get("name"),
                            location.get("district"),
                            location.get("tehsil"),
                            location.get("land_type"),
                            location.get("mauza"),
                            location.get("committee_town"),
                        )
                        if part
                    ),
                )
                for location in locations
            ],
            width=600,
        )
        location_by_id = {str(location["id"]): location for location in locations}
        selected_location = location_by_id.get(location_field.value or "", {})
        initial_classification = (
            existing.get("classification") or existing.get("property_type")
            if existing else "Agricultural"
        )
        classification_options = [
            ft.dropdown.Option("Agricultural", "Agricultural / Agri"),
            ft.dropdown.Option("Residential", "Residential"),
            ft.dropdown.Option("Commercial", "Commercial"),
        ]
        if initial_classification and initial_classification not in {
            "Agricultural", "Residential", "Commercial"
        }:
            classification_options.append(
                ft.dropdown.Option(initial_classification, initial_classification)
            )
        classification = ft.Dropdown(
            label="Land classification",
            value=initial_classification,
            options=classification_options,
            width=600,
        )
        rate_value = ft.TextField(
            label="DC / FBR schedule value",
            value=str(existing["rate_value"]) if existing else "",
            keyboard_type=ft.KeyboardType.NUMBER,
            width=600,
        )
        rate_unit = ft.Dropdown(
            label="Unit (rate per)",
            value=existing["rate_unit"] if existing else "per_marla",
            options=[
                ft.dropdown.Option(unit, UNIT_LABELS[unit])
                for unit in DC_RATE_UNITS
            ],
            width=600,
        )
        schedule_field = ft.Dropdown(
            label="Valuation schedule",
            value=existing.get("schedule_type", "dc") if existing else (
                "fbr" if selected_location.get("land_type") == "urban" else schedule_type
            ),
            options=[ft.dropdown.Option("dc", "DC schedule"), ft.dropdown.Option("fbr", "FBR schedule")],
            width=600,
        )
        floor = ft.TextField(
            label="Floor (where schedule uses it)",
            value=existing.get("floor") or "" if existing else "",
            width=600,
            visible=selected_location.get("land_type") == "urban",
        )
        published_year = ft.TextField(
            label="Published year (optional)",
            value=str(existing["published_year"])
            if existing and existing.get("published_year") else "",
            keyboard_type=ft.KeyboardType.NUMBER,
            width=600,
        )
        source_reference = ft.TextField(
            label="Schedule / notification reference (optional)",
            value=existing.get("source_reference") or "" if existing else "",
            hint_text="Notification number, schedule title, or page reference",
            width=600,
        )
        form_error = ft.Text("", color="#B91C1C", size=12)

        def update_location_specific_fields(_=None):
            """Show floor only for urban schedules and suggest its common source."""
            selected = location_by_id.get(location_field.value or "", {})
            is_urban = selected.get("land_type") == "urban"
            floor.visible = is_urban
            if rate_id is None:
                schedule_field.value = "fbr" if is_urban else "dc"
            page.update()

        location_field.on_select = update_location_specific_fields

        def save_rate(_):
            try:
                if not location_field.value:
                    raise DCRateServiceError("Add and select a property location first.")
                year_text = (published_year.value or "").strip()
                end_date = existing.get("effective_to") if existing else None
                start_for_end = date(int(year_text), 1, 1) if year_text else date.today()
                kept_end_date = (
                    end_date.isoformat()
                    if end_date and end_date >= start_for_end
                    else ""
                )
                data = {
                    # The normalized classification also fills the legacy
                    # required property_type column used by existing lookups.
                    "property_type": classification.value,
                    "classification": classification.value,
                    "schedule_type": schedule_field.value,
                    "floor": (
                        floor.value
                        if location_by_id.get(location_field.value or "", {}).get("land_type") == "urban"
                        else None
                    ),
                    "rate_value": rate_value.value,
                    "rate_unit": rate_unit.value,
                    "published_year": year_text or None,
                    "effective_to": kept_end_date,
                    "source_reference": source_reference.value,
                    "source_date": existing.get("source_date") if existing else None,
                    "verification_status": existing.get("verification_status", "unverified") if existing else "unverified",
                    "verified_by": existing.get("verified_by") if existing else None,
                }
                if rate_id is None:
                    create_dc_rate(location_id=int(location_field.value), **data)
                else:
                    update_dc_rate(
                        rate_id=rate_id,
                        location_id=int(location_field.value),
                        **data,
                    )
                close_dialog()
                # Show the saved rate together with all earlier rates; keeping
                # location/date/search filters can make existing rows seem lost.
                schedule_filter.value = "all"
                location_filter.value = "all"
                unit_filter.value = "all"
                published_year_filter.value = ""
                search_field.value = ""
                mauza_filter.value = ""
                square_filter.value = ""
                kila_filter.value = ""
                refresh_rates()
                status_text.value = f"{schedule_field.value.upper()} rate saved and shown below."
                page.update()
            except (DCRateServiceError, ValueError, TypeError) as exc:
                form_error.value = str(exc)
                page.update()

        title = (
            f"Add {schedule_type.upper()} valuation rate" if rate_id is None
            else f"Edit {existing.get('schedule_type', 'dc').upper()} rate #{rate_id}"
        )
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(title),
            content=ft.Column(
                controls=[
                    location_field,
                    schedule_field,
                    classification,
                    floor,
                    rate_value,
                    rate_unit,
                    published_year,
                    source_reference,
                    form_error,
                ],
                tight=False,
                scroll=ft.ScrollMode.AUTO,
                width=620,
                height=540,
                spacing=8,
            ),
            actions=[
                ft.TextButton("Cancel", on_click=lambda _: close_dialog()),
                ft.Button("Save rate", on_click=save_rate),
            ],
        )
        page.show_dialog(dialog)

    async def import_excel(_=None):
        # Import moves from file parsing -> column mapping -> validated preview
        # -> service commit, so spreadsheet columns are never guessed silently.
        try:
            files = await file_picker.pick_files(
                dialog_title="Import DC or FBR schedule",
                allowed_extensions=["csv", "xlsx", "ods"],
                allow_multiple=False,
                with_data=True,
            )
            if not files:
                return
            selected = files[0]
            payload = selected.bytes
            if payload is None and selected.path:
                payload = Path(selected.path).read_bytes()
            if payload is None:
                raise DCRateServiceError("Could not read the selected workbook.")
            if payload is None:
                raise DCRateServiceError("Could not read the selected spreadsheet.")
            headers, rows = read_schedule_spreadsheet(payload, selected.name)
            guesses = suggested_column_mapping(headers)
            source_options = [ft.dropdown.Option("", "Skip this field")] + [
                ft.dropdown.Option(str(i), header or f"Column {i + 1}")
                for i, header in enumerate(headers)
            ]
            profile_fields = {
                "rural_dc": [
                    ("serial_no", "Serial no. (optional)"), ("district", "District (optional)"),
                    ("tehsil", "Tehsil"), ("qanoongoi", "Qanoongoi"), ("mauza", "Mauza"),
                    ("classification", "Classification"), ("locality", "Location"),
                    ("square_no", "Square no. (optional when Khasra is supplied)"),
                    ("kila_no", "Kila no. (optional when Khasra is supplied)"),
                    ("khasra_no", "Khasra no. (optional)"),
                    ("rate_value", "DC rate"), ("rate_unit", "Unit"),
                ],
                "urban_dc": [
                    ("district", "District (optional)"), ("tehsil", "Tehsil"),
                    ("qanoongoi", "Qanoongoi (optional)"), ("committee_town", "Town"),
                    ("revenue_circle", "Revenue circle"), ("property_area", "Property area"),
                    ("classification", "Classification"), ("locality", "Location"),
                    ("square_no", "Square no. (optional)"), ("kila_no", "Kila no. (optional)"),
                    ("khasra_no", "Khasra no. (optional)"), ("rate_value", "DC rate"),
                    ("rate_unit", "Unit"), ("structure_rate", "Structure rate"),
                ],
                "fbr": [
                    ("district", "District (optional)"), ("tehsil", "Tehsil (optional)"),
                    ("serial_no", "Serial no. (optional)"), ("revenue_circle", "Revenue circle"),
                    ("property_area", "Property area"), ("classification", "Classification"),
                    ("locality", "Location"), ("square_no", "Location (Square No.)"),
                    ("kila_no", "Kila no. (optional)"), ("khasra_no", "Khasra no. (optional)"),
                    ("rate_value", "Value per square foot (Rs.)"),
                    ("per_marla_value", "Per marla valuation (optional; defaults to square-foot value × 272)"),
                ],
            }
            selects = {}
            mapping_controls = ft.ResponsiveRow(run_spacing=4)

            def rebuild_mapping(_=None):
                """Display only fields supported by the selected schedule profile."""
                profile = schedule.value or "rural_dc"
                selects.clear()
                controls = []
                for target, label in profile_fields[profile]:
                    control = ft.Dropdown(
                        label=label,
                        value=str(guesses[target]) if target in guesses else "",
                        options=source_options,
                    )
                    selects[target] = control
                    controls.append(ft.Container(control, col={"xs": 12, "md": 6}))
                mapping_controls.controls = controls
                page.update()

            schedule = ft.RadioGroup(
                value="rural_dc",
                content=ft.Row([
                    ft.Radio(value="rural_dc", label="Rural DC"),
                    ft.Radio(value="urban_dc", label="Urban DC"),
                    ft.Radio(value="fbr", label="FBR schedule"),
                ], wrap=True, spacing=12),
                on_change=rebuild_mapping,
            )

            def schedule_name() -> str:
                return {"rural_dc": "Rural DC", "urban_dc": "Urban DC", "fbr": "FBR"}.get(
                    schedule.value or "", "selected schedule"
                )

            def show_review(preview):
                candidates, issues = preview["valid_records"], preview["issues"]
                import_year = ft.TextField(
                    label="Published year for this import (optional)",
                    hint_text="Example: 2026. Leave blank to use each row's date/year.",
                    width=300,
                )
                issue_controls = [ft.Text(
                    f"Row {issue['row']} — {issue['kind']}: {issue['message']}", selectable=True,
                    color="#B91C1C" if issue["kind"].startswith("invalid") else "#B45309")
                    for issue in issues[:100]]
                if not issue_controls:
                    issue_controls.append(ft.Text("No invalid or duplicate rows found."))
                # Replace the compact issue labels with a scrollable review:
                # valid records can still be imported, while every skipped row
                # shows its source values, reason, and a concrete correction.
                issue_controls.clear()
                for issue in issues:
                    invalid = issue["kind"].startswith("invalid")
                    issue_controls.append(ft.Container(
                        padding=8,
                        bgcolor="#FEF2F2" if invalid else "#FFFBEB",
                        border=ft.Border.all(1, "#FECACA" if invalid else "#FDE68A"),
                        border_radius=6,
                        content=ft.Column([
                            ft.Text(f"Row {issue['row']} · {issue['kind']}: {issue['message']}",
                                    selectable=True, color="#B91C1C" if invalid else "#B45309",
                                    weight=ft.FontWeight.BOLD),
                            ft.Text("Source values: " + " | ".join(issue.get("source_values", [])),
                                    selectable=True, size=11, color="#7F1D1D" if invalid else "#78350F"),
                            ft.Text("Suggested fix: " + issue.get("fix", "Review and correct this source row."),
                                    selectable=True, size=12, color="#991B1B" if invalid else "#92400E"),
                        ], spacing=3),
                    ))
                if not issue_controls:
                    issue_controls.append(ft.Text("No invalid or duplicate rows found.", color="#166534"))
                valid_preview = ft.ListView(controls=[ft.Text(
                    f"Row {record['row']}: {record['location'].get('name')} · "
                    f"{record['classification'] or record['property_type']} · "
                    f"{record['rate_value']} {record['rate_unit']}",
                    size=12, color="#334155",
                ) for record in candidates[:50]], height=130, spacing=3)
                if not candidates:
                    valid_preview.controls.append(ft.Text("No valid rows are ready to import.", color="#64748B"))
                elif len(candidates) > 50:
                    valid_preview.controls.append(ft.Text(f"…and {len(candidates) - 50} more valid rows.", size=11, color="#64748B"))
                issue_preview = ft.ListView(controls=issue_controls, height=230, spacing=5)
                content = ft.Column(controls=[
                    ft.Text(f"File: {selected.name}\nSchedule: {schedule_name()}\n"
                            f"Rows read: {preview['rows_read']} · Ready to import: {len(candidates)} · "
                            f"Rows needing review: {len(issues)}. Existing rates will not be overwritten. "
                            "Valid rows can be imported while issue rows remain skipped.",
                            selectable=True),
                    import_year,
                    ft.Text("Valid row preview", weight=ft.FontWeight.BOLD), valid_preview,
                    ft.Text("Rows needing attention", weight=ft.FontWeight.BOLD), issue_preview],
                    width=680, tight=True, scroll=ft.ScrollMode.AUTO)

                def commit_import(_):
                    nonlocal import_in_progress
                    if import_in_progress:
                        status_text.value = "A schedule import is already running. Please wait for it to finish."
                        page.update()
                        return
                    year_text = (import_year.value or "").strip()
                    if year_text:
                        try:
                            year_value = int(year_text)
                            if not 1900 <= year_value <= 2200:
                                raise ValueError
                        except ValueError:
                            status_text.value = "Published year must be a four-digit year from 1900 to 2200."
                            page.update()
                            return
                        import_records = [
                            {**record, "published_year": year_value,
                             "effective_from": date(year_value, 1, 1)}
                            for record in candidates
                        ]
                    else:
                        import_records = candidates
                    import_in_progress = True
                    close_dialog()
                    status_text.value = (
                        f"Import running in the background: {len(candidates):,} valid "
                        f"{schedule_name()} rate(s). The page remains responsive."
                    )
                    page.update()

                    async def run_import_in_background():
                        nonlocal import_in_progress
                        try:
                            # Database writes happen on a worker thread so a
                            # large schedule does not block Flet's UI event loop.
                            outcome = await asyncio.to_thread(import_dc_rate_records, import_records)
                            refresh_locations()
                            schedule_filter.value = "all"
                            location_filter.value = "all"
                            unit_filter.value = "all"
                            published_year_filter.value = ""
                            search_field.value = ""
                            mauza_filter.value = ""
                            square_filter.value = ""
                            kila_filter.value = ""
                            refresh_rates()
                            status_text.value = (
                                f"Imported {outcome['imported_count']:,} {schedule_name()} "
                                f"rate(s) from {selected.name}."
                            )
                            skipped = len(issues) + len(outcome["issues"])
                            if skipped:
                                status_text.value += f" {skipped:,} invalid or conflicting row(s) were skipped."
                        except Exception as exc:
                            status_text.value = f"Schedule import failed: {exc}"
                        finally:
                            import_in_progress = False
                            page.update()

                    try:
                        page.run_task(run_import_in_background)
                    except Exception as exc:
                        import_in_progress = False
                        status_text.value = f"Could not start the background import: {exc}"
                        page.update()
                page.show_dialog(ft.AlertDialog(modal=True, title=ft.Text("Review schedule import"),
                    content=content, actions=[ft.TextButton("Cancel", on_click=lambda _: close_dialog()),
                    ft.Button(f"Import {len(candidates)} valid rate(s)", on_click=commit_import, disabled=not candidates)]))
                page.update()

            def preview_mapping(_):
                try:
                    mapping = {key: int(control.value) for key, control in selects.items()
                               if control.value not in (None, "")}
                    preview = preview_mapped_schedule(headers, rows, mapping, schedule.value)
                    close_dialog()
                    show_review(preview)
                except Exception as exc:
                    mapping_error.value = str(exc)
                    page.update()

            mapping_error = ft.Text("", color="#B91C1C", size=12)
            page.show_dialog(ft.AlertDialog(modal=True, title=ft.Text(f"Map columns — {selected.name}"),
                content=ft.Column(controls=[ft.Text("Schedule type"), schedule, mapping_controls, mapping_error], width=680,
                                  tight=True, scroll=ft.ScrollMode.AUTO),
                actions=[ft.TextButton("Cancel", on_click=lambda _: close_dialog()),
                         ft.Button("Preview rows", on_click=preview_mapping)]))
            rebuild_mapping()
            page.update()
        except Exception as exc:
            status_text.value = f"Could not open schedule spreadsheet: {exc}"
            page.update()

    def records_matching_current_filters():
        """Use the same current filters for refresh and spreadsheet export."""
        raw_year = (published_year_filter.value or "").strip()
        year = int(raw_year) if raw_year else None
        selected_location = location_filter.value or "all"
        return list_dc_rates(
            search=search_field.value or "",
            schedule_type=schedule_filter.value if schedule_filter.value != "all" else None,
            location_id=int(selected_location) if selected_location != "all" else None,
            rate_unit=unit_filter.value if unit_filter.value != "all" else None,
            published_year=year,
        )

    def make_export_handler(file_format: str):
        """Export the currently filtered records in the selected spreadsheet format."""
        async def export_schedule(_=None):
            try:
                records = records_matching_current_filters()
                payload = export_schedule_records(records, file_format)
                destination = await file_picker.save_file(
                    dialog_title=f"Export valuation schedules as {file_format.upper()}",
                    file_name=f"valuation_schedules_{date.today().isoformat()}.{file_format}",
                    allowed_extensions=[file_format],
                    src_bytes=payload,
                )
                if destination:
                    status_text.value = f"Exported {len(records)} record(s) to {destination}."
                else:
                    status_text.value = "Export cancelled."
            except Exception as exc:
                status_text.value = f"Could not export schedule records: {exc}"
            page.update()
        return export_schedule

    search_field.on_change = refresh_rates
    mauza_filter.on_change = refresh_rates
    square_filter.on_change = refresh_rates
    kila_filter.on_change = refresh_rates
    schedule_filter.on_select = refresh_rates
    location_filter.on_select = refresh_rates
    unit_filter.on_select = refresh_rates
    published_year_filter.on_change = refresh_rates

    toolbar = ft.Row(
        controls=[
            ft.Button("Add DC rate", on_click=open_rate_dialog),
            ft.OutlinedButton("Add FBR rate", on_click=lambda _: open_rate_dialog(schedule_type="fbr")),
            ft.OutlinedButton("Add location", on_click=open_location_dialog),
            ft.OutlinedButton("Import CSV / Excel / ODS", on_click=import_excel),
            *[
                ft.OutlinedButton(
                    f"Export {file_format.upper()}",
                    on_click=make_export_handler(file_format),
                )
                for file_format in ("csv", "xlsx", "ods")
            ],
            ft.TextButton("Refresh", on_click=refresh_rates),
        ],
        wrap=True,
        spacing=6,
        run_spacing=6,
    )
    filters = ft.ResponsiveRow([
        ft.Container(mauza_filter, col={"xs": 12, "sm": 6, "md": 3}),
        ft.Container(square_filter, col={"xs": 6, "sm": 3, "md": 2}),
        ft.Container(kila_filter, col={"xs": 6, "sm": 3, "md": 2}),
        ft.Container(unit_filter, col={"xs": 12, "sm": 6, "md": 2}),
        ft.Container(schedule_filter, col={"xs": 12, "sm": 6, "md": 2}),
    ], run_spacing=4)
    extended_filters = ft.ResponsiveRow([
        ft.Container(search_field, col={"xs": 12, "md": 4}),
        ft.Container(location_filter, col={"xs": 12, "md": 4}),
        ft.Container(published_year_filter, col={"xs": 12, "md": 4}),
    ], visible=False, run_spacing=4)

    def toggle_extended_filters(_):
        extended_filters.visible = not extended_filters.visible
        extended_filter_button.text = "Hide extended filters" if extended_filters.visible else "Extended filters"
        page.update()

    extended_filter_button = ft.TextButton("Extended filters", on_click=toggle_extended_filters)
    if not defer_initial_load:
        refresh_locations()
    # AppShell attaches this page after its builder returns; avoid sending an
    # update while the initial control tree is not mounted yet.
    if not defer_initial_load:
        refresh_rates(update_page=False)
    else:
        rate_list.controls.append(ft.Text("Loading valuation schedules…", color="#64748B"))

    root = ft.Column(
        controls=[
            ft.Text("DC Valuation Rates", size=25, weight=ft.FontWeight.BOLD),
            ft.Text(
                "Manage rural and urban DC/FBR schedules by location, classification, unit, and published year.",
                color="#64748B",
            ),
            toolbar,
            ft.Text(
                "Import mapped CSV, XLSX, or ODS schedules; export the current records to CSV, XLSX, or ODS.",
                size=12,
                color="#64748B",
            ),
            filters,
            extended_filter_button,
            extended_filters,
            ft.Row([result_count, status_text], wrap=True, spacing=12),
            rate_list,
        ],
        spacing=8,
        expand=True,
    )
    if defer_initial_load:
        async def load_initial_data():
            """Populate the first bounded view without blocking page navigation."""
            try:
                locations, records = await asyncio.gather(
                    asyncio.to_thread(list_locations),
                    asyncio.to_thread(list_dc_rates, limit=visible_rate_limit),
                )
                refresh_locations(locations)
                refresh_rates(update_page=False, records=records)
                page.update()
            except Exception as exc:
                status_text.value = f"Could not load valuation schedules: {exc}"
                page.update()
        if hasattr(page, "run_task"):
            page.run_task(load_initial_data)
        else:
            # Minimal test/page adapters may not expose Flet's task scheduler.
            # Keep their construction side-effect free and render on demand.
            rate_list.controls.clear()
            rate_list.controls.append(ft.Text("Use Refresh to load valuation schedules.", color="#64748B"))
    return root
