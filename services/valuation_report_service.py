"""CSV, Excel, and PDF reports for saved valuation estimates.

Version history: v0.2.26 introduced configurable PDF sections/columns and a
task-number filename. v0.2.27 added logging but left estimate reporting
unchanged. v0.2.28 adds durable identity rows (task no, rendered name,
waseeka, registry area, save time), preference-aware filenames, one-file batch
PDFs, and ZIP archives of individual estimate PDFs for selected criteria.
"""

from __future__ import annotations

import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
import zipfile
from xml.sax.saxutils import escape

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from services.settings_service import (
    DEFAULT_VALUATION_REPORT_OPTIONS,
    REPORT_CHARGE_COLUMN_LABELS,
    REPORT_COMPARISON_COLUMN_LABELS,
    REPORT_SECTION_LABELS,
    VALUATION_TABLE_TEMPLATES,
)


class ValuationReportError(ValueError):
    """Raised when an unsupported or malformed valuation report is requested."""


def task_number(estimate: dict[str, Any]) -> str:
    """Return the task number from the dedicated column or an older snapshot."""
    inputs = estimate.get("input_snapshot") or {}
    return str(estimate.get("task_no") or inputs.get("task_no") or "").strip()


def estimate_name(estimate: dict[str, Any]) -> str:
    """Return the saved naming-template result, if the estimate has one."""
    inputs = estimate.get("input_snapshot") or {}
    return str(estimate.get("estimate_name") or inputs.get("estimate_name") or "").strip()


def waseeka_name(estimate: dict[str, Any]) -> str:
    """Return the waseeka name from the saved column or old input snapshot."""
    inputs = estimate.get("input_snapshot") or {}
    return str(estimate.get("waseeka_name") or inputs.get("waseeka_name") or "").strip()


def registry_area(estimate: dict[str, Any]) -> str:
    """Return the registry area, falling back to the saved location snapshot."""
    inputs = estimate.get("input_snapshot") or {}
    location = estimate.get("location_snapshot") or {}
    return str(
        estimate.get("registry_area")
        or inputs.get("registry_area")
        or location.get("property_area")
        or ""
    ).strip()


def saved_at_text(estimate: dict[str, Any]) -> str:
    """Show when an estimate was saved, not merely its transaction date."""
    value = estimate.get("created_at") or estimate.get("updated_at")
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value or "Not available")


