"""Valuation workflow, saved-estimate history, and tax-rule management UI.

The calculator calls valuation/tax services; those services query persisted
schedule/rule models. Saving stores input/output snapshots through the estimate
service so historical calculations remain reviewable when live rules change.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from typing import Any

import flet as ft

from core.area_engine import AreaSettings
from core.models import DC_RATE_UNITS
from services.dc_rate_service import list_locations
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


def build_valuation_page(page: ft.Page) -> ft.Control:
    """Build the estimate workflow plus the existing rates and tax-rule pages."""
    try:
        locations = list_locations()
        clients = list_client_choices()
    except Exception as exc:
        return ft.Column([
            ft.Text("Property Valuation & Taxes", size=24, weight=ft.FontWeight.BOLD),
            ft.Text(f"Could not load valuation choices: {exc}", color="#B91C1C"),
        ])

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

    location = ft.Dropdown(label="Property location (required)", width=300, options=[
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
    mauza = ft.TextField(label="Mauza (required)", width=230, read_only=True)
    square_no = ft.TextField(label="Square no.", width=150)
    kila_no = ft.TextField(label="Kila no.", width=150)
    khasra_no = ft.TextField(label="Khasra no. (optional)", width=180)
    khasra_no.visible = False
    classification = ft.Dropdown(label="Property classification", width=220, options=[
        ft.dropdown.Option(value, value) for value in ("Agricultural", "Residential", "Commercial")
    ])
    floor = ft.TextField(label="Floor (if applicable)", width=180)
    dc_rate_unit = ft.Dropdown(label="DC rate unit (select explicitly)", width=220, options=[
        ft.dropdown.Option(unit, RATE_UNITS[unit]) for unit in DC_RATE_UNITS
    ])
    fbr_rate_unit = ft.Dropdown(label="FBR rate unit (if available)", width=220, options=[
        ft.dropdown.Option(unit, RATE_UNITS[unit]) for unit in DC_RATE_UNITS
    ])
    area_value = ft.TextField(label="Property area", width=190, hint_text="e.g. 2-5-0")
    area_unit = ft.Dropdown(label="Area input unit", value="kmf", width=190, options=[
        ft.dropdown.Option(key, label) for key, label in AREA_UNITS.items()
    ])
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
    def set_location_details(_=None):
        """Keep the displayed Mauza and formula scope tied to the selected profile."""
        selected = next((item for item in locations if str(item["id"]) == location.value), None)
        mauza.value = (selected or {}).get("mauza") or (selected or {}).get("name") or ""
        square_no.value = (selected or {}).get("square_no") or ""
        kila_no.value = (selected or {}).get("kila_no") or ""
        khasra_no.value = (selected or {}).get("khasra_no") or ""
        khasra_no.visible = str((selected or {}).get("land_type") or "urban").casefold() == "urban"
        page.update()

    location.on_change = set_location_details

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

    def refresh_history():
        history_list.controls.clear()
        try:
            records = list_estimates()
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
            if not location.value:
                raise ValuationError("Select a property location.")
            if not classification.value:
                raise ValuationError("Select a property classification.")
            selected_location = next(item for item in locations if str(item["id"]) == location.value)
            selected_mauza = (selected_location.get("mauza") or selected_location.get("name") or "").strip()
            if not selected_mauza:
                raise ValuationError("The selected location must have a Mauza name.")
            if str(selected_location.get("land_type") or "urban").casefold() != "urban":
                khasra_no.value = ""
            settings = AreaSettings(square_feet_per_marla=int(marla_setting.value))
            material_value = Decimal((building_material_value.value or "0").strip()) if constructed.value else Decimal("0")
            if material_value < 0:
                raise ValuationError("Building material value cannot be negative.")
            written_value = (registry_written_value.value or "").strip() or None
            valuation = calculate_dual_valuation(
                location_id=int(location.value),
                property_type=classification.value,
                classification=classification.value,
                floor=(floor.value or "").strip() or None,
                transaction_date=transaction_date,
                area_value=area_value.value,
                area_unit=area_unit.value,
                dc_rate_unit=dc_rate_unit.value or None,
                fbr_rate_unit=fbr_rate_unit.value or None,
                registry_written_value=written_value,
                settings=settings,
            )
            area_marla = Decimal(str(valuation["area_square_feet"])) / Decimal(settings.square_feet_per_marla)
            area_kanal = area_marla / Decimal(20)
            area_acre = area_kanal / Decimal(8)
            context = {
                "transaction_type": transaction_type.value,
                "property_type": classification.value,
                "classification": classification.value,
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
            rules = list_tax_fee_rules()
            # Both values are available to rules; each rule explicitly picks
            # DC or FBR as its calculation base. Registry formulas use the same
            # separated context and save their expression/source snapshots.
            charges = calculate_charges(rules=rules, transaction_date=transaction_date,
                                        context=context, jurisdiction=["Punjab", "Punjab, Pakistan"])
            formulas_result = calculate_registry_fee_formulas(
                formulas=list_registry_fee_formulas(), effective_on=transaction_date,
                context=context, jurisdictions=["Punjab", "Punjab, Pakistan"],
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
                "location_id": int(location.value),
                "client_id": int(client.value) if client.value not in (None, "none") else None,
                "transaction_date": transaction_date,
                "transaction_type": transaction_type.value,
                "jurisdiction": "Punjab",
                "mauza": selected_mauza,
                "square_no": (square_no.value or "").strip() or None,
                "kila_no": (kila_no.value or "").strip() or None,
                "khasra_no": (khasra_no.value or "").strip() or None,
                "property_type": classification.value,
                "classification": classification.value,
                "schedule_type": "dc_and_fbr",
                "transaction_date_is_automatic": True,
                "floor": (floor.value or "").strip() or None,
                "area_value": area_value.value,
                "area_unit": area_unit.value,
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
                ft.Text("Allowed values: area (marlas), area_kanal, area_acre, area_square_feet, dc_value, fbr_value, registry_written_value, building_material_value. Use + - * / and parentheses. X and % notation are supported.", size=12, color="#475569"),
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

    estimate_form = ft.Column([
        ft.Text("Estimate inputs", size=19, weight=ft.FontWeight.BOLD),
        ft.Text("Mauza and property location are required. Select a client only when saving. DC and FBR schedules are resolved independently; configured charges select their own DC or FBR base.", size=12, color="#9A3412"),
        ft.ResponsiveRow([
            ft.Container(location, col={"xs": 12, "md": 6}),
            ft.Container(mauza, col={"xs": 12, "md": 6}),
            ft.Container(client, col={"xs": 12, "md": 6}),
            ft.Container(classification, col={"xs": 12, "md": 4}),
            ft.Container(floor, col={"xs": 12, "md": 4}),
            ft.Container(area_value, col={"xs": 12, "md": 4}),
            ft.Container(area_unit, col={"xs": 12, "md": 4}),
            ft.Container(marla_setting, col={"xs": 12, "md": 4}),
            ft.Container(dc_rate_unit, col={"xs": 12, "md": 4}),
            ft.Container(fbr_rate_unit, col={"xs": 12, "md": 4}),
            ft.Container(square_no, col={"xs": 12, "md": 4}),
            ft.Container(kila_no, col={"xs": 12, "md": 4}),
            ft.Container(khasra_no, col={"xs": 12, "md": 4}),
            ft.Container(registry_written_value, col={"xs": 12, "md": 4}),
            ft.Container(constructed, col={"xs": 12, "md": 4}),
            ft.Container(building_material_value, col={"xs": 12, "md": 4}),
            ft.Container(transaction_type, col={"xs": 12, "md": 4}),
            ft.Container(buyer_filer, col={"xs": 12, "md": 4}),
            ft.Container(seller_filer, col={"xs": 12, "md": 4}),
        ], run_spacing=5),
        ft.Row([ft.Button("Calculate estimate", on_click=calculate),
                ft.OutlinedButton("Save estimate", on_click=save_current)], wrap=True, spacing=8),
        form_error,
        status,
        ft.Container(result_panel, padding=14, bgcolor="#FFFFFF", border=ft.Border.all(1, "#E2E8F0"), border_radius=10, height=360),
        ft.Row([
            ft.Text("Saved registry fee formulas", size=18, weight=ft.FontWeight.BOLD, expand=True),
            ft.OutlinedButton("Add formula", on_click=lambda _: open_formula_dialog()),
        ], wrap=True),
        ft.Text("Formulas are retained in the database, dated, and default to unverified. Examples: area * 2.5% + 2000 or (dc_value * 1.5%) + 500.", size=12, color="#64748B"),
        formula_list,
        ft.Text("Saved estimate history", size=18, weight=ft.FontWeight.BOLD),
        ft.Row([
            ft.OutlinedButton("Export history CSV", on_click=history_export_handler("csv")),
            ft.OutlinedButton("Export history Excel", on_click=history_export_handler("xlsx")),
            ft.OutlinedButton("Export history PDF", on_click=history_export_handler("pdf")),
        ], wrap=True, spacing=6),
        history_list,
    ], spacing=8, scroll=ft.ScrollMode.AUTO, expand=True)
    refresh_history()
    refresh_formulas()

    rule_status = ft.Text("", size=12, color="#475569")
    rules_list = ft.ListView(expand=True, spacing=6)

    def refresh_rules():
        rules_list.controls.clear()
        try:
            rules = list_tax_fee_rules()
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
            helper_text='Percentage bases include property_value, transferred_value, registry_written_value, building_material_value, dc_value, and fbr_value. Per-unit charges match the selected valuation rate unit.',
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

        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("Edit tax/fee rule" if existing.get("id") else "Add tax/fee rule"),
            content=ft.Column([
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
    ], expand=True)
    refresh_rules()

    # Keep existing DC/FBR maintenance available in the same page. Each tab
    # remains an independently built section with its own service callbacks.
    rates_page = __import__("ui.pages.dc_rates", fromlist=["build_dc_rates_page"]).build_dc_rates_page(page)
    tabs = ft.Tabs(
        content=ft.Column([
            ft.TabBar(tabs=[ft.Tab(label="Calculate"), ft.Tab(label="Tax / fee rules"), ft.Tab(label="DC / FBR rates")]),
            ft.TabBarView(controls=[estimate_form, rule_page, rates_page], expand=True),
        ], spacing=0, expand=True),
        length=3,
        selected_index=0,
        expand=True,
    )
    return ft.Column([
        ft.Text("Property Valuation & Taxes", size=25, weight=ft.FontWeight.BOLD),
        ft.Text("Calculate an explainable estimate, manage configurable charges, and maintain valuation schedules.", color="#64748B"),
        tabs,
    ], spacing=8, expand=True)
