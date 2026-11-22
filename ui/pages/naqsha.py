"""Naqsha Alif / Bay / Jeem page — step 1 (developer objective).

Import Land Details (Dataset A) and Owner Details (Dataset B) by pasting
Punjab Zameen portal tables or reading spreadsheet files, map the columns
(auto-detected with manual override), check every khewat (totals, owner
vs. land checkage, corrected shares), and save the records under the
District → Tehsil → Qanoongoi → Mauza → Khewat hierarchy. Saved khewats
can be viewed, deleted, and are imported into the Valuation & Taxes page
from that page itself (developer request 4).
"""

from __future__ import annotations

import asyncio
from typing import Any

import flet as ft

from services.naqsha_service import (
    LAND_FIELDS,
    LAND_FIELD_LABELS,
    OWNER_FIELDS,
    OWNER_FIELD_LABELS,
    NaqshaError,
    build_land_rows,
    build_naqsha_preview,
    build_owner_rows,
    delete_naqsha_record,
    list_naqsha_context_suggestions,
    list_naqsha_records,
    parse_pasted_table,
    save_naqsha_records,
    suggest_column_mapping,
    summarize_naqsha_record,
)
from services.settings_service import get_default_file_location
from ui.file_location import ask_location_choice


def _collapsible_card(
    page: ft.Page,
    title: str,
    body_controls: list[ft.Control],
    *,
    visible: bool = True,
) -> ft.Container:
    """Card with a click-to-collapse header (same pattern as the estimate tab)."""
    body = ft.Column(body_controls, spacing=10)
    toggle_icon = ft.IconButton(icon=ft.Icons.EXPAND_LESS, tooltip="Collapse")

    def toggle(_=None):
        body.visible = not body.visible
        toggle_icon.icon = (
            ft.Icons.EXPAND_MORE if not body.visible else ft.Icons.EXPAND_LESS
        )
        page.update()

    toggle_icon.on_click = toggle
    header = ft.Row(
        [ft.Text(title, size=15, weight=ft.FontWeight.BOLD, color="#1E293B",
                 expand=True), toggle_icon],
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )
    return ft.Container(
        ft.Column([header, body], spacing=8),
        padding=14,
        bgcolor="#FFFFFF",
        border=ft.Border.all(1, "#E2E8F0"),
        border_radius=10,
        visible=visible,
    )


def _data_table(headers: list[str], rows: list[list[str]]) -> ft.Control:
    """Compact read-only table; falls back to plain text when empty."""
    if not rows:
        return ft.Text("No rows.", size=12, color="#64748B")
    return ft.DataTable(
        columns=[ft.DataColumn(ft.Text(header, size=11,
                                       weight=ft.FontWeight.BOLD))
                 for header in headers],
        rows=[
            ft.DataRow(cells=[
                ft.DataCell(ft.Container(
                    ft.Text(str(value), size=11, max_lines=2,
                            overflow=ft.TextOverflow.ELLIPSIS),
                    width=110,
                ))
                for value in row
            ])
            for row in rows
        ],
        column_spacing=12,
        heading_row_height=30,
        data_row_height=34,
    )