def _safe_print_job_name(value: Any) -> str:
    """Sanitize a task number before using it in a temporary file name."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    return cleaned[:80].strip("._") or "valuation"


def valuation_pdf_filename(estimate: dict[str, Any], *, now: datetime | None = None) -> str:
    """Name a saved PDF with its saved identity and export date/time.

    v0.2.26 used the task number alone. v0.2.28 prefers the rendered estimate
    name (which already follows the user's template) and falls back to the
    task number for records saved by older builds. The export timestamp still
    prevents a second export from replacing the first. No client name or
    other personal data goes in the filename.
    """
    identity = estimate_name(estimate) or task_number(estimate) or "task"
    name = _safe_print_job_name(identity)
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return f"valuation_{name}_{stamp}.pdf"


CHARGE_EXPORT_KEYS = (
    "rate_value", "rate_unit", "published_year", "effective_from", "effective_to",
    "source_reference", "verification_status", "property_type", "classification",
    "floor",
)

#: PDF/print section headings shared by single and batch reports.
REPORT_SECTION_HEADINGS = {
    "property_details": "Property and valuation details",
    "charges": "Itemized taxes and fees",
    "fee_comparison": "Fee comparison (calculated valuation vs written value)",
    "explanation": "Calculation explanation",
    "warnings": "Warnings",
}


def _rate_export_row(rate: dict[str, Any] | None) -> dict[str, Any]:
    """Slim a schedule rate row to the snapshot fields reports need."""
    if not rate:
        return {}
    return {key: rate.get(key) for key in CHARGE_EXPORT_KEYS if rate.get(key) is not None}


def build_current_export_record(
    *,
    inputs: dict[str, Any],
    valuation: dict[str, Any],
    charges: dict[str, Any],
    location: dict[str, Any] | None = None,
    dc_rate: dict[str, Any] | None = None,
    fbr_rate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Shape an unsaved calculation like a stored estimate for the exporters.

    The report exporters read the same keys as :func:`estimate_service.get_estimate`
    output. Building the record keeps "export the current result" working
    without writing anything to the database first.
    """
    totals = charges.get("totals_by_currency", {})
    return {
        "id": None,
        "transaction_date": inputs.get("transaction_date"),
        "client_name": None,
        "property_type": inputs.get("property_type"),
        "classification": inputs.get("classification"),
        "area_value": inputs.get("area_value"),
        "area_unit": inputs.get("area_unit"),
        "share_numerator": inputs.get("share_numerator", 1),
        "share_denominator": inputs.get("share_denominator", 1),
        "declared_consideration": inputs.get("declared_consideration"),
        "property_value": valuation.get("property_value"),
        "transferred_value": valuation.get("transferred_value"),
        "total_charges": totals.get("PKR", charges.get("total_charges")),
        "currency_code": charges.get("total_currency", "PKR"),
        "input_snapshot": dict(inputs),
        "location_snapshot": ({key: value for key, value in location.items()
                               if not str(key).startswith("_")} if location else None),
        "rate_snapshot": {
            "dc": _rate_export_row(dc_rate),
            "fbr": _rate_export_row(fbr_rate),
        } if (dc_rate or fbr_rate) else None,
        "result_snapshot": {"valuation": dict(valuation), "charges": charges},
        "warnings": list(charges.get("warnings", [])),
        "charges": [
            {
                "name": charge.get("name"),
                "category": charge.get("category"),
                "payer": charge.get("payer"),
                "calculation_method": charge.get("calculation_method"),
                "calculation_base": charge.get("calculation_base"),
                "applied_rate": charge.get("applied_rate"),
                "amount": charge.get("amount"),
                "currency_code": charge.get("currency_code", "PKR"),
                # Per-party filer charges carry the buyer/seller label so the
                # reports can show which party each charge belongs to.
                "party": charge.get("party"),
                "rule_snapshot": charge.get("rule_snapshot") or {},
            }
            for charge in charges.get("charges", [])
        ],
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _shape_pdf_text(value: Any) -> str:
    """Shape Arabic-script text for left-to-right PDF layout when supported."""
    text = str(value)
    if not any("\u0600" <= char <= "\u08ff" for char in text):
        return text
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
    except ImportError:
        # CSV and Excel remain Unicode-preserving even when PDF extras have not
        # yet been installed; the app's requirements include both helpers.
        return text
    return get_display(arabic_reshaper.reshape(text))


def _estimate_rows(estimate: dict[str, Any]) -> list[list[str]]:
    inputs = estimate.get("input_snapshot") or {}
    location = estimate.get("location_snapshot") or {}
    rate_snapshot = estimate.get("rate_snapshot") or {}
    # New estimates carry independently resolved DC/FBR rates. Older saved
    # estimates stored a single rate object, so retain support for both shapes.
    rate = rate_snapshot.get("dc") or rate_snapshot
    fbr_rate = rate_snapshot.get("fbr") or {}
    result = estimate.get("result_snapshot") or {}
    valuation = result.get("valuation") or {}
    # The current-result export passes the DC rate row as the location
    # snapshot (it has no "name" key), so fall back through the joined
    # location fields before giving up.
    location_name = (
        location.get("name") or location.get("mauza")
        or location.get("location_name") or location.get("locality")
    )
    parties_text = ""
    for party in inputs.get("parties") or []:
        label = str(party.get("name") or "").strip() or party.get("role") or "party"
        area = party.get("area_marla")
        parties_text += (
            f"{label}: {area if area not in (None, '') else 'whole deed'} marla"
            f" ({party.get('filer_status') or 'unknown'}); "
        )
    parties_text = parties_text.rstrip("; ")
    comparison = valuation.get("comparison") or {}
    if comparison and comparison.get("declared_is_below_valuation"):
        try:
            deficient = f"PKR {abs(Decimal(str(comparison['difference_declared_minus_valuation'])))}"
        except (InvalidOperation, ValueError, TypeError):
            deficient = str(comparison['difference_declared_minus_valuation'])
    elif comparison:
        deficient = "None (written value covers the calculated valuation)"
    else:
        deficient = "Not checked (no written value entered)"
    details = [
        ("Estimate ID", estimate.get("id") or "Unsaved"),
        ("Task no.", task_number(estimate) or "Not entered"),
        ("Saved as", estimate_name(estimate) or "Not named"),
        ("Waseeka name", waseeka_name(estimate) or "Not entered"),
        ("Registry area", registry_area(estimate) or "Not entered"),
        ("Saved at", saved_at_text(estimate)),
        ("Transaction date", estimate.get("transaction_date")),
        ("Client", estimate.get("client_name") or "Unlinked"),
        ("Transaction type", inputs.get("transaction_type")),
        ("Jurisdiction", inputs.get("jurisdiction")),
        ("Property type", estimate.get("property_type")),
        ("Classification", estimate.get("classification")),
        ("Location", location_name),
        ("District", location.get("district")),
        ("Tehsil", location.get("tehsil")),
        ("Mauza", location.get("mauza")),
        ("Square no.", inputs.get("square_no")),
        ("Kila no.", inputs.get("kila_no")),
        ("Khasra no.", inputs.get("khasra_no")),
        ("Original area", f"{inputs.get('area_value', '')} {inputs.get('area_unit', '')}"),
        ("Square feet per marla", inputs.get("square_feet_per_marla")),
        ("Deed parties (area marla, filer status)", parties_text or "Not entered"),
        ("DC rate", f"{rate.get('rate_value', '')} {rate.get('rate_unit', '')}"),
        ("FBR rate", f"{fbr_rate.get('rate_value', '')} {fbr_rate.get('rate_unit', '')}" if fbr_rate else "Unavailable"),
        ("Rate publication year", rate.get("published_year")),
        ("DC rate effective dates", f"{rate.get('effective_from', '')} to {rate.get('effective_to') or 'open'}"),
        ("DC rate source", rate.get("source_reference")),
        ("DC rate verification", rate.get("verification_status")),
        ("Full property value", estimate.get("property_value")),
        ("FBR valuation", valuation.get("fbr_value") if valuation.get("fbr_value") is not None else "Schedule unavailable"),
        ("Transferred share", f"{estimate.get('share_numerator')}/{estimate.get('share_denominator')}"),
        ("Transferred value", estimate.get("transferred_value")),
        ("Registry written value", inputs.get("registry_written_value", estimate.get("declared_consideration"))),
        ("Deficient amount", deficient),
        ("Building material value", inputs.get("building_material_value")),
        ("Total saved charges (PKR)", estimate.get("total_charges")),
        ("Warnings", "; ".join(estimate.get("warnings", [])) or "None"),
    ]
    return [[str(key), "" if value is None else str(value)] for key, value in details]


def _charge_rows(estimate: dict[str, Any]) -> list[list[str]]:
    rows = [["Charge", "Category", "Payer", "Party", "Method", "Base", "Rate", "Amount", "Currency", "Source", "Effective dates", "Verification"]]
    for charge in estimate.get("charges", []):
        snapshot = charge.get("rule_snapshot") or {}
        party = charge.get("party") or {}
        party_label = ""
        if party.get("label"):
            party_label = str(party["label"])
            if party.get("role"):
                party_label += f" ({party['role']})"
        rows.append([
            str(charge.get("name", "")),
            str(charge.get("category", "")),
            str(charge.get("payer") or "Not specified"),
            party_label,
            str(charge.get("calculation_method", "")),
            str(charge.get("calculation_base") if charge.get("calculation_base") is not None else ""),
            str(charge.get("applied_rate") if charge.get("applied_rate") is not None else ""),
            str(charge.get("amount", "")),
            str(charge.get("currency_code", "PKR")),
            str(snapshot.get("source_reference") or "Not supplied"),
            f"{snapshot.get('effective_from', '')} to {snapshot.get('effective_to') or 'open'}",
            str(snapshot.get("verification_status", "unverified")),
        ])
    return rows


_CHARGE_COLUMN_WEIGHTS = {
    "charge": 24, "category": 16, "payer": 12, "party": 18, "method": 14,
    "base": 16, "rate": 12, "amount": 18, "currency": 11, "source": 22,
    "effective_dates": 20, "verification": 14,
}
_COMPARISON_COLUMN_WEIGHTS = {
    "charge": 34, "calculated": 22, "written": 22, "difference": 22,
}


def _amount_text(value: Any, currency: str = "PKR") -> str:
    """Format a report amount without turning a missing value into zero."""
    if value is None or value == "":
        return "—"
    try:
        return f"{currency} {Decimal(str(value)):,.2f}"
    except (InvalidOperation, ValueError, TypeError):
        return str(value)


def _charge_identity(charge: dict[str, Any]) -> tuple[str, str, str]:
    party = charge.get("party") or {}
    return (
        str(charge.get("name") or ""),
        str(party.get("label") or ""),
        str(party.get("role") or ""),
    )


def _charge_display(charge: dict[str, Any]) -> str:
    name, label, role = _charge_identity(charge)
    if label and role:
        return f"{name} — {label} ({role})"
    if label:
        return f"{name} — {label}"
    return name


def _comparison_rows(
    estimate: dict[str, Any], columns: list[str],
) -> list[list[str]]:
    """Build the fee-comparison table rows from stored calculation snapshots."""
    selected = [key for key in columns if key in REPORT_COMPARISON_COLUMN_LABELS]
    if not selected:
        return []
    result = estimate.get("result_snapshot") or {}
    charges_data = result.get("charges") or {}
    written_data = charges_data.get("charges_on_written_valuation") or {}
    calculated = charges_data.get("charges") or []
    written = written_data.get("charges") or []
    if not calculated or not written:
        return []

    written_by_key = {_charge_identity(line): line for line in written}
    rows = [[REPORT_COMPARISON_COLUMN_LABELS[key] for key in selected]]
    for line in calculated:
        other = written_by_key.get(_charge_identity(line))
        written_amount = other.get("amount") if other else None
        difference = None
        if written_amount is not None and line.get("amount") is not None:
            try:
                difference = Decimal(str(line["amount"])) - Decimal(str(written_amount))
            except (InvalidOperation, ValueError, TypeError):
                difference = None
        values = {
            "charge": _charge_display(line),
            "calculated": _amount_text(line.get("amount"), line.get("currency_code", "PKR")),
            "written": _amount_text(
                written_amount,
                (other or {}).get("currency_code", line.get("currency_code", "PKR")),
            ) if other else "—",
            "difference": _amount_text(
                difference, line.get("currency_code", "PKR")
            ) if difference is not None else "—",
        }
        rows.append([values[key] for key in selected])

    totals = charges_data.get("totals_by_currency") or {}
    written_totals = written_data.get("totals_by_currency") or {}
    difference_total = {
        currency: Decimal(str(totals.get(currency, 0))) - Decimal(str(written_totals.get(currency, 0)))
        for currency in sorted(set(totals) | set(written_totals))
    }

    def joined(mapping: dict[str, Any]) -> str:
        return " · ".join(f"{currency}: {value}" for currency, value in mapping.items()) or "—"

    total_values = {
        "charge": "Total",
        "calculated": joined(totals),
        "written": joined(written_totals),
        "difference": joined(difference_total),
    }
    rows.append([total_values[key] for key in selected])
    return rows


def _pdf_color(value: Any, fallback: str):
    """Resolve a saved template colour with a safe fallback."""
    try:
        return colors.HexColor(str(value))
    except (ValueError, TypeError):
        return colors.HexColor(fallback)


def _pdf_table_style(
    style: dict[str, Any],
    row_count: int,
    *,
    font_size: float = 7,
    total_row: bool = False,
):
    """Apply the selected table template to a ReportLab table."""
    header_bg = _pdf_color(style.get("header_bg"), "#1F4E78")
    header_text = _pdf_color(style.get("header_text"), "#FFFFFF")
    row_even = _pdf_color(style.get("row_even"), "#F8FAFC")
    row_odd = _pdf_color(style.get("row_odd"), "#FFFFFF")
    border = _pdf_color(style.get("border_color"), "#CBD5E1")
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), header_bg),
        ("TEXTCOLOR", (0, 0), (-1, 0), header_text),
        ("GRID", (0, 0), (-1, -1), 0.35, border),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [row_odd, row_even]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("FONTSIZE", (0, 0), (-1, -1), font_size),
    ]
    if total_row and row_count > 1:
        commands.append(
            ("BACKGROUND", (0, row_count - 1), (-1, row_count - 1),
             _pdf_color(style.get("total_bg"), "#E2E8F0"))
        )
    return TableStyle(commands)


