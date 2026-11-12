"""DC valuation rate management UI and Excel import workflow."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Awaitable, Callable

import flet as ft

from core.models import DC_RATE_UNIT_LABELS as UNIT_LABELS, DC_RATE_UNITS
from services.dc_rate_duplicate_service import (
    EXACT_ALL_REFERENCE,
    MATCH_FIELD_GROUPS,
    MATCH_FIELD_LABELS,
)
from services.dc_rate_service import (
    DCRateServiceError,
    MAUZA_CONTEXT_FIELDS,
    create_dc_rate,
    create_duplicate_rule_profile,
    create_location,
    delete_duplicate_rule_profile,
    export_import_issues,
    export_schedule_records,
    get_dc_rate,
    group_schedule_sheets,
    import_dc_rate_records,
    assign_cadastral_references_to_khewat,
    list_khewats_for_mauza,
    list_khewat_summary,
    export_khewat_summary,
    export_khewat_import_template,
    preview_khewat_import,
    import_khewat_assignments,
    list_saved_khewat_mauzas,
    list_saved_khewat_numbers,
    list_mauza_contexts,
    list_cadastral_values,
    list_dc_rates,
    list_duplicate_rule_profiles,
    list_location_hint_values,
    list_location_picker,
    list_mauza_cadastral_values,
    list_rhz_years,
    list_mauza_names,
    DELETION_SCOPES,
    delete_dc_rate_schedules,
    MAX_EXPANDED_ROWS_PER_SOURCE,
    preload_location_dimensions,
    find_duplicate_dc_rates,
    find_khewat_year_conflicts,
    latest_rhz_year,
    normalize_rhz_year,
    preview_dc_rate_workbook,
    preview_mapped_schedule,
    read_schedule_sheets,
    read_schedule_spreadsheet,
    remove_duplicate_dc_rates_with_report,
    suggested_column_mapping,
    summarize_dc_rates,
    summarize_location_hierarchy,
    summarize_mauza_cadastral_selection,
    search_khewats,
    summarize_mauza_references,
    update_khewat_record,
    update_dc_rate,
    update_duplicate_rule_profile,
)
from services.settings_service import (
    format_card_body_lines,
    format_card_heading,
    get_cadastral_storage_mode,
    get_card_appearance,
    get_card_fields,
    get_default_file_location,
    set_cadastral_storage_mode,
)
from ui.file_location import ask_location_choice, write_export_to_folder


# (canonical unit labels moved to core.models.DC_RATE_UNIT_LABELS and are
# imported as UNIT_LABELS in the header above)

DUPLICATE_SCOPE_LABELS = {
    "rural_dc": "Rural DC",
    "urban_dc": "Urban DC",
    "fbr": "FBR",
    "any": "Any schedule",
}
DUPLICATE_DATE_LABELS = {
    "overlap": "Overlapping dates",
    "exact": "Exact dates",
    "ignore": "Ignore dates",
}
DUPLICATE_ACTION_LABELS = {
    "skip": "Skip duplicates",
    "import": "Import anyway",
}


def _duplicate_rule_label(profile: dict) -> str:
    return (
        f"{profile['name']} | {DUPLICATE_SCOPE_LABELS.get(profile['schedule_scope'], profile['schedule_scope'])} | "
        f"{DUPLICATE_DATE_LABELS.get(profile['date_mode'], profile['date_mode'])} | "
        f"{DUPLICATE_ACTION_LABELS.get(profile['import_action'], profile['import_action'])}"
    )


def _duplicate_rule_options(
    profiles: list[dict], schedule_scope: str | None = None
) -> list[ft.dropdown.Option]:
    allowed = [
        profile
        for profile in profiles
        if schedule_scope is None
        or profile["schedule_scope"] in (schedule_scope, "any")
    ]
    return [ft.dropdown.Option(profile["reference"], _duplicate_rule_label(profile)) for profile in allowed]


def _duplicate_rule_by_reference(profiles: list[dict], reference: str | None) -> dict | None:
    return next((profile for profile in profiles if profile["reference"] == reference), None)


def _duplicate_rule_help(profile: dict | None) -> str:
    if profile is None:
        return "Select a duplicate rule before scanning or previewing."
    fields = profile.get("match_fields", [])
    field_labels = [MATCH_FIELD_LABELS.get(field, field) for field in fields]
    return (
        f"{profile.get('description') or 'No description.'}\n"
        f"Match {len(fields)} field(s): {', '.join(field_labels)}.\n"
        f"{DUPLICATE_DATE_LABELS.get(profile['date_mode'], profile['date_mode'])}; "
        f"{DUPLICATE_ACTION_LABELS.get(profile['import_action'], profile['import_action'])}."
    )


def _format_date(value: date | None, *, empty: str = "Not supplied") -> str:
    return value.isoformat() if value else empty


def _cadastral_label(record: dict) -> str:
    """Format cadastral identifiers consistently on rate cards and details."""
    return " | ".join((
        f"Square no.: {record.get('square_no') or '—'}",
        f"Kila no.: {record.get('kila_no') or '—'}",
        f"Khasra no.: {record.get('khasra_no') or '—'}",
    ))


def _unique_location_values(locations: list[dict], field: str) -> list[str]:
    """Return trimmed, case-insensitively unique values for a location field."""
    values = {}
    for location in locations:
        value = " ".join(str(location.get(field) or "").split())
        if value:
            values.setdefault(value.casefold(), value)
    return sorted(values.values(), key=str.casefold)


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
    card_appearance = get_card_appearance()
    # Card content (chosen body fields + formula lines) is loaded once per
    # page build so every card renders the same saved configuration.
    card_fields_config = get_card_fields()

    async def export_with_location(
        *,
        action: str,
        dialog_title: str,
        file_name: str,
        allowed_extensions: list[str],
        payload: bytes,
    ) -> str | None:
        """Write to the saved default folder or let the user choose a path."""
        default_location = get_default_file_location()
        choice = await ask_location_choice(
            page, action=action, default_location=default_location
        )
        if choice is None:
            return None
        if choice == "default":
            return await asyncio.to_thread(
                write_export_to_folder,
                default_location,
                file_name,
                payload,
            )
        return await file_picker.save_file(
            dialog_title=dialog_title,
            file_name=file_name,
            allowed_extensions=allowed_extensions,
            src_bytes=payload,
        )

    async def pick_import_file(
        *,
        action: str,
        dialog_title: str,
        allowed_extensions: list[str],
        with_data: bool = True,
        allow_multiple: bool = False,
    ) -> list:
        """Open the import picker, optionally starting in the default folder."""
        default_location = get_default_file_location()
        choice = await ask_location_choice(
            page, action=action, default_location=default_location
        )
        if choice is None:
            return []
        return await file_picker.pick_files(
            dialog_title=dialog_title,
            initial_directory=(
                default_location if choice == "default" else None
            ),
            allowed_extensions=allowed_extensions,
            allow_multiple=allow_multiple,
            with_data=with_data,
        )

    qanoongoi_filter = ft.AutoComplete(
        width=200, suggestions_max_height=240, data="qanoongoi"
    )
    mauza_filter = ft.AutoComplete(
        width=220, suggestions_max_height=240, data="mauza"
    )
    square_filter = ft.AutoComplete(width=140, suggestions_max_height=220, data="square_no")
    kila_filter = ft.AutoComplete(width=140, suggestions_max_height=220, data="kila_no")
    khasra_filter = ft.AutoComplete(width=150, suggestions_max_height=220, data="khasra_no")
    # Developer request: district/tehsil filters back in the extended panel,
    # plus the urban schedule's Town and Property area fields.
    district_filter = ft.AutoComplete(width=160, suggestions_max_height=220, data="district")
    tehsil_filter = ft.AutoComplete(width=160, suggestions_max_height=220, data="tehsil")
    town_filter = ft.AutoComplete(width=160, suggestions_max_height=220, data="committee_town")
    property_area_filter = ft.AutoComplete(width=160, suggestions_max_height=220, data="property_area")
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
    busy_message = ft.Text("Working…", size=13, color="#0F172A")
    busy_detail = ft.Text("", size=11, color="#64748B")
    busy_overlay = ft.Container(
        visible=False,
        left=0,
        top=0,
        right=0,
        bottom=0,
        bgcolor="#660F172A",
        alignment=ft.Alignment(0, 0),
        content=ft.Container(
            width=340,
            padding=20,
            bgcolor="#FFFFFF",
            border_radius=10,
            border=ft.Border.all(1, "#E2E8F0"),
            content=ft.Column([
                ft.Row([
                    ft.ProgressRing(width=30, height=30, stroke_width=3),
                    ft.Column([
                        busy_message,
                        busy_detail,
                    ], spacing=2, expand=True),
                ], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.ProgressBar(width=300),
            ], spacing=12, tight=True),
        ),
    )

    def show_busy(message: str, detail: str = "The page stays responsive; please wait."):
        busy_message.value = message
        busy_detail.value = detail
        busy_overlay.visible = True
        page.update()

    def hide_busy():
        busy_overlay.visible = False
        page.update()
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

    def refresh_locations(
        locations=None, *, include_ids=None, qanoongoi_values=None, has_more=None,
    ):
        """Reload location choices used by the filter and rate editor."""
        nonlocal location_picker_truncated
        if locations is None:
            # The location table can hold hundreds of thousands of rows, so
            # dropdown loads stay bounded; any supplied ids are pinned so a
            # just-created or selected location is never truncated away.
            pinned_ids = [int(value) for value in (include_ids or [])]
            if not pinned_ids and (location_filter.value or "") not in ("", "all"):
                try:
                    pinned_ids = [int(location_filter.value)]
                except (TypeError, ValueError):
                    pinned_ids = []
            picker = list_location_picker(
                limit=visible_location_limit, include_ids=pinned_ids,
            )
            locations = picker["locations"]
            qanoongoi_values = picker["qanoongoi"]
            has_more = picker["has_more"]
        else:
            pinned_ids = [int(value) for value in (include_ids or [])]
        if qanoongoi_values is None:
            qanoongoi_values = _unique_location_values(locations, "qanoongoi")
        qanoongoi_filter.suggestions = [
            ft.AutoCompleteSuggestion(key=value, value=value)
            for value in qanoongoi_values
        ]
        visible_locations = locations[:visible_location_limit]
        shown_ids = {str(location["id"]) for location in visible_locations}
        # Pinned records that fall outside the first page still need their
        # option rendered for the dropdown value to stay selectable.
        pinned_locations = [
            location for location in locations
            if int(location["id"]) in pinned_ids
            and str(location["id"]) not in shown_ids
        ]
        location_picker_truncated = (
            bool(has_more) if has_more is not None
            else len(locations) > visible_location_limit
        )
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
                for location in [*visible_locations, *pinned_locations]
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
                    qanoongoi=qanoongoi_filter.value or "",
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
                if summary["count"] and any(summary.get(key) is not None for key in (
                    "lowest_square", "highest_square", "lowest_kila", "highest_kila"
                )):
                    summary_text += (
                        f" | Square no. {summary.get('lowest_square') or '-'} to "
                        f"{summary.get('highest_square') or '-'}; Kila no. "
                        f"{summary.get('lowest_kila') or '-'} to {summary.get('highest_kila') or '-'}"
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

    def schedule_rates_refresh(
        _=None, *, append=False, suggestion_field=None, refresh_cadastral=False,
    ):
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
                        qanoongoi=qanoongoi_filter.value or "",
                        district=district_filter.value or "", tehsil=tehsil_filter.value or "",
                        committee_town=town_filter.value or "",
                        property_area=property_area_filter.value or "",
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
                        qanoongoi=qanoongoi_filter.value or "",
                        rate_unit=unit_filter.value if unit_filter.value != "all" else None,
                        schedule_type=schedule_type, land_type=land_type,
                    ),
                    asyncio.to_thread(
                        list_mauza_names,
                        search=mauza_filter.value or "",
                        qanoongoi=qanoongoi_filter.value or "",
                        limit=500,
                    ),
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
                cadastral_fields = list(cadastral_controls) if refresh_cadastral else []
                if suggestion_field in cadastral_controls:
                    cadastral_fields.append(suggestion_field)
                    if suggestion_field == "square_no":
                        cadastral_fields.extend(("kila_no", "khasra_no"))
                    elif suggestion_field == "kila_no":
                        cadastral_fields.append("khasra_no")
                cadastral_fields = list(dict.fromkeys(cadastral_fields))
                if cadastral_fields:
                    selected_location_id = (
                        int(selected_location) if selected_location != "all" else None
                    )
                    cadastral_values = await asyncio.gather(*(
                        asyncio.to_thread(
                            list_cadastral_values,
                            field_name,
                            search=cadastral_controls[field_name].value or "",
                            qanoongoi=qanoongoi_filter.value or "",
                            mauza=mauza_filter.value or "",
                            square_no=(
                                square_filter.value or ""
                                if field_name in {"kila_no", "khasra_no"} else ""
                            ),
                            kila_no=(
                                kila_filter.value or "" if field_name == "khasra_no" else ""
                            ),
                            schedule_type=schedule_type,
                            land_type=land_type,
                            location_id=selected_location_id,
                        )
                        for field_name in cadastral_fields
                    ))
                    if generation != request_generation:
                        return
                    for field_name, values in zip(cadastral_fields, cadastral_values):
                        cadastral_controls[field_name].suggestions = [
                            ft.AutoCompleteSuggestion(key=value, value=value)
                            for value in values
                        ]
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
            _cadastral_label(record),
            f"Location: {record.get('locality') or record['location_name']}",
            f"Classification: {record.get('classification') or '—'}",
            f"Valuation: Rs. {record['rate_value']}",
            f"Unit: {UNIT_LABELS[record['rate_unit']].removeprefix('Per ').title()}",
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
        """Render one queried schedule record as a readable, responsive card.

        The body comes from the saved card content when the user has chosen
        fields or formula lines in Settings (see the CARD_FIELDS_KEY docs in
        ``services.settings_service``): selected fields render as one wrapped
        ``Label: value`` line followed by one line per formula. With no custom
        content configured, the built-in layout below keeps its established
        look (classification/rate/floor, location summary, cadastral +
        unit, published year + source).
        """
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
        heading_text = format_card_heading(
            card_appearance["heading_template"], record
        )
        # Custom lines come from the saved card-content configuration; an
        # empty configuration (the default) keeps the built-in layout.
        custom_lines = format_card_body_lines(record, card_fields_config)

        def open_details(_):
            show_rate_details(record["id"])

        def open_edit(_):
            open_rate_dialog(rate_id=record["id"])

        if custom_lines:
            body_texts = [
                ft.Text(
                    line,
                    size=12 if index else None,
                    color=card_appearance["body_color"],
                    selectable=True,
                )
                for index, line in enumerate(custom_lines)
            ]
        else:
            body_texts = [
                ft.Text(
                    f"{record.get('classification') or record['property_type']}"
                    f" · Rate: {rate_text}"
                    f" · Floor: {record.get('floor') or 'All / not specified'}",
                    color=card_appearance["body_color"],
                    selectable=True,
                ),
                ft.Text(
                    location_summary,
                    size=12,
                    color=card_appearance["body_color"],
                    selectable=True,
                ),
                ft.Text(
                    f"{_cadastral_label(record)} | Unit: "
                    f"{UNIT_LABELS.get(record['rate_unit'], record['rate_unit']).removeprefix('Per ').title()}",
                    size=12,
                    color=card_appearance["body_color"],
                    selectable=True,
                ),
                ft.Text(
                    f"Published year: {published_year} · Source: {record.get('source_reference') or 'Not supplied'}",
                    size=12,
                    color=card_appearance["body_color"],
                    selectable=True,
                ),
            ]
        rate_list.controls.append(
            ft.Container(
                padding=14,
                bgcolor=card_appearance["background_color"],
                border=ft.Border.all(1, card_appearance["border_color"]),
                border_radius=10,
                content=ft.Column(
                    controls=[
                        ft.Row(
                            controls=[
                                ft.Text(
                                    heading_text,
                                    weight=ft.FontWeight.BOLD,
                                    size=16,
                                    color=card_appearance["heading_color"],
                                    selectable=True,
                                ),
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            wrap=True,
                            spacing=10,
                            run_spacing=4,
                        ),
                        *body_texts,
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
                refresh_locations(include_ids=[location_id])
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
        existing = get_dc_rate(rate_id) if rate_id is not None else None
        if rate_id is not None and existing is None:
            status_text.value = "This rate record no longer exists."
            page.update()
            return
        pinned_location_id = (
            int(existing["location_id"])
            if existing and existing["location_id"] else None
        )
        # The dropdown reuses the bounded location list; a rate being edited
        # always keeps its own location pinned even beyond the first page.
        locations = refresh_locations(
            include_ids=(
                [pinned_location_id] if pinned_location_id is not None else None
            ),
        )

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
                qanoongoi_filter.value = ""
                mauza_filter.value = ""
                district_filter.value = ""
                tehsil_filter.value = ""
                town_filter.value = ""
                property_area_filter.value = ""
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
        # Import moves from file parsing -> worksheet selection -> column
        # mapping -> validated preview -> service commit, so every worksheet is
        # accounted for and spreadsheet columns are never guessed silently.
        # Multiple files can be selected at once; every file's worksheets join
        # one queue that is walked in order (developer request).
        try:
            files = await pick_import_file(
                action="schedule import",
                dialog_title="Import DC or FBR schedule (one or more files)",
                allowed_extensions=["csv", "xlsx", "ods"],
                allow_multiple=True,
            )
            if not files:
                return
            queue_items: list[dict[str, Any]] = []
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
                    ("qanoongoi", "Qanoongoi (optional)"), ("mauza", "Mauza (optional)"),
                    ("committee_town", "Town"),
                    ("revenue_circle", "Revenue circle"), ("property_area", "Property area"),
                    ("classification", "Classification"), ("locality", "Location"),
                    ("square_no", "Square no. (optional)"), ("kila_no", "Kila no. (optional)"),
                    ("khasra_no", "Khasra no. (optional)"), ("rate_value", "DC rate"),
                    ("rate_unit", "Unit"), ("structure_rate", "Structure rate"),
                ],
                "fbr": [
                    ("district", "District (optional)"), ("tehsil", "Tehsil (optional)"),
                    ("mauza", "Mauza (optional)"),
                    ("serial_no", "Serial no. (optional)"), ("revenue_circle", "Revenue circle"),
                    ("property_area", "Property area"), ("classification", "Classification"),
                    ("locality", "Location"), ("square_no", "Location (Square No.)"),
                    ("kila_no", "Kila no. (optional)"), ("khasra_no", "Khasra no. (optional)"),
                    ("rate_value", "Value per square foot (Rs.)"),
                    ("per_marla_value", "Per marla valuation (optional; defaults to square-foot value × 272)"),
                ],
            }
            for selected in files:
                payload = selected.bytes
                if payload is None and selected.path:
                    payload = Path(selected.path).read_bytes()
                if payload is None:
                    raise DCRateServiceError(
                        f"Could not read the selected spreadsheet: {selected.name}"
                    )
                sheets = read_schedule_sheets(payload, selected.name)
                for group in group_schedule_sheets(sheets):
                    queue_items.append({"file": selected, "group": group})

            def open_group_picker(queue_items):
                """Let the user review every worksheet layout before importing."""
                if len(queue_items) == 1:
                    open_mapping(queue_items, 0)
                    return
                checks = []
                error = ft.Text("", color="#B91C1C", size=12)
                for index, item in enumerate(queue_items, start=1):
                    group = item["group"]
                    check = ft.Checkbox(
                        value=True,
                        label=(
                            f"{item['file'].name} · Layout {index}: "
                            f"{', '.join(group['sheets'])} "
                            f"({len(group['rows']):,} row(s))"
                        ),
                    )
                    checks.append((item, check))
                def continue_import(_):
                    selected_groups = [item for item, check in checks if check.value]
                    if not selected_groups:
                        error.value = "Select at least one worksheet layout to import."
                        page.update()
                        return
                    close_dialog()
                    open_mapping(selected_groups, 0)
                page.show_dialog(ft.AlertDialog(
                    modal=True,
                    title=ft.Text(
                        f"Choose worksheets ({len(files)} file(s))"
                        if len(files) > 1 else f"Choose worksheets - {files[0].name}"
                    ),
                    content=ft.Column([
                        ft.Text(
                            f"The selected file(s) contain {len(queue_items)} column layout(s). "
                            "Every worksheet is listed; selected layouts are imported "
                            "one after another with their own column mapping.",
                            size=12,
                            color="#475569",
                        ),
                        *[
                            ft.Container(
                                padding=6,
                                bgcolor="#F8FAFC",
                                border=ft.Border.all(1, "#E2E8F0"),
                                border_radius=6,
                                content=ft.Column([
                                    check,
                                    ft.Text(
                                        "Columns: " + ", ".join(
                                            header or "(blank)" for header in group["headers"]
                                        ),
                                        size=11,
                                        color="#64748B",
                                    ),
                                ], spacing=2),
                            )
                            for item, check in checks
                        ],
                        error,
                    ], width=680, tight=True, scroll=ft.ScrollMode.AUTO),
                    actions=[
                        ft.TextButton("Cancel", on_click=lambda _: close_dialog()),
                        ft.Button("Continue", on_click=continue_import),
                    ],
                ))
                page.update()

            def open_mapping(queue, index):
                """Map and import one worksheet layout, then continue the queue."""
                item = queue[index]
                selected = item["file"]
                group = item["group"]
                headers = group["headers"]
                rows = group["rows"]
                row_numbers = group.get("row_numbers")
                row_sheets = group.get("row_sheets")
                guesses = suggested_column_mapping(headers, rows)
                header_counts: dict[str, int] = {}
                for header in headers:
                    key = (header or "").strip().casefold()
                    header_counts[key] = header_counts.get(key, 0) + 1
                source_options = [ft.dropdown.Option("", "Skip this field")] + [
                    ft.dropdown.Option(
                        str(i),
                        (header or f"Column {i + 1}")
                        + (
                            f" (column {i + 1})"
                            if header_counts[(header or "").strip().casefold()] > 1
                            else ""
                        ),
                    )
                    for i, header in enumerate(headers)
                ]
                profiles = list_duplicate_rule_profiles()
                selects = {}
                mapping_controls = ft.ResponsiveRow(run_spacing=4)
                mapping_error = ft.Text("", color="#B91C1C", size=12)

                storage_warning = ft.Text("", color="#B45309", size=11)

                def refresh_storage_warning(_=None):
                    # Collective (range/list) values kept in one record are
                    # deprecated: individual values search and value exactly.
                    storage_warning.value = (
                        "Warning: keeping collective values as one record could "
                        "lead to wrong results. 'Store each individual value' is "
                        "the recommended mode."
                        if (storage_mode.value or "individual") == "ranges"
                        else ""
                    )
                    page.update()

                storage_mode = ft.RadioGroup(
                    value=get_cadastral_storage_mode(),
                    content=ft.Row([
                        ft.Radio(value="individual", label="Store each individual value (recommended)"),
                        ft.Radio(value="ranges", label="Keep as ranges (deprecated)"),
                    ], wrap=True, spacing=12),
                )
                storage_mode.on_change = refresh_storage_warning
                # The developer controls the caps: a typed import cap applies
                # to the whole import (and per row); blank means no cap. The
                # checkbox alone removes the per-row expansion limit.
                expansion_cap_field = ft.TextField(
                    label="Import cap (expanded records; blank = no limit)",
                    width=340,
                )
                allow_large_expansion = ft.Checkbox(
                    label="Allow very large row expansion (no per-row limit)",
                    value=False,
                )
                allow_large_expansion_help = ft.Text(
                    "Per-row default limit: "
                    f"{MAX_EXPANDED_ROWS_PER_SOURCE:,} records. A typed cap also "
                    "applies per row; the checkbox alone removes the per-row "
                    "limit.",
                    size=11, color="#64748B",
                )

                def expansion_limits():
                    """Resolve the developer's per-row and whole-import caps."""
                    raw_cap = (expansion_cap_field.value or "").strip()
                    if raw_cap:
                        cap = int(raw_cap)
                        if cap < 0:
                            raise ValueError("The import cap cannot be negative.")
                        per_row = cap if allow_large_expansion.value else min(
                            MAX_EXPANDED_ROWS_PER_SOURCE, cap
                        )
                        return per_row, cap
                    per_row = 0 if allow_large_expansion.value else None
                    return per_row, None
                duplicate_policy = ft.RadioGroup(
                    value="check_now",
                    content=ft.Row([
                        ft.Radio(value="check_now", label="Check / remove duplicates now"),
                        ft.Radio(value="import_all", label="Import everything and clean later"),
                    ], wrap=True, spacing=12),
                )
                duplicate_rule_dropdown = ft.Dropdown(
                    label="Duplicate rule",
                    value=EXACT_ALL_REFERENCE,
                    options=_duplicate_rule_options(profiles),
                    width=680,
                )
                duplicate_help = ft.Text(
                    "The default rule removes a row only when every compared column "
                    "is 100% identical; the first row is kept. Choose another rule or "
                    "import everything to clean duplicates later.",
                    size=11,
                    color="#64748B",
                )

                def selected_profile() -> dict | None:
                    return _duplicate_rule_by_reference(
                        profiles, duplicate_rule_dropdown.value
                    )

                def refresh_rule_help(_=None):
                    duplicate_help.value = _duplicate_rule_help(selected_profile())
                    page.update()

                duplicate_rule_dropdown.on_select = refresh_rule_help

                def rebuild_rule_options(_=None):
                    scope = schedule.value or "rural_dc"
                    allowed = [
                        profile for profile in profiles
                        if profile["schedule_scope"] in (scope, "any")
                    ]
                    references = [profile["reference"] for profile in allowed]
                    duplicate_rule_dropdown.options = [
                        ft.dropdown.Option(profile["reference"], _duplicate_rule_label(profile))
                        for profile in allowed
                    ]
                    if duplicate_rule_dropdown.value not in references:
                        duplicate_rule_dropdown.value = (
                            EXACT_ALL_REFERENCE
                            if EXACT_ALL_REFERENCE in references
                            else (references[0] if references else None)
                        )
                    refresh_rule_help()

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
                    rebuild_rule_options()
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
                    warnings = preview.get("warnings", [])
                    applied_rule = preview.get("duplicate_rule") or {}
                    chosen_policy = duplicate_policy.value or "check_now"
                    chosen_storage = storage_mode.value or "individual"
                    chosen_per_row_cap, chosen_total_cap = expansion_limits()
                    import_year = ft.TextField(
                        label="Published year for this import (optional)",
                        hint_text="Example: 2026. Leave blank to use each row's date/year.",
                        width=300,
                    )
                    review_status = ft.Text("", size=12, color="#334155")

                    def issue_label(issue: dict) -> str:
                        sheet = issue.get("sheet")
                        prefix = f"{sheet} " if sheet else ""
                        return f"{prefix}row {issue['row']}"

                    issue_controls = []
                    for issue in issues:
                        invalid = issue["kind"].startswith("invalid")
                        issue_controls.append(ft.Container(
                            padding=8,
                            bgcolor="#FEF2F2" if invalid else "#FFFBEB",
                            border=ft.Border.all(1, "#FECACA" if invalid else "#FDE68A"),
                            border_radius=6,
                            content=ft.Column([
                                ft.Text(f"{issue_label(issue)} · {issue['kind']}: {issue['message']}",
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
                    warning_controls = []
                    for warning in warnings:
                        warning_controls.append(ft.Container(
                            padding=8,
                            bgcolor="#EFF6FF",
                            border=ft.Border.all(1, "#BFDBFE"),
                            border_radius=6,
                            content=ft.Column([
                                ft.Text(
                                    f"{issue_label(warning)} - {warning['kind']}: {warning['message']}",
                                    selectable=True,
                                    color="#1D4ED8",
                                    weight=ft.FontWeight.BOLD,
                                ),
                                ft.Text(
                                    "Source values: " + " | ".join(warning.get("source_values", [])),
                                    selectable=True,
                                    size=11,
                                    color="#1E3A8A",
                                ),
                            ], spacing=3),
                        ))
                    if not warning_controls:
                        warning_controls.append(ft.Text("No additional duplicate notes.", color="#166534"))
                    warning_preview = ft.ListView(controls=warning_controls, height=130, spacing=5)
                    valid_preview = ft.ListView(controls=[ft.Text(
                        f"{record.get('sheet') + ' ' if record.get('sheet') else ''}"
                        f"row {record['row']}: {record['location'].get('name')} · "
                        f"{record['classification'] or record['property_type']} · "
                        f"{record['rate_value']} {record['rate_unit']}",
                        size=12, color="#334155",
                    ) for record in candidates[:50]], height=130, spacing=3)
                    if not candidates:
                        valid_preview.controls.append(ft.Text("No valid rows are ready to import.", color="#64748B"))
                    elif len(candidates) > 50:
                        valid_preview.controls.append(ft.Text(f"…and {len(candidates) - 50} more valid rows.", size=11, color="#64748B"))
                    issue_preview = ft.ListView(controls=issue_controls, height=230, spacing=5)

                    async def export_issues(_=None):
                        try:
                            payload_bytes = export_import_issues(issues)
                            destination = await export_with_location(
                                action="skipped-row report export",
                                dialog_title="Save skipped-row report",
                                file_name=(
                                    f"import_issues_{selected.name.rsplit('.', 1)[0]}_"
                                    f"{date.today().isoformat()}.csv"
                                ),
                                allowed_extensions=["csv"],
                                payload=payload_bytes,
                            )
                            review_status.value = (
                                f"Saved {len(issues):,} issue row(s) to {destination}."
                                if destination else "Issue report export cancelled."
                            )
                        except Exception as exc:
                            review_status.value = f"Could not export issue report: {exc}"
                        page.update()

                    storage_label = (
                        "Ranges (one compact record per source row)"
                        if chosen_storage == "ranges"
                        else "Individual values (one record per value combination)"
                    )
                    policy_label = (
                        "Check and remove exact duplicates now"
                        if chosen_policy == "check_now"
                        else "Import everything; clean duplicates later"
                    )
                    content = ft.Column(controls=[
                        ft.Text(
                            f"File: {selected.name}\n"
                            f"Worksheet(s): {', '.join(group['sheets'])}\n"
                            f"Schedule: {schedule_name()}\n"
                            f"Duplicate rule: {applied_rule.get('name', 'all columns exactly identical')}\n"
                            f"Duplicate policy: {policy_label}\n"
                            f"Storage: {storage_label}\n"
                            f"Source rows read: {preview['rows_read']:,} | "
                            f"Records after storage: {preview.get('expanded_rows', len(candidates)):,} | "
                            f"Ready to import: {len(candidates):,} | "
                            f"Exact duplicates removed: {preview.get('duplicates_removed', 0):,} | "
                            f"Duplicates kept for later: {preview.get('duplicates_kept', 0):,} | "
                            f"Invalid rows: {preview.get('invalid_rows', 0):,}.\n"
                            "Every source row is accounted for in the counters above. "
                            "Existing rates will not be overwritten.",
                            selectable=True,
                        ),
                        import_year,
                        ft.Row([
                            ft.OutlinedButton(
                                "Export skipped-row report (CSV)",
                                on_click=export_issues,
                                disabled=not issues,
                            ),
                            review_status,
                        ], wrap=True, spacing=10),
                        ft.Text("Valid row preview", weight=ft.FontWeight.BOLD), valid_preview,
                        ft.Text("Rows needing attention", weight=ft.FontWeight.BOLD), issue_preview,
                        ft.Text("Additional duplicate notes", weight=ft.FontWeight.BOLD),
                        warning_preview,
                    ], width=680, tight=True, scroll=ft.ScrollMode.AUTO)

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
                        show_busy(
                            f"Importing {len(import_records):,} {schedule_name()} rate(s) "
                            f"from {selected.name}…"
                        )
                        status_text.value = (
                            f"Import running in the background: {len(import_records):,} valid "
                            f"{schedule_name()} rate(s). The page remains responsive."
                        )
                        page.update()

                        async def run_import_in_background():
                            nonlocal import_in_progress
                            next_opened = False
                            try:
                                try:
                                    set_cadastral_storage_mode(chosen_storage)
                                except Exception:
                                    # The preference must never block an import.
                                    pass
                                # Database writes happen on a worker thread so a
                                # large schedule does not block Flet's UI event loop.
                                outcome = await asyncio.to_thread(
                                    import_dc_rate_records,
                                    import_records,
                                    duplicate_policy=chosen_policy,
                                    duplicate_rule=duplicate_rule_dropdown.value,
                                    storage_mode=chosen_storage,
                                    max_per_source=chosen_per_row_cap,
                                    max_total=chosen_total_cap,
                                )
                                picker = await asyncio.to_thread(list_location_picker)
                                refresh_locations(
                                    picker["locations"],
                                    qanoongoi_values=picker["qanoongoi"],
                                    has_more=picker["has_more"],
                                )
                                schedule_filter.value = "all"
                                location_filter.value = "all"
                                unit_filter.value = "all"
                                published_year_filter.value = ""
                                search_field.value = ""
                                qanoongoi_filter.value = ""
                                mauza_filter.value = ""
                                square_filter.value = ""
                                kila_filter.value = ""
                                khasra_filter.value = ""
                                schedule_rates_refresh()
                                storage_note = (
                                    "stored as ranges"
                                    if chosen_storage == "ranges"
                                    else "stored as individual values"
                                )
                                status_text.value = (
                                    f"Imported {outcome['imported_count']:,} {schedule_name()} "
                                    f"rate(s) from {selected.name} ({storage_note}). "
                                    f"{outcome.get('expanded_count', outcome['imported_count']):,} "
                                    f"record(s) came from "
                                    f"{outcome.get('source_records', len(import_records)):,} "
                                    f"submitted row(s)."
                                )
                                skipped = len(issues) + len(outcome["issues"])
                                if skipped:
                                    status_text.value += (
                                        f" {skipped:,} invalid or exact-duplicate row(s) were not imported."
                                    )
                                if outcome.get("duplicates_kept"):
                                    status_text.value += (
                                        f" {outcome['duplicates_kept']:,} duplicate row(s) were kept "
                                        "for later cleanup."
                                    )
                                if index + 1 < len(queue):
                                    next_opened = True
                                    open_mapping(queue, index + 1)
                            except Exception as exc:
                                status_text.value = f"Schedule import failed: {exc}"
                            finally:
                                import_in_progress = False
                                hide_busy()
                                if not next_opened:
                                    page.update()

                        try:
                            page.run_task(run_import_in_background)
                        except Exception as exc:
                            import_in_progress = False
                            hide_busy()
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
                        per_row_cap, total_cap = expansion_limits()
                        preview = preview_mapped_schedule(
                            headers,
                            rows,
                            mapping,
                            schedule.value,
                            duplicate_policy=duplicate_policy.value or "check_now",
                            duplicate_rule=duplicate_rule_dropdown.value,
                            storage_mode=storage_mode.value or "individual",
                            max_per_source=per_row_cap,
                            max_total=total_cap,
                            row_numbers=row_numbers,
                            row_sheets=row_sheets,
                        )
                        close_dialog()
                        show_review(preview)
                    except Exception as exc:
                        mapping_error.value = str(exc)
                        page.update()

                page.show_dialog(ft.AlertDialog(modal=True, title=ft.Text(f"Map columns - {selected.name}"),
                    content=ft.Column(controls=[
                        ft.Text(
                            f"Worksheet(s) in this layout: {', '.join(group['sheets'])} "
                            f"({len(rows):,} row(s))",
                            size=12,
                            color="#475569",
                        ),
                        ft.Text("Schedule type"),
                        schedule,
                        mapping_controls,
                        ft.Text("Cadastral storage", weight=ft.FontWeight.BOLD),
                        ft.Text(
                            "Store each individual value (recommended) so every "
                            "Square/Kila value is searchable and valued exactly. "
                            "Keeping ranges is deprecated: collective values in "
                            "one record could lead to wrong results.",
                            size=11,
                            color="#64748B",
                        ),
                        storage_mode,
                        storage_warning,
                        expansion_cap_field,
                        allow_large_expansion,
                        allow_large_expansion_help,
                        ft.Text("Duplicate detection", weight=ft.FontWeight.BOLD),
                        duplicate_policy,
                        duplicate_rule_dropdown,
                        duplicate_help,
                        mapping_error,
                    ], width=680, tight=True, scroll=ft.ScrollMode.AUTO),
                    actions=[ft.TextButton("Cancel", on_click=lambda _: close_dialog()),
                             ft.Button("Preview rows", on_click=preview_mapping)]))
                rebuild_mapping()
                page.update()

            open_group_picker(queue_items)
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
            qanoongoi=qanoongoi_filter.value or "",
            district=district_filter.value or "",
            tehsil=tehsil_filter.value or "",
            committee_town=town_filter.value or "",
            property_area=property_area_filter.value or "",
            square_no=square_filter.value or "",
            kila_no=kila_filter.value or "",
            khasra_no=khasra_filter.value or "",
        )

    def open_duplicate_rules_dialog(_=None):
        profiles = list_duplicate_rule_profiles()
        rules_list = ft.ListView(height=360, spacing=6)
        manager_status = ft.Text("", size=12, color="#334155")

        def refresh_rules(message: str = ""):
            nonlocal profiles
            profiles = list_duplicate_rule_profiles()
            rules_list.controls.clear()
            for profile in profiles:
                origin = "Built-in" if profile.get("is_builtin") else "Custom"
                actions = []
                if profile.get("is_builtin"):
                    actions.append(ft.TextButton(
                        "Copy",
                        on_click=lambda _, item=profile: open_rule_editor(item, as_copy=True),
                    ))
                else:
                    actions.append(ft.TextButton(
                        "Edit",
                        on_click=lambda _, item=profile: open_rule_editor(item),
                    ))
                    actions.append(ft.TextButton(
                        "Delete",
                        on_click=lambda _, item=profile: confirm_rule_delete(item),
                    ))
                rules_list.controls.append(ft.Container(
                    padding=8,
                    bgcolor="#F8FAFC",
                    border=ft.Border.all(1, "#CBD5E1"),
                    border_radius=6,
                    content=ft.Row([
                        ft.Column([
                            ft.Text(profile["name"], weight=ft.FontWeight.BOLD),
                            ft.Text(
                                f"{origin} | {DUPLICATE_SCOPE_LABELS.get(profile['schedule_scope'], profile['schedule_scope'])} | "
                                f"{DUPLICATE_DATE_LABELS.get(profile['date_mode'], profile['date_mode'])} | "
                                f"{DUPLICATE_ACTION_LABELS.get(profile['import_action'], profile['import_action'])} | "
                                f"{len(profile.get('match_fields', []))} match field(s)",
                                size=11,
                                color="#475569",
                            ),
                            ft.Text(profile.get("description") or "No description.", size=11, color="#64748B"),
                        ], spacing=2, expand=True),
                        ft.Row(actions, spacing=2),
                    ], vertical_alignment=ft.CrossAxisAlignment.START),
                ))
            if not rules_list.controls:
                rules_list.controls.append(ft.Text("No duplicate rules are available.", color="#64748B"))
            manager_status.value = message
            page.update()

        def confirm_rule_delete(profile: dict):
            def delete_rule(_):
                try:
                    delete_duplicate_rule_profile(profile["custom_id"])
                except Exception as exc:
                    manager_status.value = f"Could not delete duplicate rule: {exc}"
                    page.update()
                    return
                page.pop_dialog()
                refresh_rules(f"Deleted custom duplicate rule '{profile['name']}'.")

            page.show_dialog(ft.AlertDialog(
                modal=True,
                title=ft.Text("Delete duplicate rule"),
                content=ft.Text(
                    f"Delete custom duplicate rule '{profile['name']}'? Existing rate records "
                    "are not affected."
                ),
                actions=[
                    ft.TextButton("Cancel", on_click=lambda _: page.pop_dialog()),
                    ft.Button("Delete rule", on_click=delete_rule),
                ],
            ))
            page.update()

        def open_rule_editor(profile: dict | None = None, *, as_copy: bool = False):
            source = profile or {}
            is_update = bool(profile and not profile.get("is_builtin") and not as_copy)
            selected_fields = set(source.get("match_fields", []))
            default_name = source.get("name", "")
            if as_copy and default_name:
                default_name = f"{default_name} (copy)"

            name_field = ft.TextField(label="Rule name", value=default_name, width=640)
            scope_field = ft.Dropdown(
                label="Schedule scope",
                value=source.get("schedule_scope", "any"),
                options=[
                    ft.dropdown.Option(key, label)
                    for key, label in DUPLICATE_SCOPE_LABELS.items()
                ],
                width=300,
            )
            date_field = ft.Dropdown(
                label="Effective-date matching",
                value=source.get("date_mode", "overlap"),
                options=[
                    ft.dropdown.Option(key, label)
                    for key, label in DUPLICATE_DATE_LABELS.items()
                ],
                width=300,
            )
            action_field = ft.Dropdown(
                label="When a duplicate is found",
                value=source.get("import_action", "skip"),
                options=[
                    ft.dropdown.Option(key, label)
                    for key, label in DUPLICATE_ACTION_LABELS.items()
                ],
                width=300,
            )
            description_field = ft.TextField(
                label="Description (optional)",
                value=source.get("description", ""),
                multiline=True,
                min_lines=1,
                max_lines=3,
                width=640,
            )
            field_controls: dict[str, ft.Checkbox] = {}
            groups = []
            for group_name, fields in MATCH_FIELD_GROUPS:
                checks = []
                for field, label in fields:
                    control = ft.Checkbox(label=label, value=field in selected_fields)
                    field_controls[field] = control
                    checks.append(control)
                groups.append(ft.Column([
                    ft.Text(group_name, weight=ft.FontWeight.BOLD, size=12),
                    ft.ResponsiveRow(
                        [ft.Container(control, col={"xs": 12, "md": 6}) for control in checks],
                        run_spacing=0,
                    ),
                ], spacing=2))
            editor_error = ft.Text("", color="#B91C1C", size=12)

            def save_rule(_):
                try:
                    match_fields = [
                        field for field, control in field_controls.items() if control.value
                    ]
                    values = {
                        "name": name_field.value,
                        "schedule_scope": scope_field.value,
                        "match_fields": match_fields,
                        "date_mode": date_field.value,
                        "import_action": action_field.value,
                        "description": description_field.value,
                    }
                    if is_update:
                        saved = update_duplicate_rule_profile(
                            profile["custom_id"], **values
                        )
                    else:
                        saved = create_duplicate_rule_profile(**values)
                except Exception as exc:
                    editor_error.value = str(exc)
                    page.update()
                    return
                page.pop_dialog()
                refresh_rules(f"Saved duplicate rule '{saved['name']}'.")

            title = "Edit duplicate rule" if is_update else "New duplicate rule"
            page.show_dialog(ft.AlertDialog(
                modal=True,
                title=ft.Text(title),
                content=ft.Column([
                    name_field,
                    ft.Row([scope_field, date_field, action_field], wrap=True, spacing=8),
                    description_field,
                    ft.Text("Fields compared by this rule", weight=ft.FontWeight.BOLD),
                    *groups,
                    editor_error,
                ], width=680, height=480, spacing=8, scroll=ft.ScrollMode.AUTO),
                actions=[
                    ft.TextButton("Cancel", on_click=lambda _: page.pop_dialog()),
                    ft.Button("Save rule", on_click=save_rule),
                ],
            ))
            page.update()

        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("Duplicate detection rules"),
            content=ft.Column([
                ft.Text(
                    "Built-in rules cover common rural, urban, and FBR schedules. Copy one to "
                    "customize it, or create a rule from scratch. Import and removal dialogs "
                    "require an explicit rule selection.",
                    size=12,
                ),
                rules_list,
                manager_status,
            ], width=780, height=470, spacing=8, scroll=ft.ScrollMode.AUTO),
            actions=[
                ft.TextButton("Close", on_click=lambda _: close_dialog()),
                ft.Button("New rule", on_click=lambda _: open_rule_editor()),
            ],
        ))
        refresh_rules()
        page.update()

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
        profiles = list_duplicate_rule_profiles()
        duplicate_rule = ft.Dropdown(
            label="Duplicate rule (required)",
            value=None,
            options=_duplicate_rule_options(profiles),
            width=640,
        )
        duplicate_help = ft.Text(
            "Select a duplicate rule before scanning.", size=11, color="#64748B"
        )
        scan_summary = ft.Text("Select a duplicate rule, then scan existing records.", size=12)
        scan_progress = ft.ProgressBar(visible=False)
        duplicate_list = ft.ListView(height=220, spacing=4)
        remove_button = ft.Button("Export and remove duplicates", disabled=True)
        scan_button = ft.OutlinedButton("Scan for duplicates")
        scanned_count = 0

        def cleanup_profile():
            return _duplicate_rule_by_reference(profiles, duplicate_rule.value)

        def show_duplicates(records):
            nonlocal scanned_count
            scanned_count = len(records)
            profile = cleanup_profile()
            rule_name = profile["name"] if profile else "selected rule"
            duplicate_list.controls.clear()
            if not records:
                duplicate_list.controls.append(ft.Text("No duplicate schedule rows were found."))
                scan_summary.value = (
                    f"No duplicates found under '{rule_name}'. Your records were not changed."
                )
                remove_button.disabled = True
                remove_button.text = "Export and remove duplicates"
            else:
                scan_summary.value = (
                    f"Found {scanned_count:,} extra row(s) under '{rule_name}'. The lowest "
                    "record ID in each duplicate group will be kept. Removed rows are exported first."
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

        def duplicate_rule_changed(_):
            nonlocal scanned_count
            scanned_count = 0
            duplicate_help.value = _duplicate_rule_help(cleanup_profile())
            duplicate_list.controls.clear()
            scan_summary.value = "Rule changed. Scan again before removing anything."
            remove_button.disabled = True
            remove_button.text = "Export and remove duplicates"
            page.update()

        async def run_scan():
            scan_button.disabled = True
            scan_progress.visible = True
            scan_summary.value = "Scanning schedule records in the background..."
            page.update()
            try:
                records = await asyncio.to_thread(
                    find_duplicate_dc_rates, duplicate_rule.value
                )
                show_duplicates(records)
            except Exception as exc:
                scan_summary.value = f"Could not scan for duplicates: {exc}"
                remove_button.disabled = True
            finally:
                scan_button.disabled = False
                scan_progress.visible = False
                page.update()

        def scan(_):
            if not duplicate_rule.value:
                scan_summary.value = "Select a duplicate rule before scanning."
                page.update()
                return
            if hasattr(page, "run_task"):
                page.run_task(run_scan)
            else:
                scan_button.disabled = True
                try:
                    show_duplicates(find_duplicate_dc_rates(duplicate_rule.value))
                except Exception as exc:
                    scan_summary.value = f"Could not scan for duplicates: {exc}"
                finally:
                    scan_button.disabled = False
                    page.update()

        async def run_removal():
            remove_button.disabled = True
            scan_button.disabled = True
            scan_progress.visible = True
            scan_summary.value = "Writing the report and removing duplicate rows..."
            page.update()
            try:
                removed_count, report_path = await asyncio.to_thread(
                    remove_duplicate_dc_rates_with_report,
                    report_format.value,
                    duplicate_rule.value,
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
            finally:
                scan_button.disabled = False
                scan_progress.visible = False
                page.update()

        def remove_duplicates(_):
            if scanned_count == 0:
                return
            if hasattr(page, "run_task"):
                page.run_task(run_removal)
            else:
                scan_summary.value = "Background task support is required for safe duplicate removal."
                page.update()

        duplicate_rule.on_change = duplicate_rule_changed
        duplicate_rule.on_select = duplicate_rule_changed
        scan_button.on_click = scan
        remove_button.on_click = remove_duplicates
        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("Find and remove duplicate schedule rows"),
            content=ft.Column([
                ft.Text(
                    "Choose the fields and date behavior that define a duplicate. The lowest "
                    "record ID in each duplicate group is kept. Removed rows are exported first.",
                    size=12,
                ),
                duplicate_rule,
                duplicate_help,
                report_format,
                scan_summary,
                scan_progress,
                duplicate_list,
            ], width=720, height=460, scroll=ft.ScrollMode.AUTO, spacing=8),
            actions=[
                ft.TextButton("Close", on_click=lambda _: close_dialog()),
                scan_button,
                remove_button,
            ],
        ))
        page.update()

    def make_export_handler(file_format: str):
        """Ask how ranges should be exported, then export filtered records."""
        async def export_schedule(_=None):
            reference_mode = ft.RadioGroup(
                value="ranges",
                content=ft.Column([
                    ft.Radio(value="ranges", label="Keep ranges (compact)"),
                    ft.Radio(value="individual", label="Expand ranges into individual values"),
                    ft.Radio(value="stored", label="Use values exactly as stored"),
                ], spacing=2, tight=True),
            )
            export_error = ft.Text("", size=12, color="#B91C1C")
            export_count = ft.Text("Counting matching records…", size=12, color="#475569")

            async def load_export_count():
                try:
                    count = await asyncio.to_thread(records_matching_current_filters)
                    if count:
                        export_count.value = (
                            f"{len(count):,} record(s) match the current filters "
                            "and will be exported."
                        )
                    else:
                        export_count.value = (
                            "No schedule rows match the current filters. "
                            "Adjust the filters before exporting."
                        )
                    page.update()
                except Exception as exc:
                    export_count.value = f"Could not count matching records: {exc}"
                    page.update()

            def confirm_export(_):
                chosen_mode = reference_mode.value or "ranges"
                close_dialog()
                show_busy(
                    f"Preparing {file_format.upper()} export…",
                    "Filtering records and serializing the spreadsheet.",
                )
                async def run_export():
                    try:
                        records = await asyncio.to_thread(records_matching_current_filters)
                        if not records:
                            status_text.value = (
                                "No schedule rows match the current filters — "
                                "nothing was exported. Adjust the filters (or clear "
                                "them) and try again."
                            )
                            return
                        payload = await asyncio.to_thread(
                            export_schedule_records,
                            records,
                            file_format,
                            reference_mode=chosen_mode,
                        )
                        destination = await export_with_location(
                            action=f"valuation schedule {file_format.upper()} export",
                            dialog_title=f"Export valuation schedules as {file_format.upper()}",
                            file_name=f"valuation_schedules_{date.today().isoformat()}.{file_format}",
                            allowed_extensions=[file_format],
                            payload=payload,
                        )
                        if destination:
                            mode_label = {
                                "ranges": "as compact ranges",
                                "individual": "with individual values",
                                "stored": "as stored",
                            }.get(chosen_mode, "")
                            status_text.value = (
                                f"Exported {len(records):,} record(s) {mode_label} to {destination}."
                            )
                        else:
                            status_text.value = "Export cancelled."
                    except Exception as exc:
                        status_text.value = f"Could not export schedule records: {exc}"
                    finally:
                        hide_busy()
                        page.update()
                try:
                    page.run_task(run_export)
                except Exception as exc:
                    hide_busy()
                    status_text.value = f"Could not start the export: {exc}"
                    page.update()

            page.show_dialog(ft.AlertDialog(
                modal=True,
                title=ft.Text(f"Export {file_format.upper()} - choose cadastral format"),
                content=ft.Column([
                    ft.Text(
                        "Choose whether Square/Kila/Khasra values should stay as ranges "
                        "or be expanded into individual exported rows. Stored data is "
                        "never changed by exporting.",
                        size=12,
                        color="#475569",
                    ),
                    reference_mode,
                    export_count,
                    export_error,
                ], width=460, tight=True),
                actions=[
                    ft.TextButton("Cancel", on_click=lambda _: close_dialog()),
                    ft.Button("Export", on_click=confirm_export),
                ],
            ))
            page.update()
            if hasattr(page, "run_task"):
                page.run_task(load_export_count)
        return export_schedule

    def open_cadastral_selection_dialog(_=None, *, khewat_record=None):
        """Select cadastral values or edit a saved Mauza-scoped Khewat."""
        try:
            mauza_contexts = list_mauza_contexts()
        except Exception as exc:
            status_text.value = f"Could not load Mauza choices: {exc}"
            page.update()
            return
        if not mauza_contexts:
            status_text.value = "Add or import a schedule with a Mauza before selecting squares and Kilas."
            page.update()
            return
        if khewat_record:
            record_context = {field: khewat_record.get(field) for field in MAUZA_CONTEXT_FIELDS}
            if record_context not in mauza_contexts:
                mauza_contexts.append(record_context)

        if khewat_record:
            matching_context = next(
                (index for index, item in enumerate(mauza_contexts)
                 if item == record_context), 0,
            )
        else:
            matching_context = next(
                (index for index, item in enumerate(mauza_contexts)
                 if (item.get("mauza_name") or "").casefold() == (mauza_filter.value or "").casefold()),
                0,
            )
        initial_context = mauza_contexts[matching_context]

        def field_suggestions(field_name: str, query: str = ""):
            """Filter choices by the other location fields, then by typed text."""
            criteria = {
                "mauza_name": (mauza_field.value or "").strip(),
                "district": (district_field.value or "").strip(),
                "tehsil": (tehsil_field.value or "").strip(),
            }
            candidates = []
            for context in mauza_contexts:
                if any(
                    name != field_name and criteria[name]
                    and criteria[name].casefold() not in (context.get(name) or "").casefold()
                    for name in criteria
                ):
                    continue
                value = context.get(field_name) or ""
                if query.casefold() in value.casefold() and value:
                    candidates.append(value)
            return sorted(set(candidates), key=str.casefold)

        mauza_field = ft.AutoComplete(
            value=initial_context.get("mauza_name") or "", width=240,
            suggestions_max_height=240, disabled=bool(khewat_record),
        )
        district_field = ft.AutoComplete(
            value=initial_context.get("district") or "", width=220,
            suggestions_max_height=240, disabled=bool(khewat_record),
        )
        tehsil_field = ft.AutoComplete(
            value=initial_context.get("tehsil") or "", width=220,
            suggestions_max_height=240, disabled=bool(khewat_record),
        )

        def refresh_location_suggestions(_=None):
            for field, key in ((mauza_field, "mauza_name"),
                               (district_field, "district"), (tehsil_field, "tehsil")):
                field.suggestions = [
                    ft.AutoCompleteSuggestion(key=value, value=value)
                    for value in field_suggestions(key, field.value or "")
                ]
            page.update()

        refresh_location_suggestions()
        square_search = ft.TextField(label="Search squares", expand=True)
        kila_search = ft.TextField(label="Search Kilas", expand=True)
        square_checks = ft.Column(height=190, spacing=0, scroll=ft.ScrollMode.AUTO)
        kila_checks = ft.Column(height=190, spacing=0, scroll=ft.ScrollMode.AUTO)
        summary_text = ft.Text("Select a Mauza and mark one or more square/Kila values.", size=12)
        summary_table = ft.DataTable(
            columns=[ft.DataColumn(label="Summary"), ft.DataColumn(label="Value")],
            rows=[],
            column_spacing=22,
            visible=False,
        )
        summary_table_button = ft.TextButton("Show table")
        khewat_summary_table = ft.DataTable(
            columns=[ft.DataColumn(label=ft.Text(label)) for _key, label in (
                ("serial_no", "Sr no"), ("mauza", "Mauza"), ("square_no", "Square no"),
                ("kila_no", "Kila no"), ("khasra_no", "Khasra no"),
                ("unit", "Unit"), ("schedule", "Schedule"),
            )],
            rows=[], column_spacing=12, horizontal_margin=8,
        )
        khewat_summary_rows = []
        summary_sort_field = ft.Dropdown(
            label="Sort summary by", value="serial_no", width=180,
            options=[ft.dropdown.Option(key, label) for key, label in (
                ("serial_no", "Sr no"), ("mauza", "Mauza"), ("square_no", "Square no"),
                ("kila_no", "Kila no"), ("khasra_no", "Khasra no"),
                ("unit", "Unit"), ("schedule", "Schedule"),
            )],
        )
        summary_sort_direction = ft.TextButton("Ascending")
        selection_count = ft.Text("No cadastral values selected.", size=12, color="#475569")
        assignment_status = ft.Text("", size=12, color="#475569")
        saved_khewats = ft.Column(height=100, spacing=3, scroll=ft.ScrollMode.AUTO)
        khewat_number = ft.TextField(
            label="Khewat number",
            hint_text="For example: 30/1 or 30/2/1",
            max_length=100,
            value=khewat_record.get("khewat_number") if khewat_record else "",
        )
        rhz_year = ft.Dropdown(
            label="RHZ year",
            value=khewat_record.get("rhz_year") if khewat_record else None,
            options=[ft.dropdown.Option(year, year) for year in list_rhz_years()],
            enable_filter=True,
            editable=True,
            menu_height=320,
            width=220,
        )
        khewat_size = ft.TextField(
            label="Khewat size (optional, K-M-F)",
            hint_text="For example: 12-4-0",
            value=khewat_record.get("khewat_size") if khewat_record else "",
            width=240,
        )
        selected_values = {
            "square": set(khewat_record.get("squares", [])) if khewat_record else set(),
            "kila": set(khewat_record.get("kilas", [])) if khewat_record else set(),
        }
        available_values = {"square": [], "kila": []}

        def selected_context():
            mauza = (mauza_field.value or "").strip()
            district = (district_field.value or "").strip()
            tehsil = (tehsil_field.value or "").strip()
            if not mauza:
                raise DCRateServiceError("Choose a Mauza before loading or saving references.")
            matches = [item for item in mauza_contexts
                       if (item.get("mauza_name") or "").casefold() == mauza.casefold()]
            if district:
                matches = [item for item in matches
                           if (item.get("district") or "").casefold() == district.casefold()]
            if tehsil:
                matches = [item for item in matches
                           if (item.get("tehsil") or "").casefold() == tehsil.casefold()]
            if not matches:
                raise DCRateServiceError("Select a Mauza, District, and Tehsil combination from the suggestions.")
            return matches[0]

        def update_selection_count():
            squares = len(selected_values["square"])
            kilas = len(selected_values["kila"])
            selection_count.value = f"Selected: {squares} square(s), {kilas} Kila(s)."

        def render_khewat_summary():
            """Refresh the sortable, export-ready details for selected references."""
            nonlocal khewat_summary_rows
            try:
                khewat_summary_rows = summarize_mauza_references(
                    context=selected_context(),
                    square_numbers=sorted(selected_values["square"]),
                    kila_numbers=sorted(selected_values["kila"]),
                ) if selected_values["square"] or selected_values["kila"] else []
                field = summary_sort_field.value or "serial_no"
                reverse = summary_sort_direction.content == "Descending"
                khewat_summary_rows.sort(
                    key=lambda row: str(row.get(field) or "").casefold(), reverse=reverse
                )
                for serial_no, row in enumerate(khewat_summary_rows, start=1):
                    row["serial_no"] = serial_no
                khewat_summary_table.rows = [ft.DataRow(cells=[
                    ft.DataCell(ft.Text(str(row.get(key) or "")))
                    for key in ("serial_no", "mauza", "square_no", "kila_no", "khasra_no", "unit", "schedule")
                ]) for row in khewat_summary_rows]
            except Exception as exc:
                assignment_status.value = f"Could not build Khewat summary: {exc}"

        def change_summary_sort(_=None):
            render_khewat_summary()
            page.update()

        def toggle_summary_sort_direction(_=None):
            summary_sort_direction.content = (
                "Descending" if summary_sort_direction.content == "Ascending" else "Ascending"
            )
            render_khewat_summary()
            page.update()

        async def export_khewat_rows(file_format: str):
            try:
                render_khewat_summary()
                payload = export_khewat_summary(khewat_summary_rows, file_format)
                destination = await export_with_location(
                    action="Khewat summary export",
                    dialog_title="Export Khewat summary",
                    file_name=f"khewat_summary_{date.today().isoformat()}.{file_format}",
                    allowed_extensions=[file_format],
                    payload=payload,
                )
                assignment_status.value = (
                    f"Exported Khewat summary to {destination}." if destination
                    else "Export cancelled."
                )
            except Exception as exc:
                assignment_status.value = f"Could not export Khewat summary: {exc}"
            page.update()

        def export_khewat_handler(file_format: str):
            async def handler(_=None):
                await export_khewat_rows(file_format)
            return handler

        async def save_khewat_template(_=None):
            try:
                payload = export_khewat_import_template("xlsx")
                destination = await export_with_location(
                    action="Khewat import template export",
                    dialog_title="Save Khewat import template",
                    file_name="khewat_import_template.xlsx",
                    allowed_extensions=["xlsx"],
                    payload=payload,
                )
                assignment_status.value = (
                    f"Saved Khewat import template to {destination}." if destination
                    else "Template download cancelled."
                )
            except Exception as exc:
                assignment_status.value = f"Could not create import template: {exc}"
            page.update()

        async def import_khewat_file(_=None):
            try:
                selected_files = await pick_import_file(
                    action="Khewat import",
                    dialog_title="Import Khewats",
                    allowed_extensions=["csv", "xlsx", "ods"],
                    with_data=False,
                )
                if not selected_files:
                    return
                selected_file = selected_files[0]
                payload = getattr(selected_file, "bytes", None)
                if payload is None and getattr(selected_file, "path", None):
                    payload = Path(selected_file.path).read_bytes()
                if payload is None:
                    raise ValueError("The selected file could not be read.")
                headers, rows = read_schedule_spreadsheet(payload, selected_file.name)
                field_labels = [
                    ("mauza", "Mauza (required)"), ("district", "District (optional)"),
                    ("tehsil", "Tehsil (optional)"), ("khewat_number", "Khewat number (required)"),
                    ("rhz_year", "RHZ year (optional)"), ("square_no", "Square no"),
                    ("kila_no", "Kila no"),
                ]
                mapping_controls = {}
                for key, label in field_labels:
                    mapping_controls[key] = ft.Dropdown(
                        label=label,
                        value="none",
                        options=[ft.dropdown.Option("none", "Not mapped")]
                        + [ft.dropdown.Option(str(i), header or f"Column {i + 1}")
                           for i, header in enumerate(headers)],
                        expand=True,
                    )
                suggestions = {
                    "mauza": ("mauza",), "district": ("district",), "tehsil": ("tehsil",),
                    "khewat_number": ("khewat_number", "khewat_no", "khewat"),
                    "rhz_year": ("rhz_year", "year"), "square_no": ("square_no", "square"),
                    "kila_no": ("kila_no", "kila"),
                }
                for key, needles in suggestions.items():
                    for index, header in enumerate(headers):
                        normalized_header = "".join(ch for ch in header.casefold() if ch.isalnum())
                        if any("".join(ch for ch in needle if ch.isalnum()) in normalized_header
                               for needle in needles):
                            mapping_controls[key].value = str(index)
                            break

                def commit_import(_event=None):
                    try:
                        mapping = {
                            key: int(control.value) if control.value != "none" else None
                            for key, control in mapping_controls.items()
                        }
                        preview = preview_khewat_import(headers, rows, mapping)
                        contexts = list_mauza_contexts()
                        grouped = {}
                        issues = list(preview["issues"])
                        latest_year_defaults = set()
                        for record in preview["valid_records"]:
                            row_number = int(record.get("_source_row", "0"))
                            matches = [ctx for ctx in contexts if (ctx.get("mauza_name") or "").casefold()
                                       == record["mauza"].casefold()]
                            if record["district"]:
                                matches = [ctx for ctx in matches if (ctx.get("district") or "").casefold()
                                           == record["district"].casefold()]
                            if record["tehsil"]:
                                matches = [ctx for ctx in matches if (ctx.get("tehsil") or "").casefold()
                                           == record["tehsil"].casefold()]
                            if len(matches) != 1:
                                issues.append({"row": row_number, "reason": (
                                    "Mauza context is ambiguous; map District/Tehsil or confirm the Mauza exists."
                                    if matches else "Mauza does not exist in saved schedules or Khewats."
                                )})
                                continue
                            context = matches[0]
                            year = record["rhz_year"]
                            if not year:
                                year = (rhz_year.text or rhz_year.value or "").strip()
                            if not year:
                                year = latest_rhz_year(context) or ""
                                if year:
                                    latest_year_defaults.add((context.get("mauza_name") or "", year))
                            if not year:
                                issues.append({"row": row_number, "reason": "RHZ year is missing and this Mauza has no saved year."})
                                continue
                            try:
                                year = normalize_rhz_year(year)
                            except Exception as exc:
                                issues.append({"row": row_number, "reason": str(exc)})
                                continue
                            key = (tuple(context.get(field) or "" for field in MAUZA_CONTEXT_FIELDS),
                                   record["khewat_number"], year)
                            group = grouped.setdefault(key, {
                                "context": context, "khewat_number": record["khewat_number"],
                                "rhz_year": year, "square_numbers": [], "kila_numbers": [],
                            })
                            if record["square_no"]:
                                group["square_numbers"].append(record["square_no"])
                            if record["kila_no"]:
                                group["kila_numbers"].append(record["kila_no"])
                        if issues:
                            details = "\n".join(f"Row {item['row']}: {item['reason']}" for item in issues[:12])
                            assignment_status.value = (
                                f"Import stopped; fix {len(issues)} invalid row(s) before saving.\n{details}"
                            )
                            page.pop_dialog()
                            page.update()
                            return
                        def persist_import(_confirm=None):
                            try:
                                outcome = import_khewat_assignments(list(grouped.values()))
                                assignment_status.value = (
                                    f"Imported {outcome['assignments']} Khewat group(s), created "
                                    f"{outcome['created']} record(s), added {outcome['references_added']} reference(s)."
                                )
                                render_saved_khewats()
                                render_khewat_summary()
                                page.pop_dialog()
                                page.update()
                            except Exception as exc:
                                assignment_status.value = f"Khewat import failed: {exc}"
                                page.update()

                        if latest_year_defaults:
                            mauza_years = ", ".join(
                                f"{mauza}: {year}" for mauza, year in sorted(latest_year_defaults)
                            )
                            page.show_dialog(ft.AlertDialog(
                                modal=True,
                                title=ft.Text("Use latest saved RHZ year for blank rows?"),
                                content=ft.Text(
                                    f"Some imported rows have no year. Use these latest Mauza years? {mauza_years}"
                                ),
                                actions=[
                                    ft.TextButton("Cancel", on_click=lambda _: page.pop_dialog()),
                                    ft.Button("Continue import", on_click=lambda _: (
                                        page.pop_dialog(), persist_import()
                                    )),
                                ],
                            ))
                        else:
                            persist_import()
                    except Exception as exc:
                        assignment_status.value = f"Khewat import failed: {exc}"
                        page.update()

                page.show_dialog(ft.AlertDialog(
                    modal=True, title=ft.Text("Map Khewat import columns"),
                    content=ft.Column([
                        ft.Text("Map Mauza and Khewat number, plus at least one Square/Kila column. "
                                "RHZ year can be supplied here or from the selected year field. "
                                "Rows matching Mauza + Khewat + RHZ year add references to that record; "
                                "existing references are retained."),
                        *mapping_controls.values(),
                    ], width=620, height=520, spacing=8, scroll=ft.ScrollMode.AUTO),
                    actions=[
                        ft.TextButton("Cancel", on_click=lambda _: page.pop_dialog()),
                        ft.Button("Validate and import", on_click=commit_import),
                    ],
                ))
                page.update()
            except Exception as exc:
                assignment_status.value = f"Could not prepare Khewat import: {exc}"
                page.update()

        summary_sort_field.on_select = change_summary_sort
        summary_sort_direction.on_click = toggle_summary_sort_direction

        def render_reference_checks(kind: str):
            values = available_values[kind]
            query = (square_search.value if kind == "square" else kila_search.value) or ""
            filtered_values = [value for value in values if query.casefold() in value.casefold()]
            target = square_checks if kind == "square" else kila_checks
            target.controls.clear()
            if not filtered_values:
                target.controls.append(ft.Text("No matching values for this Mauza.", size=12, color="#64748B"))
            for value in filtered_values:
                target.controls.append(ft.Checkbox(
                    label=value,
                    value=value in selected_values[kind],
                    on_change=lambda event, item=value, category=kind: update_reference_selection(
                        category, item, event
                    ),
                ))

        def update_reference_selection(kind: str, value: str, event):
            if event.control.value:
                selected_values[kind].add(value)
            else:
                selected_values[kind].discard(value)
            update_selection_count()
            render_khewat_summary()
            summary_text.value = "Selection changed. Choose Show summary to refresh the totals."
            page.update()

        def toggle_summary_table(_=None):
            summary_table.visible = not summary_table.visible
            summary_table_button.content = "Hide table" if summary_table.visible else "Show table"
            page.update()

        summary_table_button.on_click = toggle_summary_table

        def render_saved_khewats():
            saved_khewats.controls.clear()
            for record in list_khewats_for_mauza(selected_context()):
                saved_khewats.controls.append(ft.Text(
                    f"Khewat {record['khewat_number']} | RHZ {record.get('rhz_year') or 'year unknown'} "
                    f"| Size: {record.get('khewat_size') or '-'} "
                    f"| Squares: {', '.join(record['squares']) or '-'} "
                    f"| Kilas: {', '.join(record['kilas']) or '-'}",
                    size=12,
                ))
            if not saved_khewats.controls:
                saved_khewats.controls.append(ft.Text("No Khewat assignments saved for this Mauza yet.", size=12))

        def load_mauza_references(_=None):
            try:
                context = selected_context()
                if not khewat_record:
                    selected_values["square"].clear()
                    selected_values["kila"].clear()
                available_values["square"] = list_mauza_cadastral_values(context, "square_no")
                available_values["kila"] = list_mauza_cadastral_values(context, "kila_no")
                if khewat_record:
                    available_values["square"] = sorted(
                        set(available_values["square"]) | set(khewat_record.get("squares", []))
                    )
                    available_values["kila"] = sorted(
                        set(available_values["kila"]) | set(khewat_record.get("kilas", []))
                    )
                render_reference_checks("square")
                render_reference_checks("kila")
                render_saved_khewats()
                render_khewat_summary()
                assignment_status.value = ""
                page.update()
            except Exception as exc:
                summary_text.value = f"Could not load cadastral values: {exc}"
                page.update()

        def clear_selection(_=None):
            selected_values["square"].clear()
            selected_values["kila"].clear()
            render_reference_checks("square")
            render_reference_checks("kila")
            update_selection_count()
            summary_text.value = "Selection cleared."
            page.update()

        def show_selection_summary(_=None):
            try:
                raw_year = (published_year_filter.value or "").strip()
                year = int(raw_year) if raw_year else None
                if year is not None and not 1900 <= year <= 2200:
                    raise ValueError("Published year must be between 1900 and 2200.")
                schedule_type, _land_type = selected_schedule_filters()
                result = summarize_mauza_cadastral_selection(
                    context=selected_context(),
                    square_numbers=sorted(selected_values["square"]),
                    kila_numbers=sorted(selected_values["kila"]),
                    schedule_type=schedule_type,
                    rate_unit=unit_filter.value if unit_filter.value != "all" else None,
                    published_year=year,
                )
                table_values = [
                    ("Matching rates", result["count"]),
                    ("Lowest Square no.", result["lowest_square"] or "-"),
                    ("Highest Square no.", result["highest_square"] or "-"),
                    ("Lowest Kila no.", result["lowest_kila"] or "-"),
                    ("Highest Kila no.", result["highest_kila"] or "-"),
                    ("Lowest rate", result["lowest_rate"] if result["lowest_rate"] is not None else "-"),
                    ("Highest rate", result["highest_rate"] if result["highest_rate"] is not None else "-"),
                ]
                summary_table.rows = [ft.DataRow(cells=[
                    ft.DataCell(ft.Text(str(label))), ft.DataCell(ft.Text(str(value))),
                ]) for label, value in table_values]
                if not result["count"]:
                    summary_text.value = "No schedule records match this selection and the current rate filters."
                else:
                    summary_text.value = (
                        f"{result['count']} matching rate(s) | "
                        f"Square range: {result['lowest_square'] or '-'} to {result['highest_square'] or '-'} | "
                        f"Kila range: {result['lowest_kila'] or '-'} to {result['highest_kila'] or '-'} | "
                        f"Rate range: {result['lowest_rate']} to {result['highest_rate']}"
                    )
            except (TypeError, ValueError) as exc:
                summary_text.value = str(exc)
            except Exception as exc:
                summary_text.value = f"Could not summarize selection: {exc}"
            page.update()

        def save_khewat_for_year(year: str):
            try:
                if khewat_record:
                    result = update_khewat_record(
                        khewat_id=khewat_record["id"],
                        khewat_number=khewat_number.value or "",
                        rhz_year=year,
                        khewat_size=khewat_size.value,
                        square_numbers=sorted(selected_values["square"]),
                        kila_numbers=sorted(selected_values["kila"]),
                    )
                    assignment_status.value = (
                        f"Saved Khewat {result['khewat_number']} for RHZ {result['rhz_year']}; "
                        f"added {result['added']} and removed {result['removed']} reference(s)."
                    )
                else:
                    result = assign_cadastral_references_to_khewat(
                        context=selected_context(),
                        khewat_number=khewat_number.value or "",
                        rhz_year=year,
                        khewat_size=khewat_size.value,
                        square_numbers=sorted(selected_values["square"]),
                        kila_numbers=sorted(selected_values["kila"]),
                    )
                    assignment_status.value = (
                        f"Khewat {result['khewat_number']} for RHZ {result['rhz_year']}: "
                        f"added {result['added']} reference(s); {result['existing']} were already assigned."
                    )
                render_saved_khewats()
            except Exception as exc:
                assignment_status.value = f"Could not save Khewat assignment: {exc}"
            page.update()

        def open_existing_khewat(record):
            page.pop_dialog()  # Close the conflict prompt.
            page.pop_dialog()  # Close the new-entry dialog.
            open_cadastral_selection_dialog(khewat_record=record)

        def check_year_conflicts_and_save(year: str):
            if khewat_record:
                save_khewat_for_year(year)
                return
            conflicts = find_khewat_year_conflicts(context=selected_context(), rhz_year=year)
            if not conflicts:
                save_khewat_for_year(year)
                return
            same_number = next(
                (item for item in conflicts if item["khewat_number"] == (khewat_number.value or "").strip()),
                None,
            )
            if same_number:
                page.show_dialog(ft.AlertDialog(
                    modal=True,
                    title=ft.Text("Khewat already saved for this RHZ year"),
                    content=ft.Text(
                        f"Khewat {same_number['khewat_number']} already exists in this Mauza for {year}. "
                        "Open it to view or edit, or change the Khewat number to create another record."
                    ),
                    actions=[
                        ft.TextButton("Choose another number", on_click=lambda _: page.pop_dialog()),
                        ft.Button("View / edit existing", on_click=lambda _: open_existing_khewat(same_number)),
                    ],
                ))
                return

            page.show_dialog(ft.AlertDialog(
                modal=True,
                title=ft.Text("Khewats already exist for this RHZ year"),
                content=ft.Text(
                    f"This Mauza already has {len(conflicts)} Khewat(s) for {year}. "
                    "You can view or edit an existing Khewat, or continue creating another one."
                ),
                actions=[
                    ft.TextButton("Cancel", on_click=lambda _: page.pop_dialog()),
                    ft.OutlinedButton(
                        f"View {conflicts[0]['khewat_number']}",
                        on_click=lambda _: open_existing_khewat(conflicts[0]),
                    ),
                    ft.Button(
                        "Create another Khewat",
                        on_click=lambda _: (page.pop_dialog(), save_khewat_for_year(year)),
                    ),
                ],
            ))

        def save_khewat_assignment(_=None):
            raw_year = rhz_year.text or rhz_year.value or ""
            if not raw_year.strip():
                try:
                    latest_year = latest_rhz_year(selected_context())
                except Exception as exc:
                    assignment_status.value = f"Could not find the latest RHZ year: {exc}"
                    page.update()
                    return
                if latest_year is None:
                    assignment_status.value = (
                        "No RHZ year exists for this Mauza yet. Choose an RHZ year before saving."
                    )
                    page.update()
                    return
                rhz_year.value = latest_year
                page.show_dialog(ft.AlertDialog(
                    modal=True,
                    title=ft.Text("Use the latest saved RHZ year?"),
                    content=ft.Text(
                        f"No year was selected. The latest year for this Mauza is {latest_year}. "
                        "Continue with that year?"
                    ),
                    actions=[
                        ft.TextButton("Choose another year", on_click=lambda _: page.pop_dialog()),
                        ft.Button(
                            f"Continue with {latest_year}",
                            on_click=lambda _: (
                                page.pop_dialog(), check_year_conflicts_and_save(latest_year)
                            ),
                        ),
                    ],
                ))
                return
            try:
                year = normalize_rhz_year(raw_year)
                rhz_year.value = year
                check_year_conflicts_and_save(year)
            except Exception as exc:
                assignment_status.value = f"Could not save Khewat: {exc}"
                page.update()

        square_search.on_change = lambda _: render_reference_checks("square")
        kila_search.on_change = lambda _: render_reference_checks("kila")
        def location_field_changed(_=None):
            refresh_location_suggestions()
            try:
                selected_context()
            except DCRateServiceError:
                return
            load_mauza_references()

        for field in (mauza_field, district_field, tehsil_field):
            field.on_change = location_field_changed
            field.on_select = load_mauza_references
        load_mauza_references()

        reference_columns = ft.ResponsiveRow([
            ft.Container(content=ft.Column([
                square_search,
                square_checks,
            ], spacing=4), col={"xs": 12, "md": 6}),
            ft.Container(content=ft.Column([
                kila_search,
                kila_checks,
            ], spacing=4), col={"xs": 12, "md": 6}),
        ], run_spacing=6)
        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("Select squares and Kilas"),
            content=ft.Column([
                ft.Text("Choose one Mauza, search its saved values, then mark any squares and Kilas."),
                ft.ResponsiveRow([
                    ft.Container(ft.Column([ft.Text("Mauza"), mauza_field], spacing=2),
                                 col={"xs": 12, "md": 4}),
                    ft.Container(ft.Column([ft.Text("District"), district_field], spacing=2),
                                 col={"xs": 12, "md": 4}),
                    ft.Container(ft.Column([ft.Text("Tehsil"), tehsil_field], spacing=2),
                                 col={"xs": 12, "md": 4}),
                ], run_spacing=4),
                reference_columns,
                selection_count,
                ft.Text("Khewat summary", weight=ft.FontWeight.BOLD, size=13),
                ft.Row([
                    summary_sort_field, summary_sort_direction,
                    ft.OutlinedButton("Export PDF", on_click=export_khewat_handler("pdf")),
                    ft.OutlinedButton("Export Excel", on_click=export_khewat_handler("xlsx")),
                    ft.OutlinedButton("Export CSV", on_click=export_khewat_handler("csv")),
                    ft.OutlinedButton("Export ODS", on_click=export_khewat_handler("ods")),
                ], wrap=True, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Container(khewat_summary_table, expand=True, bgcolor="#FFFFFF",
                             border=ft.Border.all(1, "#E2E8F0"), border_radius=6, padding=6),
                ft.Row([
                    ft.OutlinedButton("Clear selection", on_click=clear_selection),
                    ft.Button("Show summary", on_click=show_selection_summary),
                    summary_table_button,
                ], wrap=True),
                summary_text,
                summary_table,
                ft.Divider(),
                ft.Text("Khewat record", weight=ft.FontWeight.BOLD, size=13),
                ft.Text("Select the Mauza together with its district and tehsil context.", size=11, color="#64748B"),
                ft.Row([khewat_number, rhz_year, khewat_size], wrap=True,
                       vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Row([
                    ft.OutlinedButton("Import Khewats (CSV / Excel / ODS)", on_click=import_khewat_file),
                    ft.TextButton("Download Excel template", on_click=save_khewat_template),
                ], wrap=True),
                ft.Button(
                    "Save Khewat changes" if khewat_record else "Add selected to Khewat",
                    on_click=save_khewat_assignment,
                ),
                assignment_status,
                ft.Text("Saved Khewats in this Mauza", weight=ft.FontWeight.BOLD, size=13),
                saved_khewats,
            ], width=880, height=650, spacing=8, scroll=ft.ScrollMode.AUTO),
            actions=[ft.TextButton("Close", on_click=lambda _: close_dialog())],
        ))
        page.update()

    def qanoongoi_changed(event):
        """Reset child location values and cadastral choices for the new Qanoongoi."""
        mauza_filter.value = ""
        square_filter.value = ""
        kila_filter.value = ""
        khasra_filter.value = ""
        schedule_rates_refresh(event, refresh_cadastral=True)

    # Hierarchy-aware type-ahead for the filter fields (developer request):
    # Qanoongoi offers every value without a Tehsil, but only the selected
    # tehsil's values once one is typed; Mauza follows the Qanoongoi; and
    # Square/Kila hints come from the selected Mauza. Backed by the same
    # preloaded hierarchy and per-context cache the Calculate tab uses, so
    # the lookups are instant.
    hint_generation = 0

    def filter_hint_refresh() -> None:
        """Apply hierarchy-scoped suggestions to the location filter fields."""
        nonlocal hint_generation
        hint_generation += 1
        generation = hint_generation

        def land_type_for_hints() -> str | None:
            value = schedule_filter.value or "all"
            if value == "dc_rural":
                return "rural"
            if value == "dc_urban":
                return "urban"
            return None

        def apply_hints() -> None:
            try:
                values_by_field = list_location_hint_values(
                    land_type=land_type_for_hints(),
                    context={
                        "district": district_filter.value or "",
                        "tehsil": tehsil_filter.value or "",
                        "qanoongoi": qanoongoi_filter.value or "",
                        "mauza": mauza_filter.value or "",
                    },
                    fields=[
                        "district", "tehsil", "qanoongoi", "mauza",
                    ],
                    limit=100,
                    hierarchy=True,
                )
            except Exception as exc:
                status_text.value = f"Could not load filter suggestions: {exc}"
                page.update()
                return
            for field_name, control in (
                ("district", district_filter), ("tehsil", tehsil_filter),
                ("qanoongoi", qanoongoi_filter), ("mauza", mauza_filter),
            ):
                control.suggestions = [
                    ft.AutoCompleteSuggestion(key=value, value=value)
                    for value in values_by_field.get(field_name, [])
                ]
            page.update()

        async def load_hints():
            await asyncio.sleep(0.12)
            if generation != hint_generation:
                return
            await asyncio.to_thread(apply_hints)

        if hasattr(page, "run_task"):
            page.run_task(load_hints)
        else:
            apply_hints()

    def district_changed(event):
        """Reset every rural level below the District."""
        tehsil_filter.value = ""
        qanoongoi_filter.value = ""
        mauza_filter.value = ""
        square_filter.value = ""
        kila_filter.value = ""
        khasra_filter.value = ""
        schedule_rates_refresh(event, refresh_cadastral=True)

    def tehsil_changed(event):
        """Reset Qanoongoi and everything below it for the new Tehsil."""
        qanoongoi_filter.value = ""
        mauza_filter.value = ""
        square_filter.value = ""
        kila_filter.value = ""
        khasra_filter.value = ""
        schedule_rates_refresh(event, refresh_cadastral=True)

    def mauza_changed(event):
        """Reset cadastral choices that belonged to the previous Mauza."""
        square_filter.value = ""
        kila_filter.value = ""
        khasra_filter.value = ""
        schedule_rates_refresh(event, refresh_cadastral=True)

    def square_changed(event):
        """Clear child cadastral fields and narrow Killa choices to the Square."""
        kila_filter.value = ""
        khasra_filter.value = ""
        schedule_rates_refresh(event, suggestion_field="square_no")

    def kila_changed(event):
        """Clear Khasra choices that belonged to the previous Kila."""
        khasra_filter.value = ""
        schedule_rates_refresh(event, suggestion_field="kila_no")

    search_field.on_change = schedule_rates_refresh
    district_filter.on_change = district_changed
    district_filter.on_select = district_changed
    tehsil_filter.on_change = tehsil_changed
    tehsil_filter.on_select = tehsil_changed
    qanoongoi_filter.on_change = qanoongoi_changed
    qanoongoi_filter.on_select = qanoongoi_changed
    mauza_filter.on_change = mauza_changed
    mauza_filter.on_select = mauza_changed
    square_filter.on_change = square_changed
    kila_filter.on_change = kila_changed
    khasra_filter.on_change = lambda event: schedule_rates_refresh(event, suggestion_field="khasra_no")
    schedule_filter.on_select = schedule_rates_refresh
    location_filter.on_select = schedule_rates_refresh
    unit_filter.on_select = schedule_rates_refresh
    published_year_filter.on_change = schedule_rates_refresh
    load_more_button.on_click = lambda _: schedule_rates_refresh(append=True)

    def open_khewat_search_dialog(_=None):
        """Search saved Khewats by Mauza, admin area, number, and RHZ year."""
        mauza_search = ft.AutoComplete(width=220, suggestions_max_height=240)
        district_search = ft.TextField(label="District", width=190)
        tehsil_search = ft.TextField(label="Tehsil", width=190)
        number_search = ft.AutoComplete(width=190, suggestions_max_height=240)
        year_search = ft.Dropdown(
            label="RHZ year",
            value="all",
            options=[ft.dropdown.Option("all", "All years")]
            + [ft.dropdown.Option(year, year) for year in list_rhz_years()],
            enable_filter=True,
            menu_height=320,
            width=190,
        )
        search_status = ft.Text("Enter any filters, then search saved Khewats.", size=12, color="#475569")
        results = ft.Column(height=360, spacing=6, scroll=ft.ScrollMode.AUTO)

        def run_search(_=None):
            results.controls.clear()
            try:
                records = search_khewats(
                    mauza=mauza_search.value or "",
                    district=district_search.value or "",
                    tehsil=tehsil_search.value or "",
                    khewat_number=number_search.value or "",
                    rhz_year=year_search.value if year_search.value != "all" else None,
                )
                search_status.value = f"Found {len(records)} saved Khewat(s)."
                mauza_search.suggestions = [
                    ft.AutoCompleteSuggestion(key=name, value=name)
                    for name in list_saved_khewat_mauzas(search=mauza_search.value or "")
                ]
                number_search.suggestions = [
                    ft.AutoCompleteSuggestion(key=value, value=value)
                    for value in list_saved_khewat_numbers(
                        mauza=mauza_search.value or "", search=number_search.value or ""
                    )
                ]
                if not records:
                    results.controls.append(ft.Text("No Khewats match these filters."))
                for record in records:
                    details = (
                        f"{context_label_for_record(record)} | Khewat {record['khewat_number']} "
                        f"| RHZ {record.get('rhz_year') or 'year unknown'} "
                        f"| Size {record.get('khewat_size') or '-'}\n"
                        f"Squares: {', '.join(record['squares']) or '-'} | "
                        f"Kilas: {', '.join(record['kilas']) or '-'}"
                    )
                    results.controls.append(ft.Container(
                        padding=10,
                        bgcolor="#F8FAFC",
                        border=ft.Border.all(1, "#E2E8F0"),
                        border_radius=8,
                        content=ft.Row([
                            ft.Text(details, selectable=True, expand=True, size=12),
                            ft.OutlinedButton(
                                "View / edit",
                                on_click=lambda _, saved=record: open_saved_khewat(saved),
                            ),
                        ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    ))
            except Exception as exc:
                search_status.value = f"Could not search Khewats: {exc}"
            page.update()

        def context_label_for_record(record):
            return " | ".join(
                str(record[field]) for field in ("mauza_name", "district", "tehsil")
                if record.get(field)
            )

        def open_saved_khewat(record):
            page.pop_dialog()
            open_cadastral_selection_dialog(khewat_record=record)

        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("Search saved Khewats"),
            content=ft.Column([
                ft.ResponsiveRow([
                    ft.Container(ft.Column([ft.Text("Mauza"), mauza_search], spacing=2),
                                 col={"xs": 12, "sm": 6, "md": 4}),
                    ft.Container(district_search, col={"xs": 12, "sm": 6, "md": 4}),
                    ft.Container(tehsil_search, col={"xs": 12, "sm": 6, "md": 4}),
                    ft.Container(ft.Column([ft.Text("Khewat number"), number_search], spacing=2),
                                 col={"xs": 12, "sm": 6, "md": 4}),
                    ft.Container(year_search, col={"xs": 12, "sm": 6, "md": 4}),
                ], run_spacing=4),
                ft.Row([
                    ft.Button("Search", on_click=run_search),
                    ft.TextButton("Clear", on_click=lambda _: clear_search_filters()),
                ], wrap=True),
                search_status,
                results,
            ], width=900, height=570, spacing=8, scroll=ft.ScrollMode.AUTO),
            actions=[ft.TextButton("Close", on_click=lambda _: close_dialog())],
        ))

        def update_khewat_suggestions(_=None):
            try:
                mauza_search.suggestions = [
                    ft.AutoCompleteSuggestion(key=name, value=name)
                    for name in list_saved_khewat_mauzas(search=mauza_search.value or "")
                ]
                number_search.suggestions = [
                    ft.AutoCompleteSuggestion(key=value, value=value)
                    for value in list_saved_khewat_numbers(
                        mauza=mauza_search.value or "", search=number_search.value or ""
                    )
                ]
            except Exception as exc:
                search_status.value = f"Could not load Khewat suggestions: {exc}"
            page.update()

        mauza_search.on_change = update_khewat_suggestions
        number_search.on_change = update_khewat_suggestions

        def clear_search_filters():
            mauza_search.value = ""
            district_search.value = ""
            tehsil_search.value = ""
            number_search.value = ""
            year_search.value = "all"
            run_search()

        run_search()
        page.update()

    def open_hierarchy_summary_dialog(_=None):
        """Show district, Tehsil, Qanoongoi, and Mauza counts from the database."""
        try:
            rows = summarize_location_hierarchy()
        except Exception as exc:
            status_text.value = f"Could not load Tehsil details: {exc}"
            page.update()
            return
        if not rows:
            page.show_dialog(ft.AlertDialog(
                modal=True,
                title=ft.Text("Tehsil details"),
                content=ft.Text("No location records are available to summarize."),
                actions=[ft.TextButton("Close", on_click=lambda _: close_dialog())],
            ))
            return
        district_count = len({row["district"] for row in rows})
        tehsil_count = len({
            (row["district"], row["tehsil"]) for row in rows if row["tehsil"]
        })
        qanoongoi_count = len({
            (row["district"], row["tehsil"], row["qanoongoi"])
            for row in rows if row["qanoongoi"]
        })
        district_totals: dict[str, dict[str, set[str]]] = {}
        for row in rows:
            totals = district_totals.setdefault(
                row["district"], {"tehsils": set(), "qanoongoi": set()}
            )
            if row["tehsil"]:
                totals["tehsils"].add(row["tehsil"])
            if row["qanoongoi"]:
                totals["qanoongoi"].add((row["tehsil"], row["qanoongoi"]))
        district_table = ft.DataTable(
            columns=[
                ft.DataColumn(label=ft.Text("District")),
                ft.DataColumn(label=ft.Text("Tehsils in district")),
                ft.DataColumn(label=ft.Text("Qanoongoi in district")),
            ],
            rows=[
                ft.DataRow(cells=[
                    ft.DataCell(ft.Text(district or "Not recorded")),
                    ft.DataCell(ft.Text(str(len(totals["tehsils"])))),
                    ft.DataCell(ft.Text(str(len(totals["qanoongoi"])))),
                ])
                for district, totals in sorted(district_totals.items())
            ],
            column_spacing=18,
            horizontal_margin=8,
            data="district_summary",
        )
        table = ft.DataTable(
            columns=[
                ft.DataColumn(label=ft.Text("District")),
                ft.DataColumn(label=ft.Text("Tehsil")),
                ft.DataColumn(label=ft.Text("Qanoongoi")),
                ft.DataColumn(label=ft.Text("Mauzas in Qanoongoi")),
            ],
            rows=[
                ft.DataRow(cells=[
                    ft.DataCell(ft.Text(row["district"] or "Not recorded")),
                    ft.DataCell(ft.Text(row["tehsil"] or "Not recorded")),
                    ft.DataCell(ft.Text(row["qanoongoi"] or "Not recorded")),
                    ft.DataCell(ft.Text(str(row["mauza_count"]))),
                ])
                for row in rows
            ],
            column_spacing=18,
            horizontal_margin=8,
            data="tehsil_details",
        )
        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("Tehsil details"),
            content=ft.Column(
                controls=[
                    ft.Text(
                        f"{district_count} district(s) · {tehsil_count} tehsil(s) · "
                        f"{qanoongoi_count} qanoongoi(s)",
                        size=12,
                        color="#475569",
                    ),
                    ft.Text("Districts", weight=ft.FontWeight.BOLD, size=13),
                    ft.Row([district_table], scroll=ft.ScrollMode.AUTO),
                    ft.Text("Tehsil / Qanoongoi details", weight=ft.FontWeight.BOLD, size=13),
                    ft.Row([table], scroll=ft.ScrollMode.AUTO),
                ],
                width=940,
                height=520,
                spacing=8,
                scroll=ft.ScrollMode.AUTO,
            ),
            actions=[ft.TextButton("Close", on_click=lambda _: close_dialog())],
        ))
        page.update()

    def storage_preference_changed(_=None):
        mode = storage_preference.value or "ranges"
        try:
            set_cadastral_storage_mode(mode)
            status_text.value = (
                "New cadastral records will be stored as compact ranges."
                if mode == "ranges"
                else "New cadastral records will be stored as individual values."
            )
        except Exception as exc:
            status_text.value = f"Could not save the cadastral storage preference: {exc}"
        page.update()

    storage_preference = ft.Dropdown(
        value=get_cadastral_storage_mode(),
        options=[
            ft.dropdown.Option("ranges", "Keep Square/Kila/Khasra as ranges"),
            ft.dropdown.Option("individual", "Store each individual value"),
        ],
        width=320,
        on_select=storage_preference_changed,
    )
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

    def open_delete_schedules_dialog(_=None):
        """Bulk-delete schedule rows after explicit confirmation."""
        scope_choice = ft.RadioGroup(
            value="fbr",
            content=ft.Column([
                ft.Radio(value="fbr", label="All FBR schedule rows"),
                ft.Radio(value="dc_rural", label="All rural DC schedule rows"),
                ft.Radio(value="dc_urban", label="All urban DC schedule rows"),
                ft.Radio(value="all", label="Everything (all DC and FBR rows)"),
            ], spacing=6),
        )
        delete_status = ft.Text("", color="#B91C1C", size=12)

        def confirm_delete(_=None):
            scope = scope_choice.value or "all"
            try:
                deleted = delete_dc_rate_schedules(scope)
            except Exception as exc:
                delete_status.value = f"Could not delete: {exc}"
                page.update()
                return
            page.pop_dialog()
            schedule_rates_refresh()
            status_text.value = (
                f"Deleted {deleted:,} schedule row(s) ({DELETION_SCOPES[scope]}). "
                "Saved estimates keep their own snapshots."
            )
            page.update()

        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("Delete schedules"),
            content=ft.Column([
                ft.Text(
                    "This permanently deletes schedule rows in the chosen scope. "
                    "Location rows are kept, and saved estimates keep their own "
                    "charge snapshots. This cannot be undone.",
                    color="#B91C1C", size=12,
                ),
                scope_choice, delete_status,
            ], tight=True, spacing=8, width=460),
            actions=[
                ft.TextButton("Cancel", on_click=lambda _: page.pop_dialog()),
                ft.Button(
                    "Delete permanently", on_click=confirm_delete,
                    style=ft.ButtonStyle(bgcolor="#B91C1C", color="#FFFFFF"),
                ),
            ],
        ))

    toolbar = ft.Row(
        controls=[
            ft.Button("Add DC rate", on_click=open_rate_dialog),
            ft.OutlinedButton("Add FBR rate", on_click=lambda _: open_rate_dialog(schedule_type="fbr")),
            ft.OutlinedButton("Add location", on_click=open_location_dialog),
            ft.OutlinedButton("Find / remove duplicates", on_click=open_duplicate_cleanup_dialog),
            ft.OutlinedButton("Duplicate rules", on_click=open_duplicate_rules_dialog),
            ft.OutlinedButton("Select squares / Kilas", on_click=open_cadastral_selection_dialog),
            ft.OutlinedButton("View Khewats", on_click=open_khewat_search_dialog),
            ft.OutlinedButton("Tehsil Details", on_click=open_hierarchy_summary_dialog),
            ft.OutlinedButton("Delete schedules…", on_click=open_delete_schedules_dialog),
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
            ft.Row(
                controls=[
                    ft.Text("New cadastral records:", size=12, color="#475569"),
                    storage_preference,
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
        ft.Container(ft.Column([ft.Text("Qanoongoi"), qanoongoi_filter], spacing=2), col={"xs": 12, "sm": 6, "md": 3}),
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
        ft.Container(ft.Column([ft.Text("District"), district_filter], spacing=2), col={"xs": 6, "sm": 3, "md": 2}),
        ft.Container(ft.Column([ft.Text("Tehsil"), tehsil_filter], spacing=2), col={"xs": 6, "sm": 3, "md": 2}),
        ft.Container(location_filter, col={"xs": 12, "sm": 6, "md": 4}),
        ft.Container(ft.Column([ft.Text("Town"), town_filter], spacing=2), col={"xs": 6, "sm": 3, "md": 2}),
        ft.Container(ft.Column([ft.Text("Property area"), property_area_filter], spacing=2), col={"xs": 6, "sm": 3, "md": 2}),
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
    page_root = ft.Stack(controls=[root, busy_overlay], expand=True)
    if defer_initial_load:
        async def load_initial_data():
            """Populate the first bounded view without blocking page navigation."""
            try:
                location_picker_data, records, mauza_names, summary = await asyncio.gather(
                    asyncio.to_thread(list_location_picker, limit=visible_location_limit),
                    asyncio.to_thread(list_dc_rates, limit=visible_rate_limit + 1),
                    asyncio.to_thread(
                        list_mauza_names,
                        search=mauza_filter.value or "",
                        qanoongoi=qanoongoi_filter.value or "",
                        limit=500,
                    ),
                    asyncio.to_thread(
                        summarize_dc_rates,
                        qanoongoi=qanoongoi_filter.value or "",
                    ),
                )
                # Preload the tiny rural hierarchy once so the district/
                # tehsil/qanoongoi/mauza filter suggestions answer instantly,
                # then fill them from the hierarchy (developer request).
                await asyncio.to_thread(preload_location_dimensions)
                filter_hint_refresh()
                refresh_locations(
                    location_picker_data["locations"],
                    qanoongoi_values=location_picker_data["qanoongoi"],
                    has_more=location_picker_data["has_more"],
                )
                mauza_filter.suggestions = [
                    ft.AutoCompleteSuggestion(key=name, value=name) for name in mauza_names
                ]
                # Square/Kila/Khasra suggestions stay empty until a Mauza is
                # provided (developer request: no more "wild" global lists);
                # the cadastral refresh scopes them on each change.
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
    return page_root
