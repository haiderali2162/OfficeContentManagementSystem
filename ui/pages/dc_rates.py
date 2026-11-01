"""DC valuation rate management UI and Excel import workflow."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Awaitable, Callable

import flet as ft

from core.models import DC_RATE_UNITS
from services.dc_rate_service import (
    DCRateServiceError,
    create_dc_rate,
    create_location,
    export_schedule_records,
    get_dc_rate,
    import_dc_rate_records,
    list_cadastral_values,
    list_dc_rates,
    list_locations,
    list_mauza_names,
    find_duplicate_dc_rates,
    preview_dc_rate_workbook,
    preview_mapped_schedule,
    read_schedule_spreadsheet,
    remove_duplicate_dc_rates_with_report,
    suggested_column_mapping,
    summarize_dc_rates,
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


def build_dc_rates_page(
    page: ft.Page,
    *,
    defer_initial_load: bool = False,
    on_load_ready: Callable[[Callable[[], Awaitable[None]]], None] | None = None,
) -> ft.Control:
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
    visible_rate_limit = 40
    visible_location_limit = 500
    location_picker_truncated = False

    mauza_filter = ft.AutoComplete(width=220, suggestions_max_height=240)
    square_filter = ft.AutoComplete(width=140, suggestions_max_height=220)
    kila_filter = ft.AutoComplete(width=140, suggestions_max_height=220)
    khasra_filter = ft.AutoComplete(width=150, suggestions_max_height=220)
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
        options=[ft.dropdown.Option("dc_rural", "DC rural"),
                 ft.dropdown.Option("dc_urban", "DC urban"),
                 ft.dropdown.Option("fbr", "FBR"),
                 ft.dropdown.Option("all", "All")],
    )
    published_year_filter = ft.TextField(
        label="Published year (optional)",
        value="",
        width=165,
    )
    status_text = ft.Text("", size=12, color="#475569")
    result_count = ft.Text("", size=12, color="#64748B")
    rate_summary = ft.Container(
        padding=10,
        bgcolor="#F8FAFC",
        border=ft.Border.all(1, "#E2E8F0"),
        border_radius=8,
        content=ft.Text("Rate range appears after filtering by Mauza."),
    )
    rate_list = ft.ListView(expand=True, spacing=8, padding=4)
    load_more_button = ft.OutlinedButton("Load more rates", visible=False)
    request_generation = 0
    current_page_offset = 0
    has_more_rates = False

    def selected_schedule_filters() -> tuple[str | None, str | None]:
        """Translate the user-facing schedule choice into database filters."""
        selected = schedule_filter.value or "all"
        if selected == "dc_rural":
            return "dc", "rural"
        if selected == "dc_urban":
            return "dc", "urban"
        if selected == "fbr":
            return "fbr", None
        return None, None

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

    def refresh_rates(e=None, *, update_page: bool = True, records=None, append=False,
                      summary=None, total_count=None, has_more=None):
        """Apply all visible filters and replace the scrollable rate cards."""
        nonlocal has_more_rates, current_page_offset
        if not append:
            rate_list.controls.clear()
        try:
            raw_year = (published_year_filter.value or "").strip()
            published_year = int(raw_year) if raw_year else None
            if published_year is not None and not 1900 <= published_year <= 2200:
                raise ValueError("Published year must be between 1900 and 2200.")
            selected_location = location_filter.value or "all"
            schedule_type, land_type = selected_schedule_filters()
            if records is None:
                records = list_dc_rates(
                    search=search_field.value or "", mauza=mauza_filter.value or "",
                    square_no=square_filter.value or "", kila_no=kila_filter.value or "",
                    khasra_no=khasra_filter.value or "",
                    schedule_type=schedule_type, land_type=land_type,
                    location_id=int(selected_location) if selected_location != "all" else None,
                    rate_unit=unit_filter.value if unit_filter.value != "all" else None,
                    published_year=published_year, limit=visible_rate_limit,
                )
            displayed_records = records[:visible_rate_limit]
            current_page_offset = current_page_offset + len(displayed_records) if append else len(displayed_records)
            has_more_rates = (len(records) > visible_rate_limit) if has_more is None else has_more
            load_more_button.visible = has_more_rates
            result_count.value = (f"Showing {current_page_offset} of {total_count} matching rates"
                                  if total_count is not None else f"Showing {current_page_offset} rate(s)")
            if summary is not None:
                if not mauza_filter.value:
                    summary_text = "Select a Mauza to see the lowest and highest matching rates."
                elif not summary["count"]:
                    summary_text = f"No rates found for Mauza '{mauza_filter.value}'."
                else:
                    unit_text = UNIT_LABELS.get(unit_filter.value, "all units") if unit_filter.value != "all" else "all units"
                    low_unit = UNIT_LABELS.get(summary.get("lowest_unit"), summary.get("lowest_unit"))
                    high_unit = UNIT_LABELS.get(summary.get("highest_unit"), summary.get("highest_unit"))
                    summary_text = (
                        f"{summary['count']:,} rate(s) · {unit_text} · "
                        f"Lowest: {summary['lowest']} {low_unit} ({summary.get('lowest_schedule', '').upper()}) · "
                        f"Highest: {summary['highest']} {high_unit} ({summary.get('highest_schedule', '').upper()})"
                    )
                rate_summary.content.value = summary_text
            status_text.value = ""
            if not records and not append:
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

    def schedule_rates_refresh(_=None, *, append=False, suggestion_field=None):
        """Debounce rapid edits, query on a worker, and render only the latest result."""
        nonlocal request_generation
        request_generation += 1
        generation = request_generation
        status_text.value = "Loading matching rates…"
        page.update()

        async def load_filtered_rates():
            nonlocal request_generation
            await asyncio.sleep(0.18)
            if generation != request_generation:
                return
            raw_year = (published_year_filter.value or "").strip()
            try:
                year = int(raw_year) if raw_year else None
                if year is not None and not 1900 <= year <= 2200:
                    raise ValueError("Published year must be between 1900 and 2200.")
                selected_location = location_filter.value or "all"
                offset = current_page_offset if append else 0
                schedule_type, land_type = selected_schedule_filters()
                records, summary, mauza_names = await asyncio.gather(
                    asyncio.to_thread(
                        list_dc_rates,
                        search=search_field.value or "", mauza=mauza_filter.value or "",
                        square_no=square_filter.value or "", kila_no=kila_filter.value or "",
                        khasra_no=khasra_filter.value or "",
                        schedule_type=schedule_type, land_type=land_type,
                        location_id=int(selected_location) if selected_location != "all" else None,
                        rate_unit=unit_filter.value if unit_filter.value != "all" else None,
                        published_year=year, limit=visible_rate_limit + 1, offset=offset,
                    ),
                    asyncio.to_thread(
                        summarize_dc_rates,
                        mauza=mauza_filter.value or "",
                        rate_unit=unit_filter.value if unit_filter.value != "all" else None,
                        schedule_type=schedule_type, land_type=land_type,
                    ),
                    asyncio.to_thread(list_mauza_names, search=mauza_filter.value or ""),
                )
                if generation != request_generation:
                    return
                completion_message = status_text.value
                refresh_rates(records=records, append=append, summary=summary,
                              has_more=len(records) > visible_rate_limit,
                              update_page=False)
                mauza_filter.suggestions = [
                    ft.AutoCompleteSuggestion(key=name, value=name) for name in mauza_names
                ]
                cadastral_controls = {
                    "square_no": square_filter,
                    "kila_no": kila_filter,
                    "khasra_no": khasra_filter,
                }
                if suggestion_field in cadastral_controls:
                    control = cadastral_controls[suggestion_field]
                    values = await asyncio.to_thread(
                        list_cadastral_values,
                        suggestion_field,
                        search=control.value or "",
                    )
                    if generation != request_generation:
                        return
                    control.suggestions = [ft.AutoCompleteSuggestion(key=value, value=value) for value in values]
                # Keep a recent save/import confirmation visible after its
                # automatic background refresh has completed.
                if completion_message and not completion_message.startswith("Loading"):
                    status_text.value = completion_message
            except (TypeError, ValueError) as exc:
                result_count.value = ""
                status_text.value = f"Check the filters: {exc}"
            except Exception as exc:
                result_count.value = ""
                status_text.value = f"Could not load DC rates: {exc}"
            page.update()

        if hasattr(page, "run_task"):
            page.run_task(load_filtered_rates)
        else:
            # Test adapters can still exercise the filters without Flet's loop.
            refresh_rates()

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
                schedule_rates_refresh()
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
                khasra_filter.value = ""
                schedule_rates_refresh()
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
                            refreshed_locations = await asyncio.to_thread(list_locations)
                            refresh_locations(refreshed_locations)
                            schedule_filter.value = "all"
                            location_filter.value = "all"
                            unit_filter.value = "all"
                            published_year_filter.value = ""
                            search_field.value = ""
                            mauza_filter.value = ""
                            square_filter.value = ""
                            kila_filter.value = ""
                            khasra_filter.value = ""
                            schedule_rates_refresh()
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
        schedule_type, land_type = selected_schedule_filters()
        return list_dc_rates(
            search=search_field.value or "",
            schedule_type=schedule_type,
            land_type=land_type,
            location_id=int(selected_location) if selected_location != "all" else None,
            rate_unit=unit_filter.value if unit_filter.value != "all" else None,
            published_year=year,
            mauza=mauza_filter.value or "",
            square_no=square_filter.value or "",
            kila_no=kila_filter.value or "",
            khasra_no=khasra_filter.value or "",
        )

    def open_duplicate_cleanup_dialog(_=None):
        """Preview duplicates, then export their values before removing extras."""
        report_format = ft.Dropdown(
            label="Removed-row report format",
            value="xlsx",
            options=[
                ft.dropdown.Option("xlsx", "Excel (.xlsx)"),
                ft.dropdown.Option("csv", "CSV (.csv)"),
                ft.dropdown.Option("ods", "OpenDocument spreadsheet (.ods)"),
            ],
            width=280,
        )
        scan_summary = ft.Text("Scan existing schedule records before removing anything.", size=12)
        duplicate_list = ft.ListView(height=250, spacing=4)
        remove_button = ft.Button("Export and remove duplicates", disabled=True)
        scan_button = ft.OutlinedButton("Scan for duplicates")
        scanned_count = 0

        def show_duplicates(records):
            nonlocal scanned_count
            scanned_count = len(records)
            duplicate_list.controls.clear()
            if not records:
                duplicate_list.controls.append(ft.Text("No duplicate schedule rows were found."))
                scan_summary.value = "No duplicates found. Your records were not changed."
                remove_button.disabled = True
                remove_button.text = "Export and remove duplicates"
            else:
                scan_summary.value = (
                    f"Found {scanned_count:,} extra row(s). The lowest record ID in each "
                    "duplicate group will be kept. Removed rows are exported first."
                )
                for record in records[:100]:
                    duplicate_list.controls.append(ft.Text(
                        f"#{record['id']} (duplicate of #{record['duplicate_of']}) | "
                        f"{record.get('schedule_type', '').upper()} | {record.get('mauza') or 'No Mauza'} | "
                        f"Square {record.get('square_no') or '-'} | Kila {record.get('kila_no') or '-'} | "
                        f"Khasra {record.get('khasra_no') or '-'} | "
                        f"{record.get('classification') or record.get('property_type')} | "
                        f"{record.get('rate_value')} {record.get('rate_unit')} | "
                        f"Year {record.get('published_year') or 'unspecified'}",
                        size=12, selectable=True,
                    ))
                if scanned_count > 100:
                    duplicate_list.controls.append(ft.Text(
                        f"Showing the first 100 of {scanned_count:,} extra rows.", size=11,
                    ))
                remove_button.disabled = False
                remove_button.text = f"Export and remove {scanned_count:,} duplicate row(s)"
            page.update()

        async def run_scan():
            scan_button.disabled = True
            scan_summary.value = "Scanning schedule records in the background..."
            page.update()
            try:
                records = await asyncio.to_thread(find_duplicate_dc_rates)
                show_duplicates(records)
            except Exception as exc:
                scan_summary.value = f"Could not scan for duplicates: {exc}"
                remove_button.disabled = True
            finally:
                scan_button.disabled = False
                page.update()

        def scan(_):
            if hasattr(page, "run_task"):
                page.run_task(run_scan)
            else:
                scan_button.disabled = True
                try:
                    show_duplicates(find_duplicate_dc_rates())
                except Exception as exc:
                    scan_summary.value = f"Could not scan for duplicates: {exc}"
                finally:
                    scan_button.disabled = False
                    page.update()

        async def run_removal():
            remove_button.disabled = True
            scan_summary.value = "Writing the report and removing duplicate rows..."
            page.update()
            try:
                removed_count, report_path = await asyncio.to_thread(
                    remove_duplicate_dc_rates_with_report, report_format.value
                )
                if removed_count and report_path:
                    close_dialog()
                    status_text.value = (
                        f"Removed {removed_count:,} duplicate row(s). Full removed-row report: {report_path}"
                    )
                    schedule_rates_refresh()
                else:
                    scan_summary.value = "No duplicates remain. The database was not changed."
                    remove_button.disabled = True
                page.update()
            except Exception as exc:
                scan_summary.value = f"Duplicate removal failed; records were not removed: {exc}"
                remove_button.disabled = False
                page.update()

        def remove_duplicates(_):
            if scanned_count == 0:
                return
            if hasattr(page, "run_task"):
                page.run_task(run_removal)
            else:
                scan_summary.value = "Background task support is required for safe duplicate removal."
                page.update()

        scan_button.on_click = scan
        remove_button.on_click = remove_duplicates
        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("Find and remove duplicate schedule rows"),
            content=ft.Column([
                ft.Text(
                    "Rural DC rows are grouped by Mauza, cadastral numbers, location, classification, and unit. "
                    "Urban DC and FBR rows must match all schedule and location values. The first record is kept.",
                    size=12,
                ),
                report_format,
                scan_summary,
                duplicate_list,
            ], width=720, height=420, scroll=ft.ScrollMode.AUTO, spacing=8),
            actions=[
                ft.TextButton("Close", on_click=lambda _: close_dialog()),
                scan_button,
                remove_button,
            ],
        ))
        page.update()

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

    search_field.on_change = schedule_rates_refresh
    mauza_filter.on_change = lambda event: schedule_rates_refresh(event, suggestion_field="mauza")
    mauza_filter.on_select = schedule_rates_refresh
    square_filter.on_change = lambda event: schedule_rates_refresh(event, suggestion_field="square_no")
    kila_filter.on_change = lambda event: schedule_rates_refresh(event, suggestion_field="kila_no")
    khasra_filter.on_change = lambda event: schedule_rates_refresh(event, suggestion_field="khasra_no")
    schedule_filter.on_select = schedule_rates_refresh
    location_filter.on_select = schedule_rates_refresh
    unit_filter.on_select = schedule_rates_refresh
    published_year_filter.on_change = schedule_rates_refresh
    load_more_button.on_click = lambda _: schedule_rates_refresh(append=True)

    import_export_button = ft.TextButton("Show import / export")
    filter_button = ft.TextButton("Show filters")

    def toggle_import_export(_):
        import_export_toolbar.visible = not import_export_toolbar.visible
        import_export_button.content = (
            "Hide import / export" if import_export_toolbar.visible else "Show import / export"
        )
        page.update()

    import_export_button.on_click = toggle_import_export
    filter_button.on_click = lambda _: toggle_filters()

    toolbar = ft.Row(
        controls=[
            ft.Button("Add DC rate", on_click=open_rate_dialog),
            ft.OutlinedButton("Add FBR rate", on_click=lambda _: open_rate_dialog(schedule_type="fbr")),
            ft.OutlinedButton("Add location", on_click=open_location_dialog),
            ft.OutlinedButton("Find / remove duplicates", on_click=open_duplicate_cleanup_dialog),
            filter_button,
            import_export_button,
            ft.TextButton("Refresh", on_click=schedule_rates_refresh),
        ],
        wrap=False,
        scroll=ft.ScrollMode.AUTO,
        spacing=6,
    )
    import_export_toolbar = ft.Column(
        controls=[
            ft.Text(
                "Import mapped CSV, XLSX, or ODS schedules; export the current records in these formats.",
                size=12,
                color="#64748B",
            ),
            ft.Row(
                controls=[
                    ft.OutlinedButton("Import CSV / Excel / ODS", on_click=import_excel),
                    *[
                        ft.OutlinedButton(
                            f"Export {file_format.upper()}",
                            on_click=make_export_handler(file_format),
                        )
                        for file_format in ("csv", "xlsx", "ods")
                    ],
                ],
                wrap=True,
                spacing=6,
                run_spacing=6,
            ),
        ],
        spacing=4,
        visible=False,
    )
    filters = ft.ResponsiveRow([
        ft.Container(ft.Column([ft.Text("Mauza"), mauza_filter], spacing=2), col={"xs": 12, "sm": 6, "md": 3}),
        ft.Container(ft.Column([ft.Text("Square no."), square_filter], spacing=2), col={"xs": 6, "sm": 3, "md": 2}),
        ft.Container(ft.Column([ft.Text("Kila no."), kila_filter], spacing=2), col={"xs": 6, "sm": 3, "md": 2}),
        ft.Container(ft.Column([ft.Text("Khasra no."), khasra_filter], spacing=2), col={"xs": 6, "sm": 3, "md": 2}),
        ft.Container(unit_filter, col={"xs": 12, "sm": 6, "md": 2}),
        ft.Container(schedule_filter, col={"xs": 12, "sm": 6, "md": 2}),
    ], run_spacing=4)
    search_bar = ft.ResponsiveRow([
        ft.Container(search_field, col={"xs": 12, "md": 6}),
    ], run_spacing=4)
    extended_filters = ft.ResponsiveRow([
        ft.Container(location_filter, col={"xs": 12, "md": 4}),
        ft.Container(published_year_filter, col={"xs": 12, "md": 4}),
    ], visible=False, run_spacing=4)

    def toggle_extended_filters(_):
        extended_filters.visible = not extended_filters.visible
        extended_filter_button.content = "Hide extended filters" if extended_filters.visible else "Extended filters"
        page.update()

    extended_filter_button = ft.TextButton("Extended filters", on_click=toggle_extended_filters)
    filter_panel = ft.Container(
        content=ft.Column(
            controls=[filters, extended_filter_button, extended_filters],
            spacing=4,
            scroll=ft.ScrollMode.AUTO,
        ),
        height=170,
        padding=8,
        bgcolor="#FFFFFF",
        border=ft.Border.all(1, "#E2E8F0"),
        border_radius=8,
        visible=False,
    )

    def toggle_filters():
        filter_panel.visible = not filter_panel.visible
        filter_button.content = "Hide filters" if filter_panel.visible else "Show filters"
        page.update()

    # Search stays visible while optional controls start collapsed. On small
    # windows, the bounded filter panel scrolls so cards keep the remaining area.
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
            import_export_toolbar,
            search_bar,
            filter_panel,
            ft.Row([result_count, status_text], wrap=True, spacing=12),
            rate_summary,
            rate_list,
            load_more_button,
        ],
        spacing=8,
        expand=True,
    )
    if defer_initial_load:
        async def load_initial_data():
            """Populate the first bounded view without blocking page navigation."""
            try:
                locations, records, mauza_names, summary, squares, kilas, khasras = await asyncio.gather(
                    asyncio.to_thread(list_locations),
                    asyncio.to_thread(list_dc_rates, limit=visible_rate_limit + 1),
                    asyncio.to_thread(list_mauza_names, search=mauza_filter.value or ""),
                    asyncio.to_thread(summarize_dc_rates),
                    asyncio.to_thread(list_cadastral_values, "square_no"),
                    asyncio.to_thread(list_cadastral_values, "kila_no"),
                    asyncio.to_thread(list_cadastral_values, "khasra_no"),
                )
                refresh_locations(locations)
                mauza_filter.suggestions = [
                    ft.AutoCompleteSuggestion(key=name, value=name) for name in mauza_names
                ]
                for control, values in (
                    (square_filter, squares), (kila_filter, kilas), (khasra_filter, khasras)
                ):
                    control.suggestions = [ft.AutoCompleteSuggestion(key=value, value=value) for value in values]
                refresh_rates(update_page=False, records=records, summary=summary,
                              has_more=len(records) > visible_rate_limit)
                page.update()
            except Exception as exc:
                status_text.value = f"Could not load valuation schedules: {exc}"
                page.update()
        if on_load_ready is not None:
            on_load_ready(load_initial_data)
        elif hasattr(page, "run_task"):
            page.run_task(load_initial_data)
        else:
            # Minimal test/page adapters may not expose Flet's task scheduler.
            # Keep their construction side-effect free and render on demand.
            rate_list.controls.clear()
            rate_list.controls.append(ft.Text("Use Refresh to load valuation schedules.", color="#64748B"))
    return root