def _proportional_widths(
    columns: list[str], weights: dict[str, int], total_mm: float = 178,
) -> list[float]:
    """Share the printable page width across the selected report columns."""
    total_weight = sum(weights.get(key, 16) for key in columns)
    if not total_weight:
        return []
    return [total_mm * mm * weights.get(key, 16) / total_weight for key in columns]


def export_estimate_json(estimate: dict[str, Any]) -> bytes:
    """Return the full estimate record as pretty-printed UTF-8 JSON.

    JSON keeps every snapshot field (including deed parties and per-charge
    rule snapshots) machine-readable, so the report can be re-processed
    later without lossy flattening.
    """
    return json.dumps(
        estimate, ensure_ascii=False, indent=2, sort_keys=True, default=str,
    ).encode("utf-8")


def export_estimate_csv(estimate: dict[str, Any]) -> bytes:
    """Return UTF-8 CSV with labeled estimate data and itemized charges."""
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["Valuation estimate", "Value"])
    writer.writerows(_estimate_rows(estimate))
    writer.writerow([])
    writer.writerows(_charge_rows(estimate))
    writer.writerow([])
    writer.writerow(["Calculation explanation"])
    result = estimate.get("result_snapshot") or {}
    for line in (result.get("valuation") or {}).get("explanation", []):
        writer.writerow([line])
    return output.getvalue().encode("utf-8-sig")


