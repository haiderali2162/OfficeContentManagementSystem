"""Valuation workflow, saved-estimate history, and tax-rule management UI.

The calculator calls valuation/tax services; those services query persisted
schedule/rule models. Saving stores input/output snapshots through the estimate
service so historical calculations remain reviewable when live rules change.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal, InvalidOperation
import json
from typing import Any, Callable

import flet as ft

from core.area_engine import AreaSettings
from core.models import DC_RATE_UNITS
from services.dc_rate_service import (
    find_schedule_location_matches,
    list_dc_rates,
    list_location_hint_values,
)
from services.estimate_service import (
    get_estimate,
    list_client_choices,
    list_estimates,
    save_estimate,
)
from services.tax_service import (
    TaxRuleError,
    calculate_charges,
    create_tax_fee_rule,
    delete_tax_fee_rule,
    list_tax_fee_rules,
    update_tax_fee_rule,
)
from services.valuation_report_service import (
    export_estimate_history,
    export_estimate_csv,
    export_estimate_pdf,
    export_estimate_xlsx,
)
from services.registry_fee_service import (
    RegistryFeeFormulaError,
    calculate_registry_fee_formulas,
    create_registry_fee_formula,
    delete_registry_fee_formula,
    list_registry_fee_formulas,
    update_registry_fee_formula,
    create_registry_fee_custom_field,
    list_registry_fee_custom_fields,
    delete_registry_fee_custom_field,
)
from services.valuation_service import ValuationError, calculate_dual_valuation


RATE_UNITS = {
    "per_square_foot": "Per square foot", "per_square_yard": "Per square yard",
    "per_square_meter": "Per square meter", "per_marla": "Per marla",
    "per_kanal": "Per kanal", "per_acre": "Per acre", "per_kila": "Per kila",
    "per_plot": "Per plot",
}
AREA_UNITS = {
    "kmf": "K-M-F", "square_foot": "Square feet", "square_yard": "Square yards",
    "square_meter": "Square meters", "marla": "Marla", "kanal": "Kanal",
    "acre": "Acres", "kila": "Kilas",
}


def _money(value: Any, currency: str = "PKR") -> str:
    if value is None:
        return "Not supplied"
    return f"{currency} {Decimal(str(value)):,.2f}"


def build_valuation_page(
    page: ft.Page,
    *,
    on_rates_tab_change: Callable[[bool], None] | None = None,
) -> ft.Control:
    """Build the estimate workflow plus the existing rates and tax-rule pages."""
    # Populate this list asynchronously after the lightweight control tree is
    # attached; schedule location hints are queried directly from the database.
    clients: list[dict[str, Any]] = []

    status = ft.Text("", size=12, color="#475569")
    form_error = ft.Text("", color="#B91C1C", size=12)
    result_panel = ft.Column(
        spacing=8,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )

    def _reset_result_panel():
        result_panel.controls.clear()
        result_panel.controls.append(
            ft.Container(
                ft.Column([
                    ft.Icon(ft.Icons.CALCULATE_OUTLINED, size=40, color="#94A3B8"),
                    ft.Text("Calculation Breakdown", size=15, weight=ft.FontWeight.BOLD, color="#334155"),
                    ft.Text(
                        "Enter location, area (K-M-F), and DC/FBR rate above, then click 'Calculate estimate'.",
                        size=13, color="#64748B", text_align=ft.TextAlign.CENTER,
                    ),
                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=6),
                alignment=ft.Alignment.CENTER,
                padding=ft.Padding(16, 28, 16, 28),
            )
        )

    _reset_result_panel()
    history_list = ft.ListView(height=230, spacing=6)
    last_calculation: dict[str, Any] = {}
    file_picker = next(
        (service for service in page.services if isinstance(service, ft.FilePicker)),
        None,
    )
    if file_picker is None:
        file_picker = ft.FilePicker()
        page.services.append(file_picker)

    async def save_report(payload: bytes, filename: str, extension: str) -> None:
        destination = await file_picker.save_file(
            dialog_title=f"Save {extension.upper()} valuation report",
            file_name=filename,
            allowed_extensions=[extension],
            src_bytes=payload,
        )
        status.value = f"Report saved to {destination}." if destination else "Report export cancelled."
        page.update()

    def estimate_export_handler(estimate_id: int, file_format: str):
        async def export(_):
            try:
                saved = get_estimate(estimate_id)
                if saved is None:
                    raise ValueError("Saved estimate was not found.")
                exporters = {
                    "csv": export_estimate_csv,
                    "xlsx": export_estimate_xlsx,
                    "pdf": export_estimate_pdf,
                }
                payload = exporters[file_format](saved)
                await save_report(
                    payload,
                    f"valuation_estimate_{estimate_id}_{date.today().isoformat()}.{file_format}",
                    file_format,
                )
            except Exception as exc:
                status.value = f"Could not export estimate: {exc}"
                page.update()
        return export

    def history_export_handler(file_format: str):
        async def export(_):
            try:
                summaries = list_estimates(limit=500)
                full_records = [get_estimate(row["id"]) for row in summaries]
                records = [record for record in full_records if record]
                payload = export_estimate_history(records, file_format)
                await save_report(
                    payload,
                    f"valuation_estimate_history_{date.today().isoformat()}.{file_format}",
                    file_format,
                )
            except Exception as exc:
                status.value = f"Could not export estimate history: {exc}"
                page.update()
        return export

    client = ft.Dropdown(label="Client (optional)", width=250, value="none", options=[
        ft.dropdown.Option("none", "No client linked"),
        *[ft.dropdown.Option(str(item["id"]), item["name"]) for item in clients],
    ])
    # Effective date is fixed to today's date for the streamlined workflow.
    transaction_date = date.today()
    property_profile = ft.RadioGroup(value="rural", content=ft.Row([
        ft.Radio(value="rural", label="Rural"), ft.Radio(value="urban", label="Urban"),
    ], spacing=16))
    district = ft.AutoComplete(data="district", width=220, suggestions_max_height=220)
    tehsil = ft.AutoComplete(data="tehsil", width=220, suggestions_max_height=220)
    qanoongoi = ft.AutoComplete(data="qanoongoi", width=220, suggestions_max_height=220)
    mauza = ft.AutoComplete(data="mauza", width=240, suggestions_max_height=220)
    town = ft.AutoComplete(data="town", width=220, suggestions_max_height=220)
    revenue_circle = ft.AutoComplete(data="revenue_circle", width=220, suggestions_max_height=220)
    property_area = ft.AutoComplete(data="property_area", width=220, suggestions_max_height=220)
    schedule_location = ft.AutoComplete(data="schedule_location", width=240, suggestions_max_height=220)
    square_no = ft.AutoComplete(data="square_no", width=150, suggestions_max_height=220)
    kila_no = ft.AutoComplete(data="kila_no", width=150, suggestions_max_height=220)
    khasra_no = ft.TextField(label="Khasra no. (optional)", width=180)
    classification = ft.Dropdown(label="Classification (optional)", width=220, options=[
        ft.dropdown.Option("none", "All / not specified"),
    ])
    floor = ft.TextField(label="Floor (if applicable)", width=180)
    dc_rate_unit = ft.Dropdown(label="DC rate unit", hint_text="Unit appears when a matching rate is found", width=240, options=[
        ft.dropdown.Option(unit, RATE_UNITS[unit]) for unit in DC_RATE_UNITS
    ])
    fbr_rate_unit = ft.Dropdown(label="FBR rate unit (if available)", width=220, options=[
        ft.dropdown.Option(unit, RATE_UNITS[unit]) for unit in DC_RATE_UNITS
    ])
    area_value = ft.TextField(label="Area (K-M-F)", width=190, hint_text="For example: 2-5-0")
    area_unit = ft.Dropdown(label="Area input unit", value="kmf", visible=False, options=[
        ft.dropdown.Option("kmf", "K-M-F")
    ])
    dc_rate_choice = ft.Dropdown(label="Matched DC rate", hint_text="No matching rate yet", expand=True, options=[], visible=False)
    fbr_rate_choice = ft.Dropdown(label="Matched FBR rate (editable)", hint_text="No matching rate yet", expand=True, options=[], visible=False)
    location_match = ft.Dropdown(label="Choose matching schedule location", expand=True, options=[], visible=False)
    rate_match_status = ft.Text("Enter schedule details to find matching DC and FBR rates.", size=12, color="#475569")
    marla_setting = ft.Dropdown(label="Square feet per marla", value="272", width=210, options=[
        ft.dropdown.Option("272", "272 sq ft"), ft.dropdown.Option("225", "225 sq ft")
    ])
    registry_written_value = ft.TextField(label="Registry written value (PKR)", width=230)
    constructed = ft.Checkbox(label="Property is constructed", value=False)
    building_material_value = ft.TextField(label="Building material value (PKR)", width=230, value="0")
    transaction_type = ft.Dropdown(label="Transaction type", value="sale", width=190, options=[
        ft.dropdown.Option(item, item.title()) for item in ("sale", "purchase", "gift", "transfer", "other")
    ])
    buyer_filer = ft.Dropdown(label="Buyer tax status", value="unknown", width=200, options=[
        ft.dropdown.Option(item, label) for item, label in (
            ("unknown", "Unknown"), ("ATL", "ATL"), ("late_filer", "Late filer"), ("non_ATL", "Non-ATL")
        )
    ])
    seller_filer = ft.Dropdown(label="Seller tax status", value="unknown", width=200, options=[
        ft.dropdown.Option(item, label) for item, label in (
            ("unknown", "Unknown"), ("ATL", "ATL"), ("late_filer", "Late filer"), ("non_ATL", "Non-ATL")
        )
    ])
    custom_formula_fields: list[dict[str, Any]] = []
    custom_field_inputs: dict[str, ft.TextField] = {}
    custom_field_controls = ft.ResponsiveRow(run_spacing=5)

    def render_custom_field_inputs(fields: list[dict[str, Any]]) -> None:
        """Build numeric form inputs from saved custom formula field definitions."""
        custom_formula_fields.clear()
        custom_formula_fields.extend(fields)
        custom_field_inputs.clear()
        custom_field_controls.controls.clear()
        for item in custom_formula_fields:
            key = item["field_key"]
            field = ft.TextField(label=item["label"], width=220, hint_text="Optional numeric formula input")
            custom_field_inputs[key] = field
            custom_field_controls.controls.append(
                ft.Container(field, col={"xs": 12, "md": 4})
            )

    rate_records: dict[str, list[dict[str, Any]]] = {"dc": [], "fbr": []}

    hint_generation = 0

    def _text_value(control) -> str:
        return (control.value or "").strip()

    def _active_location_fields():
        if property_profile.value == "rural":
            return [("district", district), ("tehsil", tehsil),
                    ("qanoongoi", qanoongoi), ("mauza", mauza),
                    ("locality", schedule_location), ("square_no", square_no), ("kila_no", kila_no)]
        return [("tehsil", tehsil), ("committee_town", town), ("revenue_circle", revenue_circle),
                ("property_area", property_area), ("locality", schedule_location),
                ("square_no", square_no), ("kila_no", kila_no), ("khasra_no", khasra_no)]

    def _location_context() -> dict[str, str]:
        return {
            key: _text_value(control)
            for key, control in _active_location_fields()
        }

    def _apply_location_hints(values_by_field: dict[str, list[str]]) -> None:
        for key, control in _active_location_fields():
            control.suggestions = [
                ft.AutoCompleteSuggestion(key=value, value=value)
                for value in values_by_field.get(key, [])
            ]

    def refresh_location_hints() -> None:
        """Load schedule-backed type-ahead values directly from the database."""
        active = _active_location_fields()
        try:
            values_by_field = list_location_hint_values(
                land_type=property_profile.value,
                context=_location_context(),
                fields=[key for key, _control in active],
                limit=100,
            )
        except Exception as exc:
            status.value = f"Could not load location suggestions: {exc}"
            page.update()
            return
        _apply_location_hints(values_by_field)
        page.update()

    def schedule_location_hint_refresh() -> None:
        """Debounce keystroke queries so only the latest input reaches the DB."""
        nonlocal hint_generation
        hint_generation += 1
        generation = hint_generation

        async def load_hints():
            await asyncio.sleep(0.15)
            if generation != hint_generation:
                return
            active = _active_location_fields()
            try:
                values_by_field = await asyncio.to_thread(
                    list_location_hint_values,
                    land_type=property_profile.value,
                    context=_location_context(),
                    fields=[key for key, _control in active],
                    limit=100,
                )
            except Exception as exc:
                if generation == hint_generation:
                    status.value = f"Could not load location suggestions: {exc}"
                    page.update()
                return
            if generation != hint_generation:
                return
            _apply_location_hints(values_by_field)
            page.update()

        if hasattr(page, "run_task"):
            page.run_task(load_hints)
        else:
            refresh_location_hints()

    def matching_locations():
        return find_schedule_location_matches(
            land_type=property_profile.value,
            context=_location_context(),
            limit=200,
        )

    def _rate_label(rate: dict[str, Any]) -> str:
        place = rate.get("mauza") or rate.get("property_area") or rate.get("committee_town") or rate.get("location_name") or "Location"
        unit = RATE_UNITS.get(rate.get("rate_unit"), rate.get("rate_unit") or "Unit")
        return f"Rs. {rate.get('rate_value')} / {unit.removeprefix('Per ')} · {rate.get('classification') or rate.get('property_type')} · {place}"

    def refresh_rate_matches(_=None):
        """Fetch active schedule rows and preselect unique unit/rate matches."""
        lookup_value = _text_value(mauza) if property_profile.value == "rural" else (
            _text_value(property_area) or _text_value(town)
        )
        candidates = matching_locations() if lookup_value else []
        if location_match.value and any(str(row["id"]) == location_match.value for row in candidates):
            candidates = [row for row in candidates if str(row["id"]) == location_match.value]
        location_match.options = [ft.dropdown.Option(
            str(row["id"]), " · ".join(str(value) for value in (
                row.get("name"), row.get("district"), row.get("tehsil"), row.get("mauza"),
                row.get("committee_town"), row.get("property_area"),
            ) if value)
        ) for row in candidates]
        location_match.visible = len(candidates) > 1
        if len(candidates) == 1:
            location_match.value = str(candidates[0]["id"])
        if not candidates:
            rate_records["dc"], rate_records["fbr"] = [], []
            dc_rate_choice.options, fbr_rate_choice.options = [], []
            dc_rate_choice.value, fbr_rate_choice.value = None, None
            dc_rate_unit.options, fbr_rate_unit.options = [], []
            dc_rate_unit.value, fbr_rate_unit.value = None, None
            dc_rate_unit.visible = dc_rate_choice.visible = False
            fbr_rate_unit.visible = fbr_rate_choice.visible = False
            rate_match_status.value = "No schedule location matches these details. Check the selected Mauza, square and Kila."
            page.update()
            return

        candidate_ids = [int(item["id"]) for item in candidates]
        all_rates = [row for schedule in ("dc", "fbr")
                     for row in list_dc_rates(location_ids=candidate_ids,
                                              schedule_type=schedule,
                                              effective_on=transaction_date)]
        if classification.value not in (None, "", "none"):
            all_rates = [row for row in all_rates if str(row.get("classification") or "").casefold()
                         == classification.value.casefold()]
        for schedule, unit_control, rate_control in (
            ("dc", dc_rate_unit, dc_rate_choice), ("fbr", fbr_rate_unit, fbr_rate_choice)
        ):
            rows = [row for row in all_rates if row.get("schedule_type") == schedule]
            units = sorted({row["rate_unit"] for row in rows})
            unit_control.options = [ft.dropdown.Option(unit, RATE_UNITS.get(unit, unit)) for unit in units]
            unit_control.visible = bool(units) and (schedule == "dc" or property_profile.value == "urban")
            if unit_control.value not in units:
                unit_control.value = units[0] if len(units) == 1 else None
            rows = [row for row in rows if not unit_control.value or row["rate_unit"] == unit_control.value]
            rate_records[schedule] = rows
            rate_control.options = [ft.dropdown.Option(str(row["id"]), _rate_label(row)) for row in rows]
            rate_control.visible = bool(rows) and (schedule == "dc" or property_profile.value == "urban")
            if rate_control.value not in {str(row["id"]) for row in rows}:
                rate_control.value = str(rows[0]["id"]) if len(rows) == 1 else None
        classes = sorted({str(row.get("classification")) for row in all_rates if row.get("classification")})
        classification.options = [ft.dropdown.Option("none", "All / not specified")]
        classification.options.extend(ft.dropdown.Option(value, value) for value in classes)
        if classification.value not in classes and len(classes) == 1:
            classification.value = classes[0]
        selected_dc = next((row for row in rate_records["dc"]
                            if str(row["id"]) == dc_rate_choice.value), None)
        if selected_dc:
            rate_match_status.value = f"DC rate found: {_rate_label(selected_dc)}"
        else:
            rate_match_status.value = (
                "The location matches, but no active DC rate was found for today's date."
                if not rate_records["dc"] else
                f"{len(rate_records['dc'])} DC rate(s) found. Choose a unit and rate to continue."
            )
        page.update()

    def refresh_location_state(_=None):
        schedule_location_hint_refresh()
        refresh_rate_matches()

    for control in (district, tehsil, qanoongoi, mauza, town, revenue_circle, property_area,
                    schedule_location, square_no, kila_no, khasra_no, classification,
                    dc_rate_unit, fbr_rate_unit, location_match):
        control.on_change = refresh_location_state
        if isinstance(control, ft.AutoComplete):
            control.on_select = refresh_location_state

    def on_mauza_change(_=None):
        # A new Mauza invalidates cadastral choices from the prior record.
        square_no.value = ""
        kila_no.value = ""
        refresh_location_state()

    def on_square_change(_=None):
        # Kila suggestions are scoped to the selected square.
        kila_no.value = ""
        refresh_location_state()

    mauza.on_change = on_mauza_change
    mauza.on_select = on_mauza_change
    square_no.on_change = on_square_change
    square_no.on_select = on_square_change
    property_profile.on_change = refresh_location_state
    dc_rate_choice.on_select = refresh_rate_matches
    fbr_rate_choice.on_select = refresh_rate_matches

    def show_estimate(estimate_id: int):
        saved = get_estimate(estimate_id)
        if not saved:
            status.value = "Saved estimate not found."
            page.update()
            return
        valuation = saved.get("result_snapshot", {}).get("valuation", {})
        charges = saved.get("result_snapshot", {}).get("charges", {})
        details = [
            f"Estimate #{saved['id']} · {saved['transaction_date']}",
            f"Client: {saved.get('client_name') or 'Unlinked'}",
            f"Location: {(saved.get('location_snapshot') or {}).get('name', 'Not available')}",
            f"Transferred value: {_money(saved.get('transferred_value'))}",
            f"Registry written value: {_money(saved.get('input_snapshot', {}).get('registry_written_value', saved.get('declared_consideration')))}",
            "Charge totals: " + ", ".join(
                f"{currency} {amount}" for currency, amount in charges.get("totals_by_currency", {}).items()
            ),
            "Warnings: " + ("; ".join(saved.get("warnings", [])) or "None"),
            "Saved inputs: " + json.dumps(saved.get("input_snapshot", {}), ensure_ascii=False),
        ]
        for item in saved.get("charges", []):
            details.append(f"{item['name']} ({item.get('payer') or 'payer unspecified'}): {_money(item['amount'], item.get('currency_code', 'PKR'))}")
        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text(f"Saved estimate #{estimate_id}"),
            content=ft.Column([ft.Text(line, selectable=True) for line in details],
                              tight=True, scroll=ft.ScrollMode.AUTO, width=640, height=500),
            actions=[
                ft.OutlinedButton("Export CSV", on_click=estimate_export_handler(estimate_id, "csv")),
                ft.OutlinedButton("Export Excel", on_click=estimate_export_handler(estimate_id, "xlsx")),
                ft.OutlinedButton("Export PDF", on_click=estimate_export_handler(estimate_id, "pdf")),
                ft.TextButton("Close", on_click=lambda _: page.pop_dialog()),
            ],
        ))

    def refresh_history(records=None):
        history_list.controls.clear()
        try:
            records = list_estimates(limit=100) if records is None else records
            if not records:
                history_list.controls.append(ft.Text("No saved estimates yet.", color="#64748B"))
            for record in records:
                history_list.controls.append(ft.Container(
                    padding=10, bgcolor="#FFFFFF", border=ft.Border.all(1, "#E2E8F0"), border_radius=8,
                    content=ft.Row([
                        ft.Column([
                            ft.Text(f"{record['property_type']} · {record['transaction_date']}", weight=ft.FontWeight.BOLD),
                            ft.Text(f"{record.get('client_name') or 'Unlinked client'} · transferred {_money(record['transferred_value'])}", size=12),
                        ], spacing=3, expand=True),
                        ft.TextButton("Open", on_click=lambda _, estimate_id=record["id"]: show_estimate(estimate_id)),
                    ]),
                ))
        except Exception as exc:
            history_list.controls.append(ft.Text(f"Could not load saved estimates: {exc}", color="#B91C1C"))

    def draw_result(valuation: dict[str, Any], charges: dict[str, Any]):
        result_panel.controls.clear()
        result_panel.controls.extend([
            ft.Text(f"DC valuation: {_money(valuation['dc_value'])}", size=17, weight=ft.FontWeight.BOLD),
            ft.Text(f"FBR valuation: {_money(valuation['fbr_value']) if valuation['fbr_value'] is not None else 'Schedule unavailable'}", size=16),
            ft.Text(f"Registry written value: {_money(valuation['registry_written_value'])}", size=13),
            ft.Divider(),
        ])
        result_panel.controls.extend(ft.Text(line, size=12) for line in valuation["explanation"])
        if charges["charges"]:
            result_panel.controls.append(ft.Text("Taxes and fees", weight=ft.FontWeight.BOLD))
            result_panel.controls.extend(ft.Text(
                f"{line['name']} · {line.get('payer') or 'payer unspecified'}: {_money(line['amount'], line['currency_code'])}"
            ) for line in charges["charges"])
        else:
            result_panel.controls.append(ft.Text("No configured charge rules applied.", color="#64748B"))
        result_panel.controls.extend(ft.Text(
            f"Total {currency}: {_money(amount, currency)}", weight=ft.FontWeight.BOLD
        ) for currency, amount in charges["totals_by_currency"].items())
        warnings = valuation["warnings"] + charges["warnings"] + [
            f"{item['rule']}: {item['reason']}" for item in charges["skipped_rules"]
        ]
        if warnings:
            result_panel.controls.append(ft.Text("Review required", color="#B45309", weight=ft.FontWeight.BOLD))
            result_panel.controls.extend(ft.Text(f"• {warning}", color="#B45309", size=12) for warning in warnings)
        result_panel.controls.extend(ft.Text(line, size=12, color="#64748B") for line in charges.get("explanation", []))
        result_panel.update()

    def calculate(_):
        form_error.value = ""
        last_calculation.clear()
        try:
            refresh_rate_matches()
            dc_rate = next((row for row in rate_records["dc"]
                            if str(row["id"]) == dc_rate_choice.value), None)
            if dc_rate is None:
                raise ValuationError("Complete the location fields and choose a matching DC schedule rate.")
            selected_location = dc_rate
            selected_mauza = (_text_value(mauza) if property_profile.value == "rural"
                              else selected_location.get("mauza") or selected_location.get("name") or "").strip()
            if not selected_mauza:
                selected_mauza = selected_location.get("name") or ""
            effective_classification = (
                classification.value if classification.value not in (None, "", "none")
                else dc_rate.get("classification") or dc_rate.get("property_type")
            )
            settings = AreaSettings(square_feet_per_marla=int(marla_setting.value))
            material_value = Decimal((building_material_value.value or "0").strip()) if constructed.value else Decimal("0")
            if material_value < 0:
                raise ValuationError("Building material value cannot be negative.")
            written_value = (registry_written_value.value or "").strip() or None
            fbr_rate = next((row for row in rate_records["fbr"]
                             if str(row["id"]) == fbr_rate_choice.value), None)
            valuation = calculate_dual_valuation(
                location_id=int(dc_rate["location_id"]),
                fbr_location_id=int(fbr_rate["location_id"]) if fbr_rate else None,
                property_type=effective_classification,
                classification=effective_classification,
                floor=dc_rate.get("floor"),
                transaction_date=transaction_date,
                area_value=area_value.value,
                area_unit="kmf",
                dc_rate_unit=dc_rate_unit.value or None,
                fbr_rate_unit=fbr_rate_unit.value or None,
                dc_rate_override=dc_rate,
                fbr_rate_override=fbr_rate,
                registry_written_value=written_value,
                settings=settings,
            )
            area_marla = Decimal(str(valuation["area_square_feet"])) / Decimal(settings.square_feet_per_marla)
            area_kanal = area_marla / Decimal(20)
            area_acre = area_kanal / Decimal(8)
            context = {
                "transaction_type": transaction_type.value,
                "property_type": effective_classification,
                "classification": effective_classification,
                "land_type": selected_location.get("land_type"),
                "buyer_filer_status": buyer_filer.value,
                "seller_filer_status": seller_filer.value,
                "property_value": valuation["property_value"],
                "transferred_value": valuation["transferred_value"],
                "declared_consideration": valuation["registry_written_value"],
                "registry_written_value": valuation["registry_written_value"],
                "building_material_value": material_value,
                "dc_value": valuation["dc_value"],
                "fbr_value": valuation["fbr_value"],
                "area": area_marla,
                "area_marla": area_marla,
                "area_kanal": area_kanal,
                "area_acre": area_acre,
                "area_square_feet": valuation["area_square_feet"],
                "mauza": selected_mauza,
                "property_profile": property_profile.value,
                "district": _text_value(district) or selected_location.get("district"),
                "tehsil": _text_value(tehsil) or selected_location.get("tehsil"),
                "town": _text_value(town) or selected_location.get("committee_town"),
                "revenue_circle": _text_value(revenue_circle) or selected_location.get("revenue_circle"),
                "property_area": _text_value(property_area) or selected_location.get("property_area"),
                "location": _text_value(schedule_location) or selected_location.get("locality") or selected_location.get("name"),
                "square_no": (square_no.value or "").strip() or None,
                "kila_no": (kila_no.value or "").strip() or None,
                "khasra_no": (khasra_no.value or "").strip() or None,
                "has_fbr_schedule": valuation["fbr_value"] is not None,
                "area_quantity": valuation["rate_quantity"],
                "area_rate_unit": valuation["rate_unit"],
                "plot_count": 1,
                "plot_unit": "plot",
                "property_count": 1,
                "property_unit": "property",
            }
            custom_values = {}
            for key, field in custom_field_inputs.items():
                value = (field.value or "").strip()
                if value:
                    try:
                        custom_values[key] = Decimal(value)
                    except (InvalidOperation, ValueError):
                        raise ValueError(f"Custom field '{field.label}' must be a valid number.") from None
            context.update(custom_values)
            rules = list_tax_fee_rules()
            # Both values are available to rules; each rule explicitly picks
            # DC or FBR as its calculation base. Registry formulas use the same
            # separated context and save their expression/source snapshots.
            charges = calculate_charges(rules=rules, transaction_date=transaction_date,
                                        context=context, jurisdiction=["Punjab", "Punjab, Pakistan"])
            formulas_result = calculate_registry_fee_formulas(
                formulas=list_registry_fee_formulas(), effective_on=transaction_date,
                context=context, jurisdictions=["Punjab", "Punjab, Pakistan"],
                custom_variables=[field["field_key"] for field in custom_formula_fields],
            )
            charges["charges"].extend(formulas_result["charges"])
            for currency, amount in formulas_result["totals_by_currency"].items():
                charges["totals_by_currency"][currency] = charges["totals_by_currency"].get(currency, Decimal("0")) + amount
            charges["warnings"].extend(formulas_result["warnings"])
            charges["warnings"].extend(f"{item['rule']}: {item['reason']}" for item in formulas_result["skipped_rules"])
            if charges["skipped_rules"]:
                charges["warnings"].extend(
                    f"{item['rule']}: {item['reason']}" for item in charges["skipped_rules"]
                )
            inputs = {
                "location_id": int(dc_rate["location_id"]),
                "client_id": int(client.value) if client.value not in (None, "none") else None,
                "transaction_date": transaction_date,
                "transaction_type": transaction_type.value,
                "jurisdiction": "Punjab",
                "mauza": selected_mauza,
                "square_no": (square_no.value or "").strip() or None,
                "kila_no": (kila_no.value or "").strip() or None,
                "khasra_no": (khasra_no.value or "").strip() or None,
                "property_type": effective_classification,
                "classification": effective_classification,
                "schedule_type": "dc_and_fbr",
                "transaction_date_is_automatic": True,
                "floor": dc_rate.get("floor"),
                "area_value": area_value.value,
                "area_unit": "kmf",
                "square_feet_per_marla": settings.square_feet_per_marla,
                "dc_rate_unit": dc_rate_unit.value,
                "fbr_rate_unit": fbr_rate_unit.value,
                "registry_written_value": valuation["registry_written_value"],
                "building_material_value": material_value,
                "constructed": constructed.value,
                # Model compatibility: the selected property is treated as a
                # complete property for this stage; no share inputs are shown.
                "share_numerator": 1,
                "share_denominator": 1,
                "declared_consideration": valuation["registry_written_value"],
                "buyer_filer_status": buyer_filer.value,
                "seller_filer_status": seller_filer.value,
                "custom_formula_fields": custom_values,
                "fbr_rate_id": int(fbr_rate["id"]) if fbr_rate else None,
            }
            last_calculation.update({"inputs": inputs, "valuation": valuation, "charges": charges})
            draw_result(valuation, charges)
            status.value = "Estimate calculated. Review warnings before relying on any charge." 
        except (ValuationError, TaxRuleError, RegistryFeeFormulaError, ValueError, TypeError, StopIteration) as exc:
            form_error.value = str(exc)
        except Exception as exc:
            form_error.value = f"Could not calculate estimate: {exc}"
        page.update()

    def save_current(_):
        if not last_calculation:
            status.value = "Calculate an estimate before saving it."
            page.update()
            return
        try:
            payload = last_calculation
            client_id = payload["inputs"].get("client_id")
            estimate_id = save_estimate(
                inputs=payload["inputs"], valuation=payload["valuation"],
                charges=payload["charges"], client_id=client_id,
            )
            refresh_history()
            status.value = f"Estimate #{estimate_id} saved with its rate and charge snapshots."
        except Exception as exc:
            status.value = f"Could not save estimate: {exc}"
        page.update()

    formula_list = ft.ListView(height=180, spacing=5)

    def confirm_remove_formula(record: dict[str, Any]) -> None:
        """Ask before deleting one formula; nothing is removed without approval."""

        def do_remove(_=None):
            try:
                delete_registry_fee_formula(int(record["id"]))
                page.pop_dialog()
                refresh_formulas()
                status.value = f"Removed formula '{record['name']}'."
                status.color = "#15803D"
            except RegistryFeeFormulaError as exc:
                page.pop_dialog()
                status.value = str(exc)
                status.color = "#B91C1C"
            except Exception as exc:
                page.pop_dialog()
                status.value = f"Could not remove formula: {exc}"
                status.color = "#B91C1C"
            page.update()

        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("Remove registry formula?"),
            content=ft.Column([
                ft.Text(f"Remove formula '{record['name']}' ({record.get('formula_expression', '')})? It will stop applying to future calculations.", size=13),
                ft.Text("Saved estimates keep the charge snapshots recorded when they were saved.", size=12, color="#64748B"),
            ], tight=True, spacing=8),
            actions=[
                ft.TextButton("Cancel", on_click=lambda _: page.pop_dialog()),
                ft.TextButton("Remove", on_click=do_remove),
            ],
        ))

    def refresh_formulas():
        """Reload persisted fee formulas shown directly under the calculator."""
        formula_list.controls.clear()
        try:
            formulas = list_registry_fee_formulas()
            if not formulas:
                formula_list.controls.append(ft.Text("No registry fee formulas saved.", color="#64748B"))
            for formula in formulas:
                formula_list.controls.append(ft.Row([
                    ft.Column([
                        ft.Text(f"{formula['name']} · {formula['category']}", weight=ft.FontWeight.BOLD),
                        ft.Text(f"{formula['formula_expression']} · {formula['verification_status']}", size=12),
                    ], expand=True, spacing=2),
                    ft.TextButton("Remove", on_click=lambda _, row=formula: confirm_remove_formula(row)),
                    ft.TextButton("Edit", on_click=lambda _, row=formula: open_formula_dialog(row)),
                ]))
        except Exception as exc:
            formula_list.controls.append(ft.Text(f"Could not load registry formulas: {exc}", color="#B91C1C"))

    def open_formula_dialog(existing: dict[str, Any] | None = None):
        """Create or update one persisted formula; formulas remain unverified by default."""
        existing = existing or {}
        name = ft.TextField(label="Fee name", value=existing.get("name", ""), expand=True)
        category = ft.TextField(label="Category", value=existing.get("category", "registry_fee"), expand=True)
        expression = ft.TextField(
            label="Formula", value=existing.get("formula_expression", ""),
            hint_text="Example: area * 2.5% + 2000", expand=True,
        )
        conditions = ft.TextField(
            label="Optional match conditions (JSON)",
            value=existing.get("eligibility_conditions_json", ""),
            hint_text='Example: {"when":{"mauza":"Sample","square_no":"12"}}',
            multiline=True, min_lines=2, max_lines=4, expand=True,
        )
        source = ft.TextField(label="Source reference (optional)", value=existing.get("source_reference") or "", expand=True)
        effective_from = ft.TextField(
            label="Effective from", value=str(existing.get("effective_from") or date.today().isoformat()), expand=True
        )
        effective_to = ft.TextField(
            label="Effective to (optional)", value=str(existing.get("effective_to") or ""), expand=True
        )
        payer = ft.Dropdown(label="Payer (optional)", value=existing.get("payer"), expand=True, options=[
            ft.dropdown.Option("buyer", "Buyer"), ft.dropdown.Option("seller", "Seller"),
            ft.dropdown.Option("other", "Other"),
        ])
        formula_error = ft.Text("", color="#B91C1C", size=12)

        def use_plra_formula(_=None):
            name.value = "PLRA charges"
            category.value = "plra"
            expression.value = "3300 if dc_value < 3000000 else dc_value * 0.1%"
            page.update()

        def save_formula(_):
            try:
                values = {
                    "name": name.value, "category": category.value,
                    "jurisdiction": "Punjab", "formula_expression": expression.value,
                    "eligibility_conditions_json": conditions.value,
                    "source_reference": source.value,
                    "effective_from": effective_from.value,
                    "effective_to": effective_to.value,
                    "payer": payer.value,
                    "verification_status": "unverified",
                }
                if existing.get("id"):
                    update_registry_fee_formula(formula_id=existing["id"], **values)
                else:
                    create_registry_fee_formula(**values)
                page.pop_dialog()
                refresh_formulas()
                status.value = "Registry fee formula saved. It is marked unverified until confirmed from an authoritative source."
            except (RegistryFeeFormulaError, ValueError) as exc:
                formula_error.value = str(exc)
            except Exception as exc:
                formula_error.value = f"Could not save formula: {exc}"
            page.update()

        page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text("Registry fee formula"),
            content=ft.Column([
                ft.Text(
                    "Built-in values: area (marlas), area_kanal, area_acre, area_square_feet, dc_value, fbr_value, registry_written_value, building_material_value. "
                    "Use + - * /, parentheses, and numeric conditions. Custom numeric fields are available as formula keys.",
                    size=12, color="#475569",
                ),
                ft.Text("PLRA example supplied by the user (unverified; assumes DC valuation): fixed 3,300 below 3,000,000, otherwise 0.1% of DC valuation.", size=12, color="#9A3412"),
                ft.OutlinedButton("Use supplied PLRA formula", on_click=use_plra_formula),
                ft.ResponsiveRow([
                    ft.Container(name, col={"xs": 12, "md": 6}),
                    ft.Container(category, col={"xs": 12, "md": 6}),
                    ft.Container(expression, col=12),
                    ft.Container(conditions, col=12),
                    ft.Container(source, col=12),
                    ft.Container(effective_from, col={"xs": 12, "md": 6}),
                    ft.Container(effective_to, col={"xs": 12, "md": 6}),
                    ft.Container(payer, col={"xs": 12, "md": 6}),
                ], run_spacing=4),
                formula_error,
            ], tight=True, scroll=ft.ScrollMode.AUTO, width=620, height=460),
            actions=[
                ft.TextButton("Cancel", on_click=lambda _: page.pop_dialog()),
                *([
                    ft.TextButton(
                        "Remove formula",
                        style=ft.ButtonStyle(color="#B91C1C"),
                        on_click=lambda _, row=existing: [page.pop_dialog(), confirm_remove_formula(row)],
                    )
                ] if existing.get("id") else []),
                ft.FilledButton("Save formula", on_click=save_formula),
            ],
        ))

    custom_fields_status = ft.Text("", size=12, color="#475569")

    def open_custom_fields_dialog(_=None):
        """Manage numeric inputs that are exposed to saved fee formulas."""
        label_field = ft.TextField(label="Field label", width=240)
        key_field = ft.TextField(label="Formula key", hint_text="e.g. plot_count_extra", width=240)
        error = ft.Text("", size=12, color="#B91C1C")
        saved_fields = ft.Column(height=260, spacing=5, scroll=ft.ScrollMode.AUTO)

        def load_fields():
            saved_fields.controls.clear()
            try:
                rows = list_registry_fee_custom_fields()
                render_custom_field_inputs(rows)
                if not rows:
                    saved_fields.controls.append(ft.Text("No custom fields defined yet."))
                for item in rows:
                    saved_fields.controls.append(ft.Row([
                        ft.Text(f"{item['label']}  ·  {item['field_key']}", expand=True),
                        ft.TextButton("Remove", on_click=lambda _, row=item: remove_field(row)),
                    ]))
            except Exception as exc:
                saved_fields.controls.append(ft.Text(f"Could not load custom fields: {exc}", color="#B91C1C"))

        def add_field(_=None):
            try:
                create_registry_fee_custom_field(label=label_field.value, field_key=key_field.value)
                label_field.value = ""
                key_field.value = ""
                error.value = ""
                load_fields()
                custom_fields_status.value = "Custom numeric field added. It is now available in the Calculate form and fee formulas."
                page.update()
            except (RegistryFeeFormulaError, ValueError) as exc:
                error.value = str(exc)
                page.update()
            except Exception as exc:
                error.value = f"Could not add custom field: {exc}"
                page.update()

        def remove_field(item):
            try:
                delete_registry_fee_custom_field(item["id"])
                error.value = ""
                load_fields()
                custom_fields_status.value = f"Removed custom field '{item['label']}'."
            except RegistryFeeFormulaError as exc:
                error.value = str(exc)
            page.update()

        page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text("Custom formula fields"),
            content=ft.Column([
                ft.Text("Custom fields are numeric inputs. Formula keys use lowercase letters, digits, and underscores; fields used by saved formulas cannot be removed."),
                ft.Row([label_field, key_field, ft.Button("Add field", on_click=add_field)], wrap=True),
                error, ft.Divider(), saved_fields,
            ], width=680, height=430, spacing=8, scroll=ft.ScrollMode.AUTO),
            actions=[ft.TextButton("Close", on_click=lambda _: page.pop_dialog())],
        ))
        load_fields()
        page.update()

    # These controls form one shared chain: profile-specific location fields
    # query schedule-backed locations, the rate selectors expose the matching
    # DC/FBR rows, and Calculate passes those exact rows to the valuation
    # service rather than asking it to guess from a partial address.
    district_col = ft.Container(ft.Column([ft.Text("District", size=12, weight=ft.FontWeight.W_500), district], spacing=4), col={"xs": 12, "sm": 6, "md": 3})
    tehsil_col = ft.Container(ft.Column([ft.Text("Tehsil", size=12, weight=ft.FontWeight.W_500), tehsil], spacing=4), col={"xs": 12, "sm": 6, "md": 3})
    qanoongoi_col = ft.Container(ft.Column([ft.Text("Qanoongoi (optional)", size=12, weight=ft.FontWeight.W_500), qanoongoi], spacing=4), col={"xs": 12, "sm": 6, "md": 3})
    mauza_col = ft.Container(ft.Column([ft.Text("Mauza (type to search schedules)", size=12, weight=ft.FontWeight.W_500), mauza], spacing=4), col={"xs": 12, "sm": 6, "md": 3})
    town_col = ft.Container(ft.Column([ft.Text("Town / city", size=12, weight=ft.FontWeight.W_500), town], spacing=4), col={"xs": 12, "sm": 6, "md": 3}, visible=False)
    revenue_circle_col = ft.Container(ft.Column([ft.Text("Revenue circle", size=12, weight=ft.FontWeight.W_500), revenue_circle], spacing=4), col={"xs": 12, "sm": 6, "md": 3}, visible=False)
    property_area_col = ft.Container(ft.Column([ft.Text("Property area", size=12, weight=ft.FontWeight.W_500), property_area], spacing=4), col={"xs": 12, "sm": 6, "md": 3}, visible=False)
    classification_col = ft.Container(ft.Column([ft.Text("Classification", size=12, weight=ft.FontWeight.W_500), classification], spacing=4), col={"xs": 12, "sm": 6, "md": 3}, visible=False)
    locality_col = ft.Container(ft.Column([ft.Text("Locality / sub-area (optional)", size=12, weight=ft.FontWeight.W_500), schedule_location], spacing=4), col={"xs": 12, "sm": 6, "md": 6})
    square_col = ft.Container(ft.Column([ft.Text("Square no. (optional)", size=12, weight=ft.FontWeight.W_500), square_no], spacing=4), col={"xs": 6, "sm": 3, "md": 3})
    kila_col = ft.Container(ft.Column([ft.Text("Kila no. (optional)", size=12, weight=ft.FontWeight.W_500), kila_no], spacing=4), col={"xs": 6, "sm": 3, "md": 3})
    khasra_col = ft.Container(ft.Column([ft.Text("Khasra no. (optional)", size=12, weight=ft.FontWeight.W_500), khasra_no], spacing=4), col={"xs": 6, "sm": 3, "md": 3}, visible=False)

    location_fields_row = ft.ResponsiveRow([
        district_col,
        tehsil_col,
        qanoongoi_col,
        mauza_col,
        town_col,
        revenue_circle_col,
        property_area_col,
        classification_col,
        locality_col,
        square_col,
        kila_col,
        khasra_col,
    ], run_spacing=8)

    advanced_inputs = ft.Container(
        ft.ResponsiveRow([
            ft.Container(client, col={"xs": 12, "md": 6}),
            ft.Container(registry_written_value, col={"xs": 12, "md": 6}),
            ft.Container(constructed, col={"xs": 12, "md": 4}),
            ft.Container(building_material_value, col={"xs": 12, "md": 4}),
            ft.Container(transaction_type, col={"xs": 12, "md": 4}),
            ft.Container(buyer_filer, col={"xs": 12, "md": 4}),
            ft.Container(seller_filer, col={"xs": 12, "md": 4}),
            ft.Container(floor, col={"xs": 12, "md": 4}),
            ft.Container(marla_setting, col={"xs": 12, "md": 4}),
        ], run_spacing=8),
        padding=12,
        bgcolor="#F8FAFC",
        border=ft.Border.all(1, "#E2E8F0"),
        border_radius=8,
        visible=False,
    )
    advanced_button = ft.TextButton("Show advanced calculation fields")

    def toggle_advanced_inputs(_=None):
        advanced_inputs.visible = not advanced_inputs.visible
        advanced_button.content = "Hide advanced calculation fields" if advanced_inputs.visible else "Show advanced calculation fields"
        page.update()

    def switch_property_profile(_=None):
        is_rural = property_profile.value == "rural"
        district_col.visible = is_rural
        qanoongoi_col.visible = is_rural
        mauza_col.visible = is_rural
        town_col.visible = not is_rural
        revenue_circle_col.visible = not is_rural
        property_area_col.visible = not is_rural
        classification_col.visible = not is_rural
        khasra_col.visible = not is_rural
        if is_rural:
            locality_col.col = {"xs": 12, "sm": 6, "md": 6}
            square_col.col = {"xs": 6, "sm": 3, "md": 3}
            kila_col.col = {"xs": 6, "sm": 3, "md": 3}
        else:
            locality_col.col = {"xs": 12, "sm": 6, "md": 3}
            square_col.col = {"xs": 4, "sm": 2, "md": 2}
            kila_col.col = {"xs": 4, "sm": 2, "md": 2}
            khasra_col.col = {"xs": 4, "sm": 2, "md": 2}
        fbr_rate_choice.visible = not is_rural
        fbr_rate_unit.visible = not is_rural
        refresh_rate_matches()

    advanced_button.on_click = toggle_advanced_inputs
    property_profile.on_change = switch_property_profile
    switch_property_profile()

    result_card = ft.Container(
        result_panel,
        padding=16,
        bgcolor="#FFFFFF",
        border=ft.Border.all(1, "#E2E8F0"),
        border_radius=10,
        height=360,
        alignment=ft.Alignment.TOP_LEFT,
        expand=False,
    )

    estimate_form = ft.Column([
        ft.Text("Estimate inputs", size=19, weight=ft.FontWeight.BOLD),
        ft.Text("Choose Rural or Urban, enter schedule-backed location details, then enter the area in K-M-F format. Matching DC and FBR rates are fetched from saved schedules.", size=12, color="#9A3412"),
        ft.Container(
            ft.Column([
                ft.Text("Property Profile & Location", size=15, weight=ft.FontWeight.BOLD, color="#1E293B"),
                property_profile,
                location_fields_row,
                location_match,
                rate_match_status,
            ], spacing=10),
            padding=14,
            bgcolor="#FFFFFF",
            border=ft.Border.all(1, "#E2E8F0"),
            border_radius=10,
        ),
        ft.Container(
            ft.Column([
                ft.Text("Valuation Rates & Area", size=15, weight=ft.FontWeight.BOLD, color="#1E293B"),
                ft.ResponsiveRow([
                    ft.Container(dc_rate_unit, col={"xs": 12, "md": 4}),
                    ft.Container(dc_rate_choice, col={"xs": 12, "md": 8}),
                    ft.Container(fbr_rate_unit, col={"xs": 12, "md": 4}),
                    ft.Container(fbr_rate_choice, col={"xs": 12, "md": 8}),
                    ft.Container(area_value, col={"xs": 12, "md": 4}),
                ], run_spacing=8),
            ], spacing=10),
            padding=14,
            bgcolor="#FFFFFF",
            border=ft.Border.all(1, "#E2E8F0"),
            border_radius=10,
        ),
        advanced_button,
        advanced_inputs,
        custom_field_controls,
        ft.Row([
            ft.Button("Calculate estimate", on_click=calculate),
            ft.OutlinedButton("Save estimate", on_click=save_current),
        ], wrap=True, spacing=8),
        form_error,
        status,
        ft.ResponsiveRow([
            ft.Container(result_card, col=12),
        ]),
        ft.Text("Saved estimate history", size=18, weight=ft.FontWeight.BOLD),
        ft.Row([
            ft.OutlinedButton("Export history CSV", on_click=history_export_handler("csv")),
            ft.OutlinedButton("Export history Excel", on_click=history_export_handler("xlsx")),
            ft.OutlinedButton("Export history PDF", on_click=history_export_handler("pdf")),
        ], wrap=True, spacing=6),
        history_list,
    ], spacing=12, scroll=ft.ScrollMode.AUTO, expand=True)
    history_list.controls.append(ft.Text("Loading estimate history…", color="#64748B"))
    formula_list.controls.append(ft.Text("Loading saved formulas…", color="#64748B"))

    rule_status = ft.Text("", size=12, color="#475569")
    # Keep the viewport collapsed when empty. It grows with the rows up to a
    # scrollable limit so an empty list cannot leave a gray block on the page.
    rules_list = ft.ListView(data="tax_fee_rules", height=1, spacing=8, padding=4, visible=False)
    rules_empty_state = ft.Text("Loading tax and fee rules…", size=12, color="#64748B")

    def refresh_rules(rules=None):
        rules_list.controls.clear()
        try:
            rules = list_tax_fee_rules() if rules is None else rules
            if not rules:
                rules_list.visible = False
                rules_list.height = 1
                rules_empty_state.value = "No tax or fee rules saved yet."
                rules_empty_state.color = "#64748B"
                rules_empty_state.visible = True
                page.update()
                return
            for rule in rules:
                method_value = rule["calculation_method"]
                # Human-readable method name; falls back to the raw value so
                # an old rule saved with an unexpected method still shows up.
                method_label = {
                    "percentage": "Percentage",
                    "fixed_amount": "Fixed amount",
                    "per_unit": "Per unit",
                    "fixed_or_percentage": "Fixed or percentage",
                }.get(method_value, method_value)
                details = f"{rule['jurisdiction']} · {rule['category']} · {method_label} · {rule.get('verification_status', 'unverified').upper()}"
                if method_value == "fixed_amount":
                    amount = _money(rule.get("fixed_amount"), rule.get("currency_code", "PKR"))
                elif method_value == "fixed_or_percentage":
                    # Show both halves of the hybrid rule explicitly so the
                    # list explains the same behaviour the calculation uses.
                    amount = (
                        f"{_money(rule.get('fixed_amount'), rule.get('currency_code', 'PKR'))}"
                        f" if value below {rule.get('percentage_threshold')}, "
                        f"otherwise {rule.get('rate_value')}%"
                    )
                else:
                    amount = f"{rule.get('rate_value')} {rule.get('rate_unit') or '%'}"
                rules_list.controls.append(ft.Container(
                    padding=10, bgcolor="#FFFFFF", border=ft.Border.all(1, "#E2E8F0"), border_radius=8,
                    content=ft.Row([
                        ft.Column([ft.Text(rule["name"], weight=ft.FontWeight.BOLD),
                                   ft.Text(details, size=12),
                                   ft.Text(f"{amount} · {rule['effective_from']} to {rule.get('effective_to') or 'open'}", size=12)],
                                  spacing=3, expand=True),
                        ft.TextButton("Remove", on_click=lambda _, record=rule: confirm_remove_rule(record)),
                        ft.TextButton("Edit", on_click=lambda _, record=rule: open_rule_dialog(record)),
                    ]),
                ))
            # One compact row is enough for one rule; larger collections grow
            # to a capped viewport and scroll independently of the page.
            rules_list.height = min(300, max(104, len(rules) * 112))
            rules_list.visible = True
            rules_empty_state.visible = False
        except Exception as exc:
            rules_list.visible = False
            rules_list.height = 1
            rules_empty_state.value = f"Could not load rules: {exc}"
            rules_empty_state.color = "#B91C1C"
            rules_empty_state.visible = True
        page.update()

    def confirm_remove_rule(record: dict[str, Any]) -> None:
        """Ask before deleting one rule; nothing is removed without approval.

        Saved estimates keep their own charge snapshots, so removal affects
        only future calculations.
        """

        def do_remove(_=None):
            try:
                delete_tax_fee_rule(int(record["id"]))
                page.pop_dialog()
                refresh_rules()
                rule_status.value = (
                    f"Removed rule '{record['name']}'. Saved estimates keep "
                    "their recorded charges."
                )
                notification = ft.SnackBar(
                    content=ft.Text("Tax / fee rule removed."), duration=2500
                )
            except TaxRuleError as exc:
                page.pop_dialog()
                rule_status.value = str(exc)
                rule_status.color = "#B91C1C"
            except Exception as exc:
                page.pop_dialog()
                rule_status.value = f"Could not remove rule: {exc}"
                rule_status.color = "#B91C1C"
            page.update()

        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("Remove tax/fee rule?"),
            content=ft.Column([
                ft.Text(
                    f"Remove rule '{record['name']}'? It will stop applying to "
                    "future calculations.",
                    size=13,
                ),
                ft.Text(
                    "Saved estimates are not changed; each one keeps the charge "
                    "snapshot recorded when it was saved.",
                    size=12, color="#64748B",
                ),
            ], tight=True, spacing=8),
            actions=[
                ft.TextButton("Cancel", on_click=lambda _: page.pop_dialog()),
                ft.TextButton("Remove", on_click=do_remove),
            ],
        ))

    def open_rule_dialog(existing: dict[str, Any] | None = None):
        existing = existing or {}
        rule_name = ft.TextField(label="Rule name", value=existing.get("name", ""), width=260)
        category = ft.TextField(label="Category", value=existing.get("category", ""), width=220)
        rule_jurisdiction = ft.TextField(label="Jurisdiction", value=existing.get("jurisdiction", "Punjab"), width=220)
        method = ft.Dropdown(label="Calculation method", value=existing.get("calculation_method", "percentage"), width=220, options=[
            ft.dropdown.Option("percentage", "Percentage"),
            ft.dropdown.Option("fixed_amount", "Fixed amount"),
            ft.dropdown.Option("per_unit", "Per unit"),
            ft.dropdown.Option("fixed_or_percentage", "Fixed or percentage"),
        ])
        rate = ft.TextField(label="Rate / percent", value=str(existing.get("rate_value") or ""), width=180)
        fixed = ft.TextField(label="Fixed amount", value=str(existing.get("fixed_amount") or ""), width=180)
        # Hybrid method only: base value below this number takes the fixed
        # amount, otherwise the percentage applies. Never shown for the
        # other methods so it cannot gather a stale value.
        threshold = ft.TextField(
            label="Use fixed below this value (% above)",
            value=str(existing.get("percentage_threshold") or ""), width=200,
        )
        rate_unit_field = ft.TextField(label="Rate unit (required for per-unit)", value=existing.get("rate_unit") or "", width=220)
        minimum = ft.TextField(label="Minimum charge", value=str(existing.get("minimum_amount") or ""), width=180)
        maximum = ft.TextField(label="Maximum charge", value=str(existing.get("maximum_amount") or ""), width=180)
        conditions = ft.TextField(
            label="Eligibility / payer / base JSON", multiline=True,
            value=existing.get("eligibility_conditions_json") or '{"base":"transferred_value","payer":"buyer"}',
            width=560, min_lines=2, max_lines=5,
            hint_text='Percentage bases include property_value, transferred_value, registry_written_value, building_material_value, dc_value, and fbr_value.',
        )
        error = ft.Text("", color="#B91C1C", size=12)

        def update_rule_method(_=None):
            """Keep method-specific fields visible and clear stale values."""
            # Rate applies everywhere except the pure fixed rule; the fixed
            # amount applies to fixed and the hybrid; the threshold only to
            # the hybrid; the unit only to per-unit.
            rate.visible = method.value in ("percentage", "per_unit", "fixed_or_percentage")
            fixed.visible = method.value in ("fixed_amount", "fixed_or_percentage")
            threshold.visible = method.value == "fixed_or_percentage"
            rate_unit_field.visible = method.value == "per_unit"
            # Clear fields that the chosen method ignores so switching
            # method cannot leave a hidden stale value in the saved rule.
            if not rate.visible:
                rate.value = ""
            if not fixed.visible:
                fixed.value = ""
            if not threshold.visible:
                threshold.value = ""
            page.update()

        method.on_select = update_rule_method
        update_rule_method()

        def use_percentage_preset(label: str):
            rule_name.value = label
            category.value = label.casefold().replace(" ", "_")
            method.value = "percentage"
            rate.value = ""
            conditions.value = '{"base":"dc_value","payer":"buyer"}'
            update_rule_method()

        def open_236_case_chooser(section: str):
            """Preset for 236C/236K, which apply different rates by filer case.

            Section 236C applies to the seller (transferor), while Section 236K
            applies to the buyer (purchaser). Each section has its own rate for
            active filers (ATL), in-active cases, and late filers (previous years).
            """
            cases = [
                ("Active filer", "ATL"),
                ("In-active case", "non_ATL"),
                ("Late filer (previous years)", "late_filer"),
            ]
            payer = "seller" if section == "236C" else "buyer"
            filer_key = "seller_filer_status" if section == "236C" else "buyer_filer_status"
            role_label = "seller (transferor)" if section == "236C" else "buyer (purchaser)"

            def choose(case_label, status, _=None):
                rule_name.value = f"{section} ({case_label})"
                category.value = "236c" if section == "236C" else "236k"
                method.value = "percentage"
                rate.value = ""
                conditions.value = json.dumps({
                    "base": "dc_value",
                    "payer": payer,
                    "when": {filer_key: status},
                }, sort_keys=True)
                update_rule_method()
                page.pop_dialog()

            page.show_dialog(ft.AlertDialog(
                modal=True,
                title=ft.Text(f"{section} case rate"),
                content=ft.Column([
                    ft.Text(
                        f"Section {section} applies a different rate in each filer "
                        f"case. For {section}, the tax is payable by the {role_label}.\n"
                        f"Choose the filer case below, then enter the applicable rate "
                        f"from your verified source.",
                        size=12, color="#475569",
                    ),
                    ft.Row([
                        ft.OutlinedButton(
                            label,
                            on_click=lambda _, c=label, s=status: choose(c, s),
                        )
                        for label, status in cases
                    ], wrap=True),
                    ft.Text(
                        "Late filer (previous years) usually needs an "
                        "effective date range covering the earlier years; set "
                        "it after saving via Edit.",
                        size=12, color="#475569",
                    ),
                ], tight=True, spacing=10),
                actions=[ft.TextButton("Cancel", on_click=lambda _: page.pop_dialog())],
            ))

        def use_custom_preset(_=None):
            """Preset for a fee the user names themselves.

            Clears the name so the typed value is always the user's own fee
            name; everything else matches the percentage presets. The name
            field is required by the service, so saving without one is
            caught there.
            """
            rule_name.value = ""
            category.value = "custom_fee"
            method.value = "percentage"
            rate.value = ""
            conditions.value = '{"base":"transferred_value","payer":"buyer"}'
            update_rule_method()

        def use_fixed_preset(_=None):
            rule_name.value = "Service charge"
            category.value = "service_charge"
            method.value = "fixed_amount"
            fixed.value = ""
            conditions.value = '{"payer":"other"}'
            update_rule_method()

        def save(_):
            try:
                values = {
                    "name": rule_name.value, "category": category.value,
                    "jurisdiction": rule_jurisdiction.value,
                    "calculation_method": method.value,
                    "rate_value": (rate.value or "").strip() or None,
                    "fixed_amount": (fixed.value or "").strip() or None,
                    "percentage_threshold": (threshold.value or "").strip() or None,
                    "rate_unit": rate_unit_field.value,
                    # The compact dialog hides schedule metadata. Existing
                    # metadata is preserved on edit; new rules start today
                    # and remain unverified until reviewed elsewhere.
                    "effective_from": existing.get("effective_from") or date.today(),
                    "effective_to": existing.get("effective_to"),
                    "minimum_amount": (minimum.value or "").strip() or None,
                    "maximum_amount": (maximum.value or "").strip() or None,
                    "eligibility_conditions_json": conditions.value,
                    "source_reference": existing.get("source_reference"),
                    "source_date": existing.get("source_date"),
                    "verification_status": existing.get("verification_status") or "unverified",
                    "verified_by": existing.get("verified_by"),
                }
                if existing.get("id"):
                    update_tax_fee_rule(rule_id=existing["id"], **values)
                else:
                    create_tax_fee_rule(**values)
                refresh_rules()
                rule_status.value = "Rule saved. Applicable rules are included in the next deed cost calculation."
                if existing.get("id"):
                    page.pop_dialog()
                else:
                    # Keep Add open for consecutive entries, but reset every
                    # input so the next rule cannot inherit stale values.
                    rule_name.value = ""
                    category.value = ""
                    rule_jurisdiction.value = "Punjab"
                    method.value = "percentage"
                    rate.value = ""
                    fixed.value = ""
                    threshold.value = ""
                    rate_unit_field.value = ""
                    minimum.value = ""
                    maximum.value = ""
                    conditions.value = '{"base":"transferred_value","payer":"buyer"}'
                    update_rule_method()
                notification = ft.SnackBar(
                    content=ft.Text("Tax / fee rule saved successfully."), duration=2500
                )
                if hasattr(page, "show_dialog"):
                    page.show_dialog(notification)
                else:
                    notification.open = True
                    page.snack_bar = notification
                page.update()
            except (TaxRuleError, ValueError, TypeError) as exc:
                error.value = str(exc)
                page.update()
            except Exception as exc:
                # Keep storage/runtime errors inside the dialog so a failed
                # write does not escape the Flet click handler and crash UI.
                error.value = f"Could not save rule: {exc}"
                page.update()

        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("Edit tax/fee rule" if existing.get("id") else "Add tax/fee rule"),
            content=ft.Column([
                ft.Text("Start with a common rule type. Enter the applicable percentage or amount from your verified source. 236C and 236K first ask for the filer case (active, non-active, or late filer) because each case has its own rate.", size=12, color="#475569"),
                ft.Row([
                    ft.OutlinedButton("Stamp fee", on_click=lambda _: use_percentage_preset("Stamp fee")),
                    ft.OutlinedButton("Mutation fee", on_click=lambda _: use_percentage_preset("Mutation fee")),
                    ft.OutlinedButton("Committee fee", on_click=lambda _: use_percentage_preset("Committee fee")),
                    ft.OutlinedButton("236C", on_click=lambda _: open_236_case_chooser("236C")),
                    ft.OutlinedButton("236K", on_click=lambda _: open_236_case_chooser("236K")),
                    ft.OutlinedButton("PLRA fee", on_click=lambda _: use_percentage_preset("PLRA fee")),
                    ft.OutlinedButton("Commission fee", on_click=lambda _: use_percentage_preset("Commission fee")),
                    ft.OutlinedButton("Custom fee", on_click=use_custom_preset),
                    ft.OutlinedButton("Fixed service charge", on_click=use_fixed_preset),
                ], wrap=True),
                ft.ResponsiveRow([
                    ft.Container(rule_name, col={"xs": 12, "md": 4}),
                    ft.Container(category, col={"xs": 12, "md": 4}),
                    ft.Container(rule_jurisdiction, col={"xs": 12, "md": 4}),
                    ft.Container(method, col={"xs": 12, "md": 4}),
                    ft.Container(rate, col={"xs": 12, "md": 4}),
                    ft.Container(fixed, col={"xs": 12, "md": 4}),
                    ft.Container(threshold, col={"xs": 12, "md": 4}),
                    ft.Container(rate_unit_field, col={"xs": 12, "md": 4}),
                    ft.Container(minimum, col={"xs": 12, "md": 4}),
                    ft.Container(maximum, col={"xs": 12, "md": 4}),
                ], run_spacing=8),
                conditions, error,
            ], tight=True, scroll=ft.ScrollMode.AUTO, width=720, height=540, spacing=10),
            actions=[
                ft.TextButton("Cancel", on_click=lambda _: page.pop_dialog()),
                *([
                    ft.TextButton(
                        "Remove rule",
                        style=ft.ButtonStyle(color="#B91C1C"),
                        on_click=lambda _, record=existing: [page.pop_dialog(), confirm_remove_rule(record)],
                    )
                ] if existing.get("id") else []),
                ft.Button("Save rule", on_click=save),
            ],
        ))

    rule_page = ft.Column([
        ft.Text("Tax and fee rules", size=20, weight=ft.FontWeight.BOLD),
        ft.Text("Enter rules from their official source. Verification requires a source and reviewer. The calculator reports estimates and missing eligibility inputs.", size=12, color="#9A3412"),
        ft.Row([ft.Button("Add rule", on_click=lambda _: open_rule_dialog()), ft.TextButton("Refresh", on_click=lambda _: refresh_rules()), rule_status], wrap=True),
        rules_list,
        rules_empty_state,
        ft.Divider(),
        ft.Row([
            ft.Text("Custom registry formulas", size=18, weight=ft.FontWeight.BOLD, expand=True),
            ft.OutlinedButton("Add formula", on_click=lambda _: open_formula_dialog()),
            ft.OutlinedButton("Manage custom fields", on_click=open_custom_fields_dialog),
        ]),
        ft.Text("Formula fields are numeric inputs shown on Calculate. Formulas are dated and default to unverified. Example: area * 2.5% + 2000.", size=12, color="#64748B"),
        custom_fields_status,
        formula_list,
    ], expand=True, spacing=10, scroll=ft.ScrollMode.AUTO)

    # Keep existing DC/FBR maintenance available in the same page. Each tab
    # remains an independently built section with its own service callbacks.
    # Keep the schedule tab truly lazy: its first query starts only when the
    # user selects it, so valuation-page navigation has no competing I/O.
    rate_page_loader: dict[str, Any] = {}
    rates_page = __import__("ui.pages.dc_rates", fromlist=["build_dc_rates_page"]).build_dc_rates_page(
        page,
        defer_initial_load=True,
        on_load_ready=lambda loader: rate_page_loader.update(load=loader),
    )
    rates_load_started = False

    def on_valuation_tab_change(event):
        nonlocal rates_load_started
        rates_tab_selected = event.control.selected_index == 2
        valuation_heading.visible = not rates_tab_selected
        valuation_description.visible = not rates_tab_selected
        if on_rates_tab_change is not None:
            on_rates_tab_change(rates_tab_selected)
        if rates_tab_selected and not rates_load_started:
            rates_load_started = True
            loader = rate_page_loader.get("load")
            if loader is not None:
                page.run_task(loader)
    tabs = ft.Tabs(
        content=ft.Column([
            ft.TabBar(tabs=[ft.Tab(label="Calculate"), ft.Tab(label="Tax / fee rules"), ft.Tab(label="DC / FBR rates")]),
            ft.TabBarView(controls=[estimate_form, rule_page, rates_page], expand=True),
        ], spacing=0, expand=True),
        length=3,
        selected_index=0,
        on_change=on_valuation_tab_change,
        expand=True,
    )
    valuation_heading = ft.Text("Property Valuation & Taxes", size=25, weight=ft.FontWeight.BOLD)
    valuation_description = ft.Text(
        "Calculate an explainable estimate, manage configurable charges, and maintain valuation schedules.",
        color="#64748B",
    )
    root = ft.Column([
        valuation_heading,
        valuation_description,
        tabs,
    ], spacing=8, expand=True)

    async def load_initial_data():
        """Fetch page data on worker threads so navigation stays responsive."""
        try:
            loaded_clients, estimates, rules, formulas, custom_fields = await asyncio.gather(
                asyncio.to_thread(list_client_choices),
                asyncio.to_thread(list_estimates, limit=100),
                asyncio.to_thread(list_tax_fee_rules),
                asyncio.to_thread(list_registry_fee_formulas),
                asyncio.to_thread(list_registry_fee_custom_fields),
            )
            clients.extend(loaded_clients)
            client.options = [ft.dropdown.Option("none", "No client linked"), *[
                ft.dropdown.Option(str(item["id"]), item["name"]) for item in clients
            ]]
            refresh_history(estimates)
            refresh_rules(rules)
            refresh_formulas_with_records(formulas)
            render_custom_field_inputs(custom_fields)
            # Populate schedule-backed suggestions immediately on page open.
            # Rate matching remains empty until the user provides enough detail.
            refresh_location_hints()
            refresh_rate_matches()
            page.update()
        except Exception as exc:
            status.value = f"Could not load valuation data: {exc}"
            page.update()

    def refresh_formulas_with_records(formulas):
        """Render the already fetched formula rows without another DB read."""
        formula_list.controls.clear()
        if not formulas:
            formula_list.controls.append(ft.Text("No registry fee formulas saved.", color="#64748B"))
        for formula in formulas:
            formula_list.controls.append(ft.Row([
                ft.Column([
                    ft.Text(f"{formula['name']} · {formula['category']}", weight=ft.FontWeight.BOLD),
                    ft.Text(f"{formula['formula_expression']} · {formula['verification_status']}", size=12),
                ], expand=True, spacing=2),
                ft.TextButton("Remove", on_click=lambda _, row=formula: confirm_remove_formula(row)),
                ft.TextButton("Edit", on_click=lambda _, row=formula: open_formula_dialog(row)),
            ]))

    if hasattr(page, "run_task"):
        page.run_task(load_initial_data)
    return root