def build_naqsha_page(page: ft.Page) -> ft.Control:
    """Build the Naqsha Alif / Bay / Jeem workspace."""
    file_picker = next(
        (service for service in page.services if isinstance(service, ft.FilePicker)),
        None,
    )
    if file_picker is None:
        file_picker = ft.FilePicker()
        page.services.append(file_picker)

    status = ft.Text("", size=12, color="#475569")

    # Loaded datasets: {"headers", "rows", "mapping", "file"} per dataset.
    datasets: dict[str, dict[str, Any]] = {"land": {}, "owner": {}}
    preview_groups: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Dataset loading (paste or file) + column mapping
    # ------------------------------------------------------------------
    land_paste = ft.TextField(
        label="Paste Land Details here (copy the table from the portal)",
        multiline=True, min_lines=3, max_lines=6, expand=True,
    )
    owner_paste = ft.TextField(
        label="Paste Owner Details here (copy the table from the portal)",
        multiline=True, min_lines=3, max_lines=6, expand=True,
    )
    land_file_text = ft.Text("", size=11, color="#64748B")
    owner_file_text = ft.Text("", size=11, color="#64748B")
    land_mapping_row = ft.Row(wrap=True, spacing=10)
    owner_mapping_row = ft.Row(wrap=True, spacing=10)
    land_load_status = ft.Text("", size=11, color="#64748B")
    owner_load_status = ft.Text("", size=11, color="#64748B")

    async def _pick_file_bytes(dialog_title: str) -> tuple[bytes, str] | None:
        default_location = ""
        try:
            default_location = get_default_file_location()
        except Exception:
            default_location = ""
        choice = await ask_location_choice(
            page, action="open", default_location=default_location,
        )
        if choice is None:
            return None
        result = await file_picker.pick_files(
            dialog_title=dialog_title,
            initial_directory=(
                default_location if choice == "default" else None
            ),
            allowed_extensions=["xlsx", "xls", "ods", "csv"],
            allow_multiple=False,
            with_data=True,
        )
        # flet 1.0.4 returns the chosen files as a plain list here; older
        # shapes returned a result object with a ``files`` attribute.
        chosen_files = getattr(result, "files", result)
        if not chosen_files:
            return None
        chosen = chosen_files[0]
        return chosen.bytes, chosen.name

    def _load_dataset(dataset: str, text: str, file_bytes: bytes | None,
                      filename: str) -> None:
        from services.dc_rate_service import read_schedule_sheets
        if file_bytes:
            sheets = read_schedule_sheets(file_bytes, filename)
            if not sheets:
                raise NaqshaError("The selected file has no readable sheet.")
            headers = [str(header) for header in sheets[0]["headers"]]
            rows = [[str(cell) if cell is not None else "" for cell in row]
                    for row in sheets[0]["rows"]]
        else:
            headers, rows = parse_pasted_table(text)
        mapping = suggest_column_mapping(headers, dataset)
        datasets[dataset] = {
            "headers": headers, "rows": rows, "mapping": mapping,
            "file": filename if file_bytes else "",
        }

    def _render_mapping(dataset: str) -> ft.Control:
        state = datasets[dataset]
        fields = LAND_FIELDS if dataset == "land" else OWNER_FIELDS
        labels = LAND_FIELD_LABELS if dataset == "land" else OWNER_FIELD_LABELS
        controls: list[ft.Control] = []
        for field in fields:
            options = [ft.dropdown.Option(key="-1", text="(not mapped)")]
            for index, header in enumerate(state["headers"]):
                options.append(ft.dropdown.Option(
                    key=str(index), text=f"{index + 1}. {header}"[:60],
                ))
            dropdown = ft.Dropdown(
                label=labels[field], width=280, options=options,
                value=str(state["mapping"].get(field))
                if state["mapping"].get(field) is not None else "-1",
            )

            def on_map_change(_e, field=field, dataset=dataset,
                              dropdown=dropdown):
                value = int(dropdown.value)
                datasets[dataset]["mapping"][field] = (
                    value if value >= 0 else None
                )

            dropdown.on_change = on_map_change
            controls.append(dropdown)
        return ft.Row(controls, wrap=True, spacing=10)

    def load_land_data(_=None):
        try:
            _load_dataset("land", land_paste.value or "", None, "")
        except NaqshaError as exc:
            land_load_status.value = str(exc)
            land_load_status.color = "#B91C1C"
            page.update()
            return
        except Exception as exc:
            land_load_status.value = f"Could not read the land data: {exc}"
            land_load_status.color = "#B91C1C"
            page.update()
            return
        state = datasets["land"]
        land_file_text.value = (
            f"Land file loaded: {state['file']} ({len(state['rows'])} rows)"
            if state["file"] else ""
        )
        land_load_status.value = (
            f"Land data loaded: {len(state['rows'])} row(s), "
            f"{len(state['headers'])} column(s). Check the mapping below."
        )
        land_load_status.color = "#166534"
        land_mapping_row.controls = [_render_mapping("land")]
        page.update()

    def load_land_file(_=None):
        async def run():
            picked = await _pick_file_bytes("Open the Land Details file")
            if not picked:
                return
            file_bytes, filename = picked
            try:
                _load_dataset("land", "", file_bytes, filename)
            except NaqshaError as exc:
                land_load_status.value = str(exc)
                land_load_status.color = "#B91C1C"
            except Exception as exc:
                land_load_status.value = f"Could not read the land file: {exc}"
                land_load_status.color = "#B91C1C"
            else:
                state = datasets["land"]
                land_file_text.value = (
                    f"Land file loaded: {filename} ({len(state['rows'])} rows)"
                )
                land_load_status.value = (
                    f"Land data loaded: {len(state['rows'])} row(s). "
                    "Check the mapping below."
                )
                land_load_status.color = "#166534"
                land_mapping_row.controls = [_render_mapping("land")]
            page.update()

        if hasattr(page, "run_task"):
            page.run_task(run)
        else:
            page.update()

    def load_owner_data(_=None):
        try:
            _load_dataset("owner", owner_paste.value or "", None, "")
        except NaqshaError as exc:
            owner_load_status.value = str(exc)
            owner_load_status.color = "#B91C1C"
            page.update()
            return
        except Exception as exc:
            owner_load_status.value = f"Could not read the owner data: {exc}"
            owner_load_status.color = "#B91C1C"
            page.update()
            return
        state = datasets["owner"]
        owner_file_text.value = (
            f"Owner file loaded: {state['file']} ({len(state['rows'])} rows)"
            if state["file"] else ""
        )
        owner_load_status.value = (
            f"Owner data loaded: {len(state['rows'])} row(s). "
            "Check the mapping below."
        )
        owner_load_status.color = "#166534"
        owner_mapping_row.controls = [_render_mapping("owner")]
        page.update()

    def load_owner_file(_=None):
        async def run():
            picked = await _pick_file_bytes("Open the Owner Details file")
            if not picked:
                return
            file_bytes, filename = picked
            try:
                _load_dataset("owner", "", file_bytes, filename)
            except NaqshaError as exc:
                owner_load_status.value = str(exc)
                owner_load_status.color = "#B91C1C"
            except Exception as exc:
                owner_load_status.value = f"Could not read the owner file: {exc}"
                owner_load_status.color = "#B91C1C"
            else:
                state = datasets["owner"]
                owner_file_text.value = (
                    f"Owner file loaded: {filename} ({len(state['rows'])} rows)"
                )
                owner_load_status.value = (
                    f"Owner data loaded: {len(state['rows'])} row(s). "
                    "Check the mapping below."
                )
                owner_load_status.color = "#166534"
                owner_mapping_row.controls = [_render_mapping("owner")]
            page.update()

        if hasattr(page, "run_task"):
            page.run_task(run)
        else:
            page.update()

    # ------------------------------------------------------------------
    # Preview: per-khewat totals, checkage, corrected shares
    # ------------------------------------------------------------------
    unmatched_khewat_field = ft.TextField(
        label="Unmatched owners belong to Khewat No. (optional)", width=280,
    )
    preview_column = ft.Column(spacing=10)

    def build_preview(_=None):
        nonlocal preview_groups
        if not datasets["land"]:
            status.value = "Load the Land Details (Dataset A) first."
            page.update()
            return
        try:
            land_rows = build_land_rows(
                datasets["land"]["headers"], datasets["land"]["rows"],
                datasets["land"]["mapping"],
            )
            owner_rows: list[dict[str, Any]] = []
            if datasets["owner"]:
                owner_rows = build_owner_rows(
                    datasets["owner"]["headers"], datasets["owner"]["rows"],
                    datasets["owner"]["mapping"],
                )
            preview_groups = build_naqsha_preview(
                land_rows, owner_rows,
                unmatched_owner_khewat=unmatched_khewat_field.value or "",
            )
        except NaqshaError as exc:
            status.value = str(exc)
            page.update()
            return
        except Exception as exc:
            status.value = f"Preview failed: {exc}"
            page.update()
            return
        preview_column.controls = [_render_group(group) for group in preview_groups]
        save_section.visible = True
        status.value = (
            f"Preview ready: {len(preview_groups)} khewat(s). Review the "
            "checkage below, then save under a location."
        )
        page.update()

    def _render_group(group: dict[str, Any]) -> ft.Control:
        checkage_color = {
            "valid": "#166534", "mismatch": "#B91C1C",
        }.get(group["checkage_status"], "#B45309")
        checkage_text = (
            f"Checkage: {group['checkage_ratio']} ({group['checkage_status']})"
            if group["checkage_ratio"]
            else f"Checkage: {group['checkage_status']}"
        )
        land_table = _data_table(
            ["Khewat", "Khatoni", "Square", "Killa", "Qism Arazi", "Area"],
            [[row["khewat_no"], row["khatoni_no"], row["square_no"],
              row["killa_no"], row["land_type"], row["area_text"]]
             for row in group["land_rows"]],
        )
        owner_table = _data_table(
            ["Owner", "Relation", "Parent", "Qaum", "Hissa", "Raqba",
             "Corrected share"],
            [[row["owner_name"] or row["owner_name_raw"], row["relation"],
              row["parent_name"], row["caste"], row["share_text"],
              row["area_text"], row["share_text_corrected"] or "—"]
             for row in group["owner_rows"]],
        ) if group["owner_rows"] else ft.Text(
            "No owner rows were loaded for this khewat.", size=12,
            color="#64748B",
        )
        return ft.Container(
            ft.Column([
                ft.Text(
                    f"Khewat {group['khewat_no']}", size=13,
                    weight=ft.FontWeight.BOLD, color="#1E293B",
                ),
                ft.Text(
                    f"Land rows: {len(group['land_rows'])} · "
                    f"Total land area: {group['land_total_text']} marla · "
                    f"Owners: {len(group['owner_rows'])} · "
                    f"Total owner area: {group['owner_total_text']} marla",
                    size=11, color="#64748B",
                ),
                ft.Text(checkage_text, size=12, weight=ft.FontWeight.BOLD,
                        color=checkage_color),
                *([
                    ft.Text(
                        "Land total and owner total differ — check which "
                        "dataset row is wrong before saving.", size=11,
                        color="#B91C1C",
                    )
                ] if group["checkage_status"] == "mismatch" else []),
                *([ft.Column(
                    [ft.Text(problem, size=11, color="#B91C1C")
                     for problem in group["problems"]], spacing=2,
                )] if group["problems"] else []),
                land_table,
                ft.Text("Owners (Hissa corrected from the areas)",
                        size=12, weight=ft.FontWeight.W_500),
                owner_table,
            ], spacing=6),
            padding=10,
            bgcolor="#F8FAFC",
            border=ft.Border.all(1, "#E2E8F0"),
            border_radius=8,
        )

    # ------------------------------------------------------------------
    # Save under the hierarchy
    # ------------------------------------------------------------------
    province_field = ft.TextField(label="Province (optional)", width=190)
    district_field = ft.TextField(label="District", width=190)
    tehsil_field = ft.TextField(label="Tehsil", width=190)
    qanoongoi_field = ft.TextField(label="Qanoongoi", width=190)
    mauza_field = ft.TextField(label="Mauza", width=190)
    source_note_field = ft.TextField(
        label="Source note (optional)", width=280,
        hint_text="e.g. Punjab Zameen portal, 15-11-2026",
    )

    def refresh_context_suggestions() -> None:
        async def load():
            try:
                suggestions = await asyncio.to_thread(
                    list_naqsha_context_suggestions
                )
            except Exception:
                suggestions = {}
            for field, key in (
                (district_field, "district"), (tehsil_field, "tehsil"),
                (qanoongoi_field, "qanoongoi"), (mauza_field, "mauza"),
                (province_field, "province"),
            ):
                field.suggestions = [
                    ft.AutoCompleteSuggestion(key=value, value=value)
                    for value in suggestions.get(key, [])
                ]
            page.update()

        if hasattr(page, "run_task"):
            page.run_task(load)

    def save_records(_=None):
        try:
            result = save_naqsha_records(
                {
                    "province": province_field.value,
                    "district": district_field.value,
                    "tehsil": tehsil_field.value,
                    "qanoongoi": qanoongoi_field.value,
                    "mauza": mauza_field.value,
                    "source_note": source_note_field.value,
                },
                preview_groups,
            )
        except NaqshaError as exc:
            status.value = str(exc)
            page.update()
            return
        except Exception as exc:
            status.value = f"Saving failed: {exc}"
            page.update()
            return
        status.value = (
            f"Saved: {result['records']} new khewat record(s), "
            f"{result['land_rows_added']} land row(s), "
            f"{result['owner_rows_added']} owner row(s) added, "
            f"{result['skipped']} duplicate row(s) skipped."
        )
        refresh_context_suggestions()
        refresh_saved_records()

    # ------------------------------------------------------------------
    # Saved records: search, view, delete
    # ------------------------------------------------------------------
    search_khewat = ft.TextField(label="Khewat No.", width=140)
    search_mauza = ft.TextField(label="Mauza", width=160)
    search_district = ft.TextField(label="District", width=150)
    search_tehsil = ft.TextField(label="Tehsil", width=150)
    search_qanoongoi = ft.TextField(label="Qanoongoi", width=150)
    saved_table = ft.Column(spacing=8)

    def clear_saved_search(_=None):
        for field in (search_khewat, search_mauza, search_district,
                      search_tehsil, search_qanoongoi):
            field.value = ""
        refresh_saved_records()

    def refresh_saved_records(_=None):
        async def load():
            try:
                records = await asyncio.to_thread(
                    list_naqsha_records,
                    khewat_no=search_khewat.value or "",
                    mauza=search_mauza.value or "",
                    district=search_district.value or "",
                    tehsil=search_tehsil.value or "",
                    qanoongoi=search_qanoongoi.value or "",
                )
            except Exception as exc:
                status.value = f"Could not load the saved khewats: {exc}"
                page.update()
                return
            saved_table.controls = [
                _render_saved_record(record) for record in records
            ] or [ft.Text(
                "No saved khewat matches. Import records above or clear "
                "the search.", size=12, color="#64748B",
            )]
            page.update()

        if hasattr(page, "run_task"):
            page.run_task(load)

    def _render_saved_record(record: dict[str, Any]) -> ft.Control:
        summary = summarize_naqsha_record(record)
        return ft.Container(
            ft.Column([
                ft.Row([
                    ft.Text(
                        f"{record['mauza']} · Khewat {record['khewat_no']}"
                        + (f" · Khatoni {record['khatoni_no']}"
                           if record["khatoni_no"] else ""),
                        size=13, weight=ft.FontWeight.BOLD, color="#1E293B",
                        expand=True,
                    ),
                    ft.TextButton("View",
                                  on_click=lambda _, r=record: view_record(r)),
                    ft.TextButton(
                        "Delete",
                        on_click=lambda _, r=record: confirm_delete_record(r),
                    ),
                ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Text(
                    f"{record['district']} · {record['tehsil']} · "
                    f"{record['qanoongoi']} · Land rows: "
                    f"{len(record['land_rows'])} · Owners: "
                    f"{len(record['owner_rows'])} · Checkage: "
                    f"{summary['checkage_ratio'] or summary['checkage_status']}",
                    size=11, color="#64748B",
                ),
            ], spacing=2),
            padding=10,
            bgcolor="#FFFFFF",
            border=ft.Border.all(1, "#E2E8F0"),
            border_radius=8,
        )

    def view_record(record: dict[str, Any]):
        summary = summarize_naqsha_record(record)
        checkage_color = {
            "valid": "#166534", "mismatch": "#B91C1C",
        }.get(summary["checkage_status"], "#B45309")
        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text(
                f"Khewat {record['khewat_no']} — {record['mauza']}",
            ),
            content=ft.Column([
                ft.Text(
                    f"{record['district']} · {record['tehsil']} · "
                    f"{record['qanoongoi']}"
                    + (f" · {record['province']}" if record["province"] else "")
                    + (f" · Source: {record['source_note']}"
                       if record["source_note"] else ""),
                    size=11, color="#64748B",
                ),
                ft.Text(
                    f"Total land area: {summary['land_total_text']} marla · "
                    f"Total owner area: {summary['owner_total_text']} marla",
                    size=12,
                ),
                ft.Text(
                    f"Checkage: {summary['checkage_ratio'] or summary['checkage_status']}",
                    size=12, weight=ft.FontWeight.BOLD, color=checkage_color,
                ),
                _data_table(
                    ["Khewat", "Khatoni", "Square", "Killa", "Qism", "Area"],
                    [[row["khewat_no"], row["khatoni_no"], row["square_no"],
                      row["killa_no"], row["land_type"], row["area_text"]]
                     for row in record["land_rows"]],
                ),
                _data_table(
                    ["Owner", "Relation", "Parent", "Qaum", "Hissa",
                     "Raqba", "Corrected share"],
                    [[row["owner_name_raw"] or row["owner_name"],
                      row["relation"], row["parent_name"], row["caste"],
                      row["share_text"], row["area_text"],
                      row["share_text_corrected"] or "—"]
                     for row in record["owner_rows"]],
                ),
            ], spacing=8, scroll=ft.ScrollMode.AUTO),
            actions=[ft.TextButton("Close",
                                   on_click=lambda _: page.pop_dialog())],
        ))

    def confirm_delete_record(record: dict[str, Any]):
        def do_delete(_=None):
            try:
                deleted = delete_naqsha_record(record["id"])
            except Exception as exc:
                page.pop_dialog()
                status.value = f"Could not delete: {exc}"
                page.update()
                return
            page.pop_dialog()
            status.value = (
                f"Khewat {record['khewat_no']} in {record['mauza']} deleted."
                if deleted else "This khewat record was already gone."
            )
            refresh_saved_records()

        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("Delete saved khewat?"),
            content=ft.Text(
                f"Delete Khewat {record['khewat_no']} in {record['mauza']} "
                "with all of its land and owner rows? This cannot be "
                "undone.", size=13,
            ),
            actions=[
                ft.TextButton("Cancel", on_click=lambda _: page.pop_dialog()),
                ft.TextButton("Delete", on_click=do_delete),
            ],
        ))

    # ------------------------------------------------------------------
    # Layout (save_section is built here, after `save_records` exists,
    # so the button can reference the handler)
    # ------------------------------------------------------------------
    import_section = _collapsible_card(page, "1. Import Naqsha data", [
        ft.Text(
            "Dataset A (Land Details) and Dataset B (Owner Details) can be "
            "pasted straight from the Punjab Zameen portal or read from "
            ".xlsx / .xls / .ods / .csv files. Columns are detected "
            "automatically; correct any wrong assignment below. Extra "
            "portal columns are ignored.",
            size=12, color="#64748B",
        ),
        ft.Text("Dataset A — Land Details", size=13,
                weight=ft.FontWeight.W_600),
        land_paste,
        ft.Row([
            ft.OutlinedButton("Load pasted land data", on_click=load_land_data),
            ft.OutlinedButton("Load land file…", on_click=load_land_file),
            land_file_text,
        ], wrap=True, spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        land_load_status,
        ft.Container(land_mapping_row),
        ft.Divider(height=8),
        ft.Text("Dataset B — Owner Details", size=13,
                weight=ft.FontWeight.W_600),
        owner_paste,
        ft.Row([
            ft.OutlinedButton("Load pasted owner data", on_click=load_owner_data),
            ft.OutlinedButton("Load owner file…", on_click=load_owner_file),
            owner_file_text,
        ], wrap=True, spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        owner_load_status,
        ft.Container(owner_mapping_row),
        unmatched_khewat_field,
        ft.Row([ft.FilledButton("Build preview & checkage",
                                on_click=build_preview)], spacing=8),
    ])
    preview_section = _collapsible_card(page, "2. Preview, totals & checkage", [
        preview_column,
    ])
    save_section = _collapsible_card(page, "Save location", [
        ft.Text(
            "The saved khewats are grouped District → Tehsil → Qanoongoi → "
            "Mauza → Khewat. The same khewat number may exist in several "
            "mauzas; re-saving a khewat skips rows that are already stored.",
            size=12, color="#64748B",
        ),
        ft.Row([province_field, district_field, tehsil_field, qanoongoi_field,
                mauza_field, source_note_field], wrap=True, spacing=8),
        ft.Row([
            ft.FilledButton("Save khewat records", on_click=save_records),
        ], spacing=8),
    ], visible=False)
    saved_section = _collapsible_card(page, "3. Saved khewat records", [
        ft.Text(
            "Saved records can be imported into the Valuation & Taxes page "
            "from its Calculate tab ('Load from Naqsha') and from the "
            "DC / FBR rates khewat option ('Import from Naqsha').",
            size=12, color="#64748B",
        ),
        ft.Row([search_khewat, search_mauza, search_district, search_tehsil,
                search_qanoongoi,
                ft.OutlinedButton("Search", on_click=refresh_saved_records),
                ft.TextButton("Show all", on_click=clear_saved_search)],
               wrap=True, spacing=8,
               vertical_alignment=ft.CrossAxisAlignment.CENTER),
        saved_table,
    ])

    refresh_context_suggestions()
    refresh_saved_records()

    return ft.Container(
        ft.Column([
            ft.Text("Naqsha Alif / Bay / Jeem", size=22,
                    weight=ft.FontWeight.BOLD),
            ft.Text(
                "Import land and owner records (Punjab Zameen portal or "
                "spreadsheets), check every khewat, and save it for reuse "
                "in Valuation & Taxes. Step 1: data import, mapping, "
                "validation, and storage.",
                size=12, color="#64748B",
            ),
            status,
            import_section,
            preview_section,
            save_section,
            saved_section,
        ], spacing=12, scroll=ft.ScrollMode.AUTO, expand=True),
        padding=ft.Padding(16, 28, 16, 28),
        expand=True,
    )