def _style_sheet(sheet, rows: list[list[str]]) -> None:
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(fill_type="solid", fgColor="1F4E78")
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    for column_number in range(1, sheet.max_column + 1):
        values = [len(str(sheet.cell(row, column_number).value or "")) for row in range(1, sheet.max_row + 1)]
        sheet.column_dimensions[get_column_letter(column_number)].width = min(max(14, max(values, default=14) + 2), 60)


def export_estimate_xlsx(estimate: dict[str, Any]) -> bytes:
    """Return a workbook with estimate details, charges, and calculation trace."""
    workbook = Workbook()
    details = workbook.active
    details.title = "Estimate"
    details.append(["Valuation estimate", "Value"])
    for row in _estimate_rows(estimate):
        details.append(row)
    _style_sheet(details, [])

    charges = workbook.create_sheet("Charges")
    for row in _charge_rows(estimate):
        charges.append(row)
    _style_sheet(charges, [])

    working = workbook.create_sheet("Calculation")
    working.append(["Calculation explanation"])
    result = estimate.get("result_snapshot") or {}
    valuation = result.get("valuation") or {}
    for line in valuation.get("explanation", []):
        working.append([line])
    working.append(["Charge totals by currency"])
    for currency, amount in (result.get("charges") or {}).get("totals_by_currency", {}).items():
        working.append([currency, str(amount)])
    working.append(["Warnings"])
    for warning in estimate.get("warnings", []):
        working.append([warning])
    _style_sheet(working, [])
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def export_estimate_ods(estimate: dict[str, Any]) -> bytes:
    """Return an OpenDocument spreadsheet (ODS) with details, charges, trace.

    The first sheet mirrors the CSV layout: estimate key/value rows, the
    itemized charge table, totals, the calculation explanation, and warnings.
    """
    try:
        from odf.opendocument import OpenDocumentSpreadsheet
        from odf.table import Table, TableCell, TableRow
        from odf.text import P
    except ImportError as exc:  # pragma: no cover - odfpy ships in requirements
        raise ValuationReportError(
            "ODS export needs the optional odfpy package."
        ) from exc

    document = OpenDocumentSpreadsheet()
    sheet = Table(name="Estimate")

    def add_row(values: list[Any]) -> None:
        row = TableRow()
        for value in values:
            cell = TableCell(valuetype="string")
            cell.addElement(P(text=str(value) if value is not None else ""))
            row.addElement(cell)
        sheet.addElement(row)

    charge_rows = _charge_rows(estimate)
    add_row(["Valuation estimate", "Value"])
    for key, value in _estimate_rows(estimate):
        add_row([key, value])
    add_row([])
    add_row(charge_rows[0])
    for row in charge_rows[1:]:
        add_row(row)

    totals = ((estimate.get("result_snapshot") or {}).get("charges") or {}).get(
        "totals_by_currency", {}
    )
    if totals:
        add_row([])
        for currency, amount in totals.items():
            add_row([f"Total charges ({currency})", str(amount)])
    result = estimate.get("result_snapshot") or {}
    add_row([])
    add_row(["Calculation explanation", ""])
    for line in (result.get("valuation") or {}).get("explanation", []):
        add_row([line])
    add_row(["Warnings", "; ".join(estimate.get("warnings", [])) or "None"])

    document.spreadsheet.addElement(sheet)
    output = BytesIO()
    document.write(output)
    return output.getvalue()


