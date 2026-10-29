"""CSV, Excel, and PDF reports for saved valuation estimates."""

from __future__ import annotations

import csv
from datetime import datetime
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
from typing import Any
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
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


class ValuationReportError(ValueError):
    """Raised when an unsupported or malformed valuation report is requested."""


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
    details = [
        ("Estimate ID", estimate.get("id")),
        ("Transaction date", estimate.get("transaction_date")),
        ("Client", estimate.get("client_name") or "Unlinked"),
        ("Transaction type", inputs.get("transaction_type")),
        ("Jurisdiction", inputs.get("jurisdiction")),
        ("Property type", estimate.get("property_type")),
        ("Classification", estimate.get("classification")),
        ("Location", location.get("name")),
        ("District", location.get("district")),
        ("Tehsil", location.get("tehsil")),
        ("Mauza", location.get("mauza")),
        ("Square no.", inputs.get("square_no")),
        ("Kila no.", inputs.get("kila_no")),
        ("Khasra no.", inputs.get("khasra_no")),
        ("Original area", f"{inputs.get('area_value', '')} {inputs.get('area_unit', '')}"),
        ("Square feet per marla", inputs.get("square_feet_per_marla")),
        ("DC rate", f"{rate.get('rate_value', '')} {rate.get('rate_unit', '')}"),
        ("FBR rate", f"{fbr_rate.get('rate_value', '')} {fbr_rate.get('rate_unit', '')}" if fbr_rate else "Unavailable"),
        ("Rate publication year", rate.get("published_year")),
        ("DC rate effective dates", f"{rate.get('effective_from', '')} to {rate.get('effective_to') or 'open'}"),
        ("DC rate source", rate.get("source_reference")),
        ("DC rate verification", rate.get("verification_status")),
        ("Full property value", estimate.get("property_value")),
        ("Transferred share", f"{estimate.get('share_numerator')}/{estimate.get('share_denominator')}"),
        ("Transferred value", estimate.get("transferred_value")),
        ("Registry written value", inputs.get("registry_written_value", estimate.get("declared_consideration"))),
        ("Building material value", inputs.get("building_material_value")),
        ("Total saved charges (PKR)", estimate.get("total_charges")),
        ("Warnings", "; ".join(estimate.get("warnings", [])) or "None"),
    ]
    return [[str(key), "" if value is None else str(value)] for key, value in details]


def _charge_rows(estimate: dict[str, Any]) -> list[list[str]]:
    rows = [["Charge", "Category", "Payer", "Method", "Base", "Rate", "Amount", "Currency", "Source", "Effective dates", "Verification"]]
    for charge in estimate.get("charges", []):
        snapshot = charge.get("rule_snapshot") or {}
        rows.append([
            str(charge.get("name", "")),
            str(charge.get("category", "")),
            str(charge.get("payer") or "Not specified"),
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


def export_estimate_pdf(estimate: dict[str, Any]) -> bytes:
    """Build a detailed PDF report with source/verification and warning fields."""
    output = BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=A4, rightMargin=16 * mm, leftMargin=16 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Property valuation estimate #{estimate.get('id', '')}",
        author="Land Office Manager",
    )
    font = _pdf_font_name()
    styles = getSampleStyleSheet()
    title = ParagraphStyle("ValuationTitle", parent=styles["Title"], fontName=font,
                           fontSize=17, leading=21, alignment=TA_CENTER,
                           textColor=colors.HexColor("#1F4E78"), spaceAfter=6 * mm)
    body = ParagraphStyle("ValuationBody", parent=styles["BodyText"], fontName=font,
                          fontSize=8.5, leading=12, wordWrap="LTR")
    heading = ParagraphStyle("ValuationHeading", parent=styles["Heading2"], fontName=font,
                             fontSize=11, leading=14, textColor=colors.HexColor("#1F4E78"),
                             spaceBefore=4 * mm, spaceAfter=2 * mm)
    story = [
        Paragraph("Property Valuation and Charge Estimate", title),
        Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", body),
        Paragraph("Estimate only. Verify the applicable schedule, rule, transaction details, and official challan before use.", body),
        Spacer(1, 3 * mm),
        Paragraph("Property and valuation details", heading),
    ]
    detail_data = []
    for key, value in _estimate_rows(estimate):
        detail_data.append([
            Paragraph(f"<b>{escape(key)}</b>", body),
            Paragraph(escape(_shape_pdf_text(value)).replace("\n", "<br/>"), body),
        ])
    table = Table(detail_data, colWidths=[48 * mm, 128 * mm], repeatRows=0)
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#CBD5E1")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F1F5F9")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(table)

    story.append(Paragraph("Itemized taxes and fees", heading))
    for row in _charge_rows(estimate)[1:]:
        line = " · ".join(part for part in (row[0], row[1], row[2], f"{row[6]} {row[7]}") if part)
        story.append(Paragraph(escape(_shape_pdf_text(line)), body))
        story.append(Paragraph(
            escape(_shape_pdf_text(f"Source: {row[8]} | Effective: {row[9]} | Status: {row[10]}")), body
        ))
    if not estimate.get("charges"):
        story.append(Paragraph("No charge rule was applied to this estimate.", body))

    snapshot = estimate.get("result_snapshot") or {}
    story.append(Paragraph("Calculation explanation", heading))
    for line in (snapshot.get("valuation") or {}).get("explanation", []):
        story.append(Paragraph(escape(_shape_pdf_text(line)), body))
    story.append(Paragraph("Warnings", heading))
    for warning in estimate.get("warnings", []):
        story.append(Paragraph("• " + escape(_shape_pdf_text(warning)), body))
    if not estimate.get("warnings"):
        story.append(Paragraph("No calculation warnings were recorded.", body))

    def footer(canvas, _document):
        canvas.saveState()
        canvas.setFont(font, 7)
        canvas.setFillColor(colors.HexColor("#64748B"))
        canvas.drawString(16 * mm, 9 * mm, "Land Office Manager · Valuation estimate")
        canvas.drawRightString(A4[0] - 16 * mm, 9 * mm, f"Page {_document.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()


def export_estimate_history(records: list[dict[str, Any]], file_format: str) -> bytes:
    """Export a compact estimate-history summary to CSV, XLSX, or PDF."""
    normalized = file_format.strip().lower()
    headers = ["Estimate ID", "Date", "Client", "Property", "Classification", "Transferred value", "Charges (PKR)"]
    rows = [[str(record.get(key, "") or "") for key in (
        "id", "transaction_date", "client_name", "property_type", "classification",
        "transferred_value", "total_charges",
    )] for record in records]
    if normalized == "csv":
        text_output = StringIO(newline="")
        writer = csv.writer(text_output)
        writer.writerow(headers)
        writer.writerows(rows)
        return text_output.getvalue().encode("utf-8-sig")
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
    raise ValuationReportError("Choose CSV, XLSX, or PDF for estimate reports.")
