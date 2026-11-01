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
from services.dc_rate_service import list_locations, list_dc_rates
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
    # Populate these lists asynchronously after the lightweight control tree is
    # attached; event handlers continue to use the same shared choice cache.
    locations: list[dict[str, Any]] = []
    clients: list[dict[str, Any]] = []

    status = ft.Text("", size=12, color="#475569")
    form_error = ft.Text("", color="#B91C1C", size=12)
    result_panel = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO, expand=True)
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

    location = ft.Dropdown(label="Resolved location", visible=False, options=[
        ft.dropdown.Option(str(item["id"]), " · ".join(str(v) for v in (
            item.get("name"), item.get("district"), item.get("tehsil"), item.get("land_type")
        ) if v)) for item in locations
    ])
    client = ft.Dropdown(label="Client (optional)", width=250, value="none", options=[
        ft.dropdown.Option("none", "No client linked"),
        *[ft.dropdown.Option(str(item["id"]), item["name"]) for item in clients],
    ])
    # Effective date is fixed to today's date for the streamlined workflow.
    transaction_date = date.today()
    property_profile = ft.RadioGroup(value="rural", content=ft.Row([
        ft.Radio(value="rural", label="Rural"), ft.Radio(value="urban", label="Urban"),
    ], spacing=16))
    district = ft.AutoComplete(width=220, suggestions_max_height=220)
    tehsil = ft.AutoComplete(width=220, suggestions_max_height=220)
    mauza = ft.AutoComplete(width=240, suggestions_max_height=220)
    town = ft.AutoComplete(width=220, suggestions_max_height=220)
    revenue_circle = ft.AutoComplete(width=220, suggestions_max_height=220)
    property_area = ft.AutoComplete(width=220, suggestions_max_height=220)
    schedule_location = ft.AutoComplete(width=240, suggestions_max_height=220)
    square_no = ft.TextField(label="Square no.", width=150)
    kila_no = ft.TextField(label="Kila no.", width=150)
    khasra_no = ft.TextField(label="Khasra no. (optional)", width=180)
    classification = ft.Dropdown(label="Classification (optional)", width=220, options=[
        ft.dropdown.Option("none", "All / not specified"),
    ])
    floor = ft.TextField(label="Floor (if applicable)", width=180)
    dc_rate_unit = ft.Dropdown(label="DC rate unit (select explicitly)", width=220, options=[
        ft.dropdown.Option(unit, RATE_UNITS[unit]) for unit in DC_RATE_UNITS
    ])
    fbr_rate_unit = ft.Dropdown(label="FBR rate unit (if available)", width=220, options=[
        ft.dropdown.Option(unit, RATE_UNITS[unit]) for unit in DC_RATE_UNITS
    ])
    area_value = ft.TextField(label="Area (K-M-F)", width=190, hint_text="For example: 2-5-0")
    area_unit = ft.Dropdown(label="Area input unit", value="kmf", visible=False, options=[
        ft.dropdown.Option("kmf", "K-M-F")
    ])
    dc_rate_choice = ft.Dropdown(label="Matched DC rate", width=360, options=[])
    fbr_rate_choice = ft.Dropdown(label="Matched FBR rate (editable)", width=360, options=[])
    location_match = ft.Dropdown(label="Matching schedule location", width=360, options=[], visible=False)
    rate_match_status = ft.Text("Enter schedule fields to find matching DC and FBR rates.", size=12, color="#475569")
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

    def _text_value(control) -> str:
        return (control.value or "").strip()

    def _value_matches(candidate: Any, query: str) -> bool:
        return not query or query.casefold() in str(candidate or "").casefold()

    def _reference_matches(candidate: Any, query: str) -> bool:
        """Match cadastral identifiers as whole tokens (2 must not match 12)."""
        if not query:
            return True
        tokens = {
            part.strip().casefold()
            for part in str(candidate or "").replace(";", ",").split(",")
            if part.strip()
        }
        return query.casefold() in tokens

    def _location_field_matches(item: dict[str, Any], key: str, query: str) -> bool:
        if key == "locality":
            # Schedule imports may put a location in either the named
            # location or its finer locality/detail column.
            return not query or any(
                _value_matches(item.get(field), query)
                for field in ("locality", "name")
            )
        if key in {"square_no", "kila_no", "khasra_no"}:
            return _reference_matches(item.get(key), query)
        return _value_matches(item.get(key), query)

    def _active_location_fields():
        if property_profile.value == "rural":
            return [("district", district), ("tehsil", tehsil), ("mauza", mauza),
                    ("locality", schedule_location), ("square_no", square_no), ("kila_no", kila_no)]
        return [("tehsil", tehsil), ("committee_town", town), ("revenue_circle", revenue_circle),
                ("property_area", property_area), ("locality", schedule_location),
                ("square_no", square_no), ("kila_no", kila_no), ("khasra_no", khasra_no)]

    def matching_locations():
        active_fields = _active_location_fields()
        matches = []
        for item in locations:
            if str(item.get("land_type") or "urban").casefold() != property_profile.value:
                continue
            if all(_location_field_matches(item, key, _text_value(control))
                   for key, control in active_fields):
                matches.append(item)
        return matches

    def refresh_location_hints(_=None):
        """Filter type-ahead suggestions from schedule-backed location values."""
        active = _active_location_fields()
        for key, control in active:
            candidates = []
            for item in locations:
                if str(item.get("land_type") or "urban").casefold() != property_profile.value:
                    continue
                if any(other_key != key and _text_value(other_control)
                       and not _location_field_matches(item, other_key, _text_value(other_control))
                       for other_key, other_control in active):
                    continue
                raw = item.get(key)
                values = [part.strip() for part in str(raw or "").replace(";", ",").split(",") if part.strip()]
                if key == "locality":
                    values.extend((str(item.get("name") or "").strip(),))
                query = _text_value(control)
                candidates.extend(value for value in values if value and _value_matches(value, query))
            control.suggestions = [ft.AutoCompleteSuggestion(key=value, value=value)
                                   for value in sorted(set(candidates), key=str.casefold)[:100]]

    def _rate_label(rate: dict[str, Any]) -> str:
        place = rate.get("mauza") or rate.get("property_area") or rate.get("committee_town") or rate.get("location_name") or "Location"
        unit = RATE_UNITS.get(rate.get("rate_unit"), rate.get("rate_unit") or "Unit")
        return f"Rs. {rate.get('rate_value')} / {unit.removeprefix('Per ')} · {rate.get('classification') or rate.get('property_type')} · {place}"

    def refresh_rate_matches(_=None):
        """Fetch active schedule rows and preselect unique unit/rate matches."""
        refresh_location_hints()
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
            rate_match_status.value = "No matching schedule location yet. Complete the location fields."
            page.update()
            return

        all_rates = [row for item in candidates for schedule in ("dc", "fbr")
                     for row in list_dc_rates(location_id=int(item["id"]), schedule_type=schedule,
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
            if unit_control.value not in units:
                unit_control.value = units[0] if len(units) == 1 else None
            rows = [row for row in rows if not unit_control.value or row["rate_unit"] == unit_control.value]
            rate_records[schedule] = rows
            rate_control.options = [ft.dropdown.Option(str(row["id"]), _rate_label(row)) for row in rows]
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
            location.value = str(selected_dc["location_id"])
            rate_match_status.value = f"DC rate found: {_rate_label(selected_dc)}"
        else:
            rate_match_status.value = f"{len(rate_records['dc'])} matching DC rate(s); choose a unit or rate if needed."
        page.update()

    for control in (district, tehsil, mauza, town, revenue_circle, property_area,
                    schedule_location, square_no, kila_no, khasra_no, classification,
                    dc_rate_unit, fbr_rate_unit, location_match):
        control.on_change = refresh_rate_matches
        if isinstance(control, ft.AutoComplete):
            control.on_select = refresh_rate_matches
    property_profile.on_change = refresh_rate_matches
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
                    ], wrap=True),
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
            selected_location = next(item for item in locations
                                     if int(item["id"]) == int(dc_rate["location_id"]))
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
                    ft.TextButton("Edit", on_click=lambda _, row=formula: open_formula_dialog(row)),
                ], wrap=True))
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
            actions=[ft.TextButton("Cancel", on_click=lambda _: page.pop_dialog()),
                     ft.FilledButton("Save formula", on_click=save_formula)],
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
    # narrow the cached schedule locations, the rate selectors expose the
    # matching DC/FBR rows, and Calculate passes those exact rows to the
    # valuation service rather than asking it to guess from a partial address.
    rural_fields_panel = ft.ResponsiveRow([
        ft.Container(ft.Column([ft.Text("District"), district]), col={"xs": 12, "md": 4}),
        ft.Container(ft.Column([ft.Text("Mauza (type to search schedules)"), mauza]), col={"xs": 12, "md": 4}),
    ], run_spacing=5)
    urban_fields_panel = ft.ResponsiveRow([
        ft.Container(ft.Column([ft.Text("Town / city"), town]), col={"xs": 12, "md": 4}),
        ft.Container(ft.Column([ft.Text("Revenue circle"), revenue_circle]), col={"xs": 12, "md": 4}),
        ft.Container(ft.Column([ft.Text("Property area"), property_area]), col={"xs": 12, "md": 4}),
        ft.Container(classification, col={"xs": 12, "md": 4}),
        ft.Container(khasra_no, col={"xs": 12, "md": 4}),
    ], run_spacing=5, visible=False)
    advanced_inputs = ft.ResponsiveRow([
        ft.Container(client, col={"xs": 12, "md": 6}),
        ft.Container(registry_written_value, col={"xs": 12, "md": 6}),
        ft.Container(constructed, col={"xs": 12, "md": 4}),
        ft.Container(building_material_value, col={"xs": 12, "md": 4}),
        ft.Container(transaction_type, col={"xs": 12, "md": 4}),
        ft.Container(buyer_filer, col={"xs": 12, "md": 4}),
        ft.Container(seller_filer, col={"xs": 12, "md": 4}),
        ft.Container(floor, col={"xs": 12, "md": 4}),
        ft.Container(marla_setting, col={"xs": 12, "md": 4}),
    ], run_spacing=5, visible=False)
    advanced_button = ft.TextButton("Show advanced calculation fields")

    def toggle_advanced_inputs(_=None):
        advanced_inputs.visible = not advanced_inputs.visible
        advanced_button.content = "Hide advanced calculation fields" if advanced_inputs.visible else "Show advanced calculation fields"
        page.update()

    def switch_property_profile(_=None):
        is_rural = property_profile.value == "rural"
        rural_fields_panel.visible = is_rural
        urban_fields_panel.visible = not is_rural
        fbr_rate_choice.visible = not is_rural
        fbr_rate_unit.visible = not is_rural
        refresh_rate_matches()

    advanced_button.on_click = toggle_advanced_inputs
    property_profile.on_change = switch_property_profile
    switch_property_profile()

    estimate_form = ft.Column([
        ft.Text("Estimate inputs", size=19, weight=ft.FontWeight.BOLD),
        ft.Text("Choose Rural or Urban, enter schedule-backed location details, then enter the area in K-M-F format. Matching DC and FBR rates are fetched from saved schedules.", size=12, color="#9A3412"),
        ft.ResponsiveRow([
            ft.Container(property_profile, col=12),
            ft.Container(rural_fields_panel, col=12),
            ft.Container(urban_fields_panel, col=12),
            ft.Container(ft.ResponsiveRow([
                ft.Container(ft.Column([ft.Text("Tehsil"), tehsil]), col={"xs": 12, "md": 4}),
                ft.Container(ft.Column([ft.Text("Location"), schedule_location]), col={"xs": 12, "md": 4}),
                ft.Container(square_no, col={"xs": 12, "md": 2}),
                ft.Container(kila_no, col={"xs": 12, "md": 2}),
            ], run_spacing=5), col=12),
            ft.Container(location_match, col={"xs": 12, "md": 6}),
            ft.Container(dc_rate_unit, col={"xs": 12, "md": 4}),
            ft.Container(dc_rate_choice, col={"xs": 12, "md": 8}),
            ft.Container(fbr_rate_unit, col={"xs": 12, "md": 4}),
            ft.Container(fbr_rate_choice, col={"xs": 12, "md": 8}),
            ft.Container(rate_match_status, col=12),
            ft.Container(area_value, col={"xs": 12, "md": 4}),
        ], run_spacing=5),
        advanced_button,
        advanced_inputs,
        custom_field_controls,
        ft.Row([ft.Button("Calculate estimate", on_click=calculate),
                ft.OutlinedButton("Save estimate", on_click=save_current)], wrap=True, spacing=8),
        form_error,
        status,
        ft.Container(result_panel, padding=14, bgcolor="#FFFFFF", border=ft.Border.all(1, "#E2E8F0"), border_radius=10, height=360),
        ft.Text("Saved estimate history", size=18, weight=ft.FontWeight.BOLD),
        ft.Row([
            ft.OutlinedButton("Export history CSV", on_click=history_export_handler("csv")),
            ft.OutlinedButton("Export history Excel", on_click=history_export_handler("xlsx")),
            ft.OutlinedButton("Export history PDF", on_click=history_export_handler("pdf")),
        ], wrap=True, spacing=6),
        history_list,
    ], spacing=8, scroll=ft.ScrollMode.AUTO, expand=True)
    history_list.controls.append(ft.Text("Loading estimate history…", color="#64748B"))
    formula_list.controls.append(ft.Text("Loading saved formulas…", color="#64748B"))

    rule_status = ft.Text("", size=12, color="#475569")
    rules_list = ft.ListView(expand=True, spacing=6)

    def refresh_rules(rules=None):
        rules_list.controls.clear()
        try:
            rules = list_tax_fee_rules() if rules is None else rules
            if not rules:
                rules_list.controls.append(ft.Text("No tax or fee rules yet. Add rules from verified sources or mark manually entered rules unverified.", color="#64748B"))
            for rule in rules:
                details = f"{rule['jurisdiction']} · {rule['category']} · {rule['calculation_method']} · {rule.get('verification_status', 'unverified').upper()}"
                if rule["calculation_method"] == "fixed_amount":
                    amount = _money(rule.get("fixed_amount"), rule.get("currency_code", "PKR"))
                else:
                    amount = f"{rule.get('rate_value')} {rule.get('rate_unit') or '%'}"
                rules_list.controls.append(ft.Container(
                    padding=10, bgcolor="#FFFFFF", border=ft.Border.all(1, "#E2E8F0"), border_radius=8,
                    content=ft.Row([
                        ft.Column([ft.Text(rule["name"], weight=ft.FontWeight.BOLD),
                                   ft.Text(details, size=12),
                                   ft.Text(f"{amount} · {rule['effective_from']} to {rule.get('effective_to') or 'open'}", size=12)],
                                  spacing=3, expand=True),
                        ft.TextButton("Edit", on_click=lambda _, record=rule: open_rule_dialog(record)),
                    ], wrap=True),
                ))
        except Exception as exc:
            rules_list.controls.append(ft.Text(f"Could not load rules: {exc}", color="#B91C1C"))

    def open_rule_dialog(existing: dict[str, Any] | None = None):
        existing = existing or {}
        rule_name = ft.TextField(label="Rule name", value=existing.get("name", ""), width=260)
        category = ft.TextField(label="Category", value=existing.get("category", ""), width=220)
        rule_jurisdiction = ft.TextField(label="Jurisdiction", value=existing.get("jurisdiction", "Punjab"), width=220)
        method = ft.Dropdown(label="Calculation method", value=existing.get("calculation_method", "percentage"), width=220, options=[
            ft.dropdown.Option("percentage", "Percentage"), ft.dropdown.Option("fixed_amount", "Fixed amount"), ft.dropdown.Option("per_unit", "Per unit")
        ])
        rate = ft.TextField(label="Rate / percent", value=str(existing.get("rate_value") or ""), width=180)
        fixed = ft.TextField(label="Fixed amount", value=str(existing.get("fixed_amount") or ""), width=180)
        rate_unit_field = ft.TextField(label="Rate unit (required for per-unit)", value=existing.get("rate_unit") or "", width=220)
        effective_from = ft.TextField(label="Effective from", value=str(existing.get("effective_from") or date.today().isoformat()), width=180)
        effective_to = ft.TextField(label="Effective to (optional)", value=str(existing.get("effective_to") or ""), width=180)
        minimum = ft.TextField(label="Minimum charge", value=str(existing.get("minimum_amount") or ""), width=180)
        maximum = ft.TextField(label="Maximum charge", value=str(existing.get("maximum_amount") or ""), width=180)
        source = ft.TextField(label="Source reference", value=existing.get("source_reference") or "", width=380)
        source_date = ft.TextField(label="Source date", value=str(existing.get("source_date") or ""), width=180)
        verification = ft.Dropdown(label="Verification", value=existing.get("verification_status", "unverified"), width=180, options=[
            ft.dropdown.Option("unverified", "Unverified"), ft.dropdown.Option("verified", "Verified")
        ])
        verified_by = ft.TextField(label="Verified by", value=existing.get("verified_by") or "", width=220)
        conditions = ft.TextField(
            label="Eligibility / payer / base JSON", multiline=True,
            value=existing.get("eligibility_conditions_json") or '{"base":"transferred_value","payer":"buyer"}',
            width=560, min_lines=2, max_lines=5,
            hint_text='Percentage bases include property_value, transferred_value, registry_written_value, building_material_value, dc_value, and fbr_value.',
        )
        error = ft.Text("", color="#B91C1C", size=12)

        def update_rule_method(_=None):
            """Keep method-specific fields visible and clear stale values."""
            rate.visible = method.value != "fixed_amount"
            fixed.visible = method.value == "fixed_amount"
            rate_unit_field.visible = method.value == "per_unit"
            if method.value == "fixed_amount":
                rate.value = ""
            else:
                fixed.value = ""
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
                    "rate_unit": rate_unit_field.value,
                    "effective_from": effective_from.value,
                    "effective_to": effective_to.value,
                    "minimum_amount": (minimum.value or "").strip() or None,
                    "maximum_amount": (maximum.value or "").strip() or None,
                    "eligibility_conditions_json": conditions.value,
                    "source_reference": source.value,
                    "source_date": (source_date.value or "").strip() or None,
                    "verification_status": verification.value,
                    "verified_by": verified_by.value,
                }
                if existing.get("id"):
                    update_tax_fee_rule(rule_id=existing["id"], **values)
                else:
                    create_tax_fee_rule(**values)
                page.pop_dialog()
                refresh_rules()
                rule_status.value = "Rule saved. It applies according to its dates and eligibility configuration."
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
                ft.Text("Start with a common rule type. Enter the applicable percentage or amount from your verified source.", size=12, color="#475569"),
                ft.Row([
                    ft.OutlinedButton("Stamp fee", on_click=lambda _: use_percentage_preset("Stamp fee")),
                    ft.OutlinedButton("Committee fee", on_click=lambda _: use_percentage_preset("Committee fee")),
                    ft.OutlinedButton("236C", on_click=lambda _: use_percentage_preset("236C")),
                    ft.OutlinedButton("236K", on_click=lambda _: use_percentage_preset("236K")),
                    ft.OutlinedButton("Fixed service charge", on_click=use_fixed_preset),
                ], wrap=True),
                ft.Row([rule_name, category, rule_jurisdiction], wrap=True),
                ft.Row([method, rate, fixed, rate_unit_field], wrap=True),
                ft.Row([effective_from, effective_to, minimum, maximum], wrap=True),
                ft.Row([source, source_date, verification, verified_by], wrap=True),
                conditions, error,
            ], tight=True, scroll=ft.ScrollMode.AUTO, width=700, height=570, spacing=8),
            actions=[ft.TextButton("Cancel", on_click=lambda _: page.pop_dialog()), ft.Button("Save rule", on_click=save)],
        ))

    rule_page = ft.Column([
        ft.Text("Tax and fee rules", size=20, weight=ft.FontWeight.BOLD),
        ft.Text("Enter rules from their official source. Verification requires a source and reviewer. The calculator reports estimates and missing eligibility inputs.", size=12, color="#9A3412"),
        ft.Row([ft.Button("Add rule", on_click=lambda _: open_rule_dialog()), ft.TextButton("Refresh", on_click=lambda _: refresh_rules()), rule_status], wrap=True),
        rules_list,
        ft.Divider(),
        ft.Row([
            ft.Text("Custom registry formulas", size=18, weight=ft.FontWeight.BOLD, expand=True),
            ft.OutlinedButton("Add formula", on_click=lambda _: open_formula_dialog()),
            ft.OutlinedButton("Manage custom fields", on_click=open_custom_fields_dialog),
        ], wrap=True),
        ft.Text("Formula fields are numeric inputs shown on Calculate. Formulas are dated and default to unverified. Example: area * 2.5% + 2000.", size=12, color="#64748B"),
        custom_fields_status,
        formula_list,
    ], expand=True)
    rules_list.controls.append(ft.Text("Loading tax and fee rules…", color="#64748B"))

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
            loaded_locations, loaded_clients, estimates, rules, formulas, custom_fields = await asyncio.gather(
                asyncio.to_thread(list_locations),
                asyncio.to_thread(list_client_choices),
                asyncio.to_thread(list_estimates, limit=100),
                asyncio.to_thread(list_tax_fee_rules),
                asyncio.to_thread(list_registry_fee_formulas),
                asyncio.to_thread(list_registry_fee_custom_fields),
            )
            locations.extend(loaded_locations)
            clients.extend(loaded_clients)
            location.options = [ft.dropdown.Option(str(item["id"]), " · ".join(
                str(value) for value in (item.get("name"), item.get("district"), item.get("tehsil"), item.get("land_type")) if value
            )) for item in locations]
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
                ft.TextButton("Edit", on_click=lambda _, row=formula: open_formula_dialog(row)),
            ], wrap=True))

    if hasattr(page, "run_task"):
        page.run_task(load_initial_data)
    return root