def _pdf_font_name() -> str:
    """Use a local Unicode font where available; retain a portable fallback."""
    candidates = [
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf",
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "segoeui.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path.is_file():
            try:
                if "ValuationUnicode" not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont("ValuationUnicode", str(path)))
                return "ValuationUnicode"
            except Exception:
                continue
    return "Helvetica"


def _append_estimate_pdf_sections(
    story: list[Any],
    estimate: dict[str, Any],
    *,
    sections: set[str],
    charge_columns: list[str],
    comparison_columns: list[str],
    style: dict[str, Any],
    font_name: str,
    body: ParagraphStyle,
    heading: ParagraphStyle,
    cell_size: float,
) -> None:
    """Append one estimate's selected report sections to a PDF story.

    Factored out in v0.2.28 so a single estimate and a batch PDF render exactly
    the same tables, columns, explanation, and warnings.
    """
    if "property_details" in sections:
        story.append(Paragraph(REPORT_SECTION_HEADINGS["property_details"], heading))
        detail_rows = _estimate_rows(estimate)
        if "warnings" not in sections:
            detail_rows = [row for row in detail_rows if row[0] != "Warnings"]
        detail_data = [
            [
                Paragraph(f"<b>{escape(key)}</b>", body),
                Paragraph(escape(_shape_pdf_text(value)).replace("\n", "<br/>"), body),
            ]
            for key, value in detail_rows
        ]
        detail_table = Table(detail_data, colWidths=[48 * mm, 128 * mm], repeatRows=0)
        detail_table.setStyle(_pdf_table_style(style, len(detail_data), font_size=8))
        story.append(detail_table)

    snapshot = estimate.get("result_snapshot") or {}
    charges_data = snapshot.get("charges") or {}

    if "charges" in sections:
        story.append(Paragraph(REPORT_SECTION_HEADINGS["charges"], heading))
        charge_rows = _charge_rows(estimate)
        if charge_rows[1:]:
            charge_style = ParagraphStyle(
                "ValuationChargeCell", parent=body, fontName=font_name,
                fontSize=cell_size, leading=cell_size + 2,
            )
            header_style = ParagraphStyle(
                "ValuationChargeHead", parent=charge_style,
                textColor=_pdf_color(style.get("header_text"), "#FFFFFF"),
            )
            all_charge_keys = list(REPORT_CHARGE_COLUMN_LABELS)
            chart_data = [[
                Paragraph(
                    f"<b>{escape(REPORT_CHARGE_COLUMN_LABELS[key])}</b>", header_style
                )
                for key in charge_columns
            ]]
            for row in charge_rows[1:]:
                values = dict(zip(all_charge_keys, row))
                chart_data.append([
                    Paragraph(escape(_shape_pdf_text(values.get(key, ""))), charge_style)
                    for key in charge_columns
                ])
            charge_table = Table(
                chart_data,
                colWidths=_proportional_widths(
                    charge_columns, _CHARGE_COLUMN_WEIGHTS
                ),
                repeatRows=1,
            )
            charge_table.setStyle(
                _pdf_table_style(style, len(chart_data), font_size=cell_size)
            )
            story.append(charge_table)
            total_style = ParagraphStyle(
                "ValuationTotal", parent=body, textColor=accent_color(style),
                spaceAfter=2 * mm,
            )
            for currency, amount in charges_data.get("totals_by_currency", {}).items():
                story.append(Spacer(1, 1.5 * mm))
                story.append(Paragraph(
                    escape(_shape_pdf_text(f"Total {currency}: {amount}")), total_style
                ))
        else:
            story.append(Paragraph("No charge rule was applied to this estimate.", body))

    if "fee_comparison" in sections:
        story.append(Paragraph(REPORT_SECTION_HEADINGS["fee_comparison"], heading))
        comparison_rows = _comparison_rows(estimate, comparison_columns)
        if comparison_rows:
            comparison_style = ParagraphStyle(
                "ValuationComparisonCell", parent=body, fontName=font_name,
                fontSize=cell_size, leading=cell_size + 2,
            )
            comparison_header = ParagraphStyle(
                "ValuationComparisonHead", parent=comparison_style,
                textColor=_pdf_color(style.get("header_text"), "#FFFFFF"),
            )
            comparison_data = [
                [
                    Paragraph(
                        f"<b>{escape(value)}</b>" if index == 0
                        else escape(_shape_pdf_text(value)),
                        comparison_header if index == 0 else comparison_style,
                    )
                    for value in row
                ]
                for index, row in enumerate(comparison_rows)
            ]
            comparison_table = Table(
                comparison_data,
                colWidths=_proportional_widths(
                    comparison_columns, _COMPARISON_COLUMN_WEIGHTS
                ),
                repeatRows=1,
            )
            comparison_table.setStyle(_pdf_table_style(
                style, len(comparison_data), font_size=cell_size, total_row=True
            ))
            story.append(comparison_table)
        else:
            story.append(Paragraph(
                "Fee comparison was not calculated for this estimate.", body
            ))

    if "explanation" in sections:
        story.append(Paragraph(REPORT_SECTION_HEADINGS["explanation"], heading))
        for line in (snapshot.get("valuation") or {}).get("explanation", []):
            story.append(Paragraph(escape(_shape_pdf_text(line)), body))

    if "warnings" in sections:
        story.append(Paragraph(REPORT_SECTION_HEADINGS["warnings"], heading))
        for warning in estimate.get("warnings", []):
            story.append(Paragraph("• " + escape(_shape_pdf_text(warning)), body))
        if not estimate.get("warnings"):
            story.append(Paragraph("No calculation warnings were recorded.", body))


def accent_color(style: dict[str, Any]):
    """Return the configured accent colour for PDF totals/headings."""
    return _pdf_color(style.get("header_bg"), "#1F4E78")


def export_estimate_pdf(
    estimate: dict[str, Any],
    options: dict[str, Any] | None = None,
    table_style: dict[str, Any] | None = None,
) -> bytes:
    """Build a PDF using the saved section, column, and table-style choices.

    ``options`` and ``table_style`` are supplied by the UI from settings. When
    omitted, the built-in defaults keep standalone calls and older tests
    working without touching any database.
    """
    output = BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=A4, rightMargin=16 * mm, leftMargin=16 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Property valuation estimate #{estimate.get('id') or 'Unsaved'}",
        author="Land Office Manager",
    )
    font = _pdf_font_name()
    styles = getSampleStyleSheet()

    merged_options = {
        key: list(value) for key, value in DEFAULT_VALUATION_REPORT_OPTIONS.items()
    }
    if isinstance(options, dict):
        for key in merged_options:
            if isinstance(options.get(key), (list, tuple)):
                merged_options[key] = list(options[key])
    sections = set(merged_options["sections"]) & set(REPORT_SECTION_LABELS)
    charge_columns = [
        key for key in merged_options["charge_columns"]
        if key in REPORT_CHARGE_COLUMN_LABELS
    ] or list(REPORT_CHARGE_COLUMN_LABELS)
    comparison_columns = [
        key for key in merged_options["comparison_columns"]
        if key in REPORT_COMPARISON_COLUMN_LABELS
    ] or list(REPORT_COMPARISON_COLUMN_LABELS)

    style = dict(VALUATION_TABLE_TEMPLATES["classic"])
    if isinstance(table_style, dict):
        style.update({
            key: value for key, value in table_style.items() if key in style
        })
    accent = _pdf_color(style.get("header_bg"), "#1F4E78")
    text_color = _pdf_color(style.get("text_color"), "#1E293B")
    compact = bool(style.get("compact"))
    cell_size = 6.2 if compact else 7.0
    title = ParagraphStyle("ValuationTitle", parent=styles["Title"], fontName=font,
                           fontSize=17, leading=21, alignment=TA_CENTER,
                           textColor=accent, spaceAfter=6 * mm)
    body = ParagraphStyle("ValuationBody", parent=styles["BodyText"], fontName=font,
                          fontSize=8.5, leading=12, wordWrap="LTR",
                          textColor=text_color)
    task_style = ParagraphStyle("ValuationTask", parent=body, fontSize=10.5, leading=13,
                                alignment=TA_CENTER, textColor=accent,
                                spaceAfter=2 * mm)
    heading = ParagraphStyle("ValuationHeading", parent=styles["Heading2"], fontName=font,
                             fontSize=11, leading=14, textColor=accent,
                             spaceBefore=4 * mm, spaceAfter=2 * mm)
    task_no = task_number(estimate)
    story = [
        Paragraph("Property Valuation and Charge Estimate", title),
        Paragraph(f"Task no: {escape(_shape_pdf_text(task_no)) if task_no else 'Not entered'}", task_style),
        Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", body),
        Paragraph("Estimate only. Verify the applicable schedule, rule, transaction details, and official challan before use.", body),
        Spacer(1, 3 * mm),
    ]

    _append_estimate_pdf_sections(
        story, estimate,
        sections=sections,
        charge_columns=charge_columns,
        comparison_columns=comparison_columns,
        style=style,
        font_name=font,
        body=body,
        heading=heading,
        cell_size=cell_size,
    )

    def footer(canvas, _document):
        canvas.saveState()
        canvas.setFont(font, 7)
        canvas.setFillColor(colors.HexColor("#64748B"))
        canvas.drawString(16 * mm, 9 * mm, "Land Office Manager · Valuation estimate")
        canvas.drawRightString(A4[0] - 16 * mm, 9 * mm, f"Page {_document.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()


def _resolve_report_settings(
    options: dict[str, Any] | None,
    table_style: dict[str, Any] | None,
) -> tuple[set[str], list[str], list[str], dict[str, Any], float]:
    """Apply saved/default report choices for both single and batch PDFs."""
    merged_options = {
        key: list(value) for key, value in DEFAULT_VALUATION_REPORT_OPTIONS.items()
    }
    if isinstance(options, dict):
        for key in merged_options:
            if isinstance(options.get(key), (list, tuple)):
                merged_options[key] = list(options[key])
    sections = set(merged_options["sections"]) & set(REPORT_SECTION_LABELS)
    charge_columns = [
        key for key in merged_options["charge_columns"]
        if key in REPORT_CHARGE_COLUMN_LABELS
    ] or list(REPORT_CHARGE_COLUMN_LABELS)
    comparison_columns = [
        key for key in merged_options["comparison_columns"]
        if key in REPORT_COMPARISON_COLUMN_LABELS
    ] or list(REPORT_COMPARISON_COLUMN_LABELS)
    style = dict(VALUATION_TABLE_TEMPLATES["classic"])
    if isinstance(table_style, dict):
        style.update({
            key: value for key, value in table_style.items() if key in style
        })
    cell_size = 6.2 if bool(style.get("compact")) else 7.0
    return sections, charge_columns, comparison_columns, style, cell_size


def export_estimates_combined_pdf(
    records: list[dict[str, Any]],
    options: dict[str, Any] | None = None,
    table_style: dict[str, Any] | None = None,
    *,
    title: str | None = None,
) -> bytes:
    """Build one PDF containing every selected saved estimate.

    v0.2.26 could only export a one-row-per-estimate summary. This v0.2.28
    renderer reuses the same section builder as a single-estimate PDF, adding
    a page break between records and an identity header for each one.
    """
    selected = [record for record in records if isinstance(record, dict)]
    if not selected:
        raise ValuationReportError("Select at least one saved estimate to print.")
    sections, charge_columns, comparison_columns, style, cell_size = (
        _resolve_report_settings(options, table_style)
    )
    output = BytesIO()
    batch_title = title or "Valuation estimates — selected batch"
    document = SimpleDocTemplate(
        output, pagesize=A4, rightMargin=16 * mm, leftMargin=16 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
        title=batch_title, author="Land Office Manager",
    )
    font = _pdf_font_name()
    styles = getSampleStyleSheet()
    accent = accent_color(style)
    title_style = ParagraphStyle(
        "BatchTitle", parent=styles["Title"], fontName=font, fontSize=16,
        leading=20, alignment=TA_CENTER, textColor=accent, spaceAfter=4 * mm,
    )
    body = ParagraphStyle(
        "BatchBody", parent=styles["BodyText"], fontName=font, fontSize=8.5,
        leading=12, wordWrap="LTR",
        textColor=_pdf_color(style.get("text_color"), "#1E293B"),
    )
    heading = ParagraphStyle(
        "BatchHeading", parent=styles["Heading2"], fontName=font, fontSize=11,
        leading=14, textColor=accent, spaceBefore=3 * mm, spaceAfter=2 * mm,
    )
    task_style = ParagraphStyle(
        "BatchTask", parent=body, fontSize=10.5, leading=13,
        alignment=TA_CENTER, textColor=accent, spaceAfter=2 * mm,
    )
    story = [
        Paragraph(escape(_shape_pdf_text(batch_title)), title_style),
        Paragraph(
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')} · "
            f"Estimates: {len(selected)}",
            body,
        ),
        Paragraph(
            "Estimate only. Verify the applicable schedule, rule, transaction "
            "details, and official challan before use.",
            body,
        ),
        Spacer(1, 2 * mm),
    ]
    for index, estimate in enumerate(selected):
        if index:
            story.append(PageBreak())
        label = estimate_name(estimate) or (
            f"Estimate #{estimate.get('id')}" if estimate.get("id")
            else f"Estimate {index + 1}"
        )
        story.append(Paragraph(
            f"Estimate {index + 1} of {len(selected)} — "
            f"{escape(_shape_pdf_text(label))}",
            heading,
        ))
        task = task_number(estimate)
        story.append(Paragraph(
            f"Task no: {escape(_shape_pdf_text(task)) if task else 'Not entered'}",
            task_style,
        ))
        identity_bits = []
        if waseeka_name(estimate):
            identity_bits.append(f"Waseeka: {waseeka_name(estimate)}")
        if registry_area(estimate):
            identity_bits.append(f"Registry area: {registry_area(estimate)}")
        identity_bits.append(f"Saved: {saved_at_text(estimate)}")
        story.append(Paragraph(
            escape(_shape_pdf_text(" · ".join(identity_bits))), body
        ))
        _append_estimate_pdf_sections(
            story, estimate,
            sections=sections,
            charge_columns=charge_columns,
            comparison_columns=comparison_columns,
            style=style,
            font_name=font,
            body=body,
            heading=heading,
            cell_size=cell_size,
        )

    def footer(canvas, _document):
        canvas.saveState()
        canvas.setFont(font, 7)
        canvas.setFillColor(colors.HexColor("#64748B"))
        canvas.drawString(16 * mm, 9 * mm, "Land Office Manager · Batch estimate report")
        canvas.drawRightString(A4[0] - 16 * mm, 9 * mm, f"Page {_document.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()


def export_estimates_zip(
    records: list[dict[str, Any]],
    options: dict[str, Any] | None = None,
    table_style: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> bytes:
    """Package one full PDF per selected estimate in a single ZIP download."""
    selected = [record for record in records if isinstance(record, dict)]
    if not selected:
        raise ValuationReportError("Select at least one saved estimate to export.")
    output = BytesIO()
    used_names: set[str] = set()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, estimate in enumerate(selected, start=1):
            payload = export_estimate_pdf(estimate, options, table_style)
            stem = valuation_pdf_filename(estimate, now=now)[:-4]
            filename = f"{index:02d}_{stem}.pdf"
            suffix = 2
            while filename in used_names:
                filename = f"{index:02d}_{stem}_{suffix}.pdf"
                suffix += 1
            used_names.add(filename)
            archive.writestr(filename, payload)
        archive.writestr(
            "README.txt",
            "Land Office Manager individual estimate PDFs.\n"
            "Estimate only. Verify schedules, rules, and official challans before use.\n",
        )
    return output.getvalue()


def prepare_print_pdf(payload: bytes, job_name: str = "valuation") -> Path:
    """Write a PDF to a temporary print job without exposing client data in logs."""
    directory = Path(tempfile.gettempdir()) / "land_office_print_jobs"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    target = directory / f"{_safe_print_job_name(job_name)}_{stamp}.pdf"
    target.write_bytes(payload)
    return target


def print_pdf(payload: bytes, job_name: str = "valuation") -> Path:
    """Send a generated PDF to the Windows default printer.

    The spooler needs the file after this call returns, so the temporary PDF is
    intentionally left in place for the print subsystem to finish.
    """
    if os.name != "nt":
        raise ValuationReportError("Direct printing is available on Windows only.")
    target = prepare_print_pdf(payload, job_name)
    try:
        os.startfile(str(target), "print")  # type: ignore[attr-defined]
    except OSError as exc:
        raise ValuationReportError(f"Could not send the PDF to the printer: {exc}") from exc
    return target


def export_estimate_history(records: list[dict[str, Any]], file_format: str) -> bytes:
    """Export a compact estimate-history summary to CSV, XLSX, PDF, or JSON."""
    normalized = file_format.strip().lower()
    # v0.2.26 headers stay in their original positions so existing spreadsheet
    # columns/reports do not move; v0.2.28 identity columns are appended.
    headers = [
        "Estimate ID", "Date", "Client", "Property", "Classification",
        "Transferred value", "Charges (PKR)", "Task no.", "Saved as",
        "Waseeka name", "Registry area", "Saved at",
    ]

    def row_for(record: dict[str, Any]) -> list[str]:
        base = [str(record.get(key, "") or "") for key in (
            "id", "transaction_date", "client_name", "property_type", "classification",
            "transferred_value", "total_charges",
        )]
        base.extend([
            task_number(record), estimate_name(record), waseeka_name(record),
            registry_area(record), saved_at_text(record),
        ])
        return base

    rows = [row_for(record) for record in records]
    if normalized == "csv":
        text_output = StringIO(newline="")
        writer = csv.writer(text_output)
        writer.writerow(headers)
        writer.writerows(rows)
        return text_output.getvalue().encode("utf-8-sig")
    if normalized == "json":
        # Full machine-readable records, same shape as the single-estimate
        # JSON export, so history keeps every snapshot field.
        return json.dumps(
            records, ensure_ascii=False, indent=2, sort_keys=True, default=str,
        ).encode("utf-8")
    if normalized == "xlsx":
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Estimate History"
        sheet.append(headers)
        for row in rows:
            sheet.append(row)
        _style_sheet(sheet, [])
        output = BytesIO()
        workbook.save(output)
        workbook.close()
        return output.getvalue()
    if normalized == "pdf":
        output = BytesIO()
        font = _pdf_font_name()
        document = SimpleDocTemplate(output, pagesize=A4, rightMargin=12 * mm,
                                     leftMargin=12 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
                                     title="Valuation estimate history")
        style = ParagraphStyle("HistoryCell", fontName=font, fontSize=7, leading=9)
        data = [[Paragraph(escape(_shape_pdf_text(value)), style) for value in headers]]
        data.extend([[Paragraph(escape(_shape_pdf_text(value)), style) for value in row] for row in rows])
        table = Table(data, repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E78")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#CBD5E1")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        document.build([Paragraph("Valuation Estimate History", getSampleStyleSheet()["Title"]), table])
        return output.getvalue()
    raise ValuationReportError("Choose CSV, XLSX, PDF, or JSON for estimate reports.")
