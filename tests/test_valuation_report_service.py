"""Valuation report exports preserve estimate content and Unicode data."""

from io import BytesIO

from openpyxl import load_workbook

from services.valuation_report_service import (
    export_estimate_csv,
    export_estimate_history,
    export_estimate_pdf,
    export_estimate_xlsx,
    _shape_pdf_text,
)


def _estimate():
    return {
        "id": 12,
        "transaction_date": "2026-10-26",
        "client_name": "احمد خان",
        "property_type": "Residential",
        "classification": "Residential",
        "share_numerator": 1,
        "share_denominator": 5,
        "property_value": "400000.00",
        "transferred_value": "80000.00",
        "declared_consideration": "70000.00",
        "total_charges": "250.00",
        "currency_code": "PKR",
        "input_snapshot": {
            "transaction_type": "sale", "jurisdiction": "Punjab",
            "area_value": "4-0-0", "area_unit": "kmf", "square_feet_per_marla": 272,
        },
        "location_snapshot": {"name": "موضع لاہور", "district": "لاہور"},
        "rate_snapshot": {
            "schedule_type": "dc", "rate_value": "800000", "rate_unit": "per_acre",
            "published_year": 2025, "effective_from": "2025-07-01",
            "source_reference": "Hypothetical source", "verification_status": "unverified",
        },
        "result_snapshot": {
            "valuation": {"explanation": ["Area: 4-0-0 K-M-F = 21760 square feet."]},
            "charges": {"totals_by_currency": {"PKR": "250.00"}},
        },
        "warnings": ["Unverified hypothetical test rule."],
        "charges": [{
            "name": "Hypothetical fee", "category": "test", "payer": "buyer",
            "calculation_method": "fixed_amount", "calculation_base": None,
            "applied_rate": None, "amount": "250.00", "currency_code": "PKR",
            "rule_snapshot": {
                "source_reference": "Hypothetical test source",
                "effective_from": "2025-07-01", "effective_to": None,
                "verification_status": "unverified",
            },
        }],
    }


def test_single_estimate_csv_and_xlsx_keep_urdu_and_snapshots():
    estimate = _estimate()
    csv_content = export_estimate_csv(estimate).decode("utf-8-sig")
    assert "احمد خان" in csv_content
    assert "موضع لاہور" in csv_content
    assert "Hypothetical test source" in csv_content
    assert "Unverified hypothetical" in csv_content

    workbook = load_workbook(BytesIO(export_estimate_xlsx(estimate)), read_only=True)
    assert workbook.sheetnames == ["Estimate", "Charges", "Calculation"]
    detail_values = [cell for row in workbook["Estimate"].iter_rows(values_only=True) for cell in row]
    charge_values = [cell for row in workbook["Charges"].iter_rows(values_only=True) for cell in row]
    assert "احمد خان" in detail_values
    assert "موضع لاہور" in detail_values
    assert "Hypothetical test source" in charge_values
    workbook.close()


def test_dual_schedule_report_labels_dc_and_fbr_rates_and_cadastral_inputs():
    estimate = _estimate()
    estimate["input_snapshot"].update({
        "square_no": "12", "kila_no": "4", "khasra_no": "8/2",
        "registry_written_value": "70000", "building_material_value": "5000",
    })
    estimate["rate_snapshot"] = {
        "dc": {"rate_value": "800000", "rate_unit": "per_acre", "published_year": 2025,
               "effective_from": "2025-07-01", "source_reference": "DC test", "verification_status": "verified"},
        "fbr": {"rate_value": "1000000", "rate_unit": "per_acre"},
    }
    content = export_estimate_csv(estimate).decode("utf-8-sig")
    for label in ("DC rate", "FBR rate", "Square no.", "Kila no.", "Khasra no.", "Registry written value", "Building material value"):
        assert label in content


def test_estimate_pdf_and_history_exports_create_valid_files():
    estimate = _estimate()
    pdf = export_estimate_pdf(estimate)
    assert pdf.startswith(b"%PDF-")
    assert len(pdf) > 1000

    summary = [{
        "id": estimate["id"], "transaction_date": estimate["transaction_date"],
        "client_name": estimate["client_name"], "property_type": estimate["property_type"],
        "classification": estimate["classification"],
        "transferred_value": estimate["transferred_value"],
        "total_charges": estimate["total_charges"],
    }]
    history_csv = export_estimate_history(summary, "csv").decode("utf-8-sig")
    assert "احمد خان" in history_csv
    history_xlsx = load_workbook(BytesIO(export_estimate_history(summary, "xlsx")), read_only=True)
    assert history_xlsx["Estimate History"]["C2"].value == "احمد خان"
    history_xlsx.close()
    assert export_estimate_history(summary, "pdf").startswith(b"%PDF-")


def test_pdf_arabic_text_gets_contextual_forms_when_optional_helpers_are_installed():
    shaped = _shape_pdf_text("لاہور")
    assert shaped
    try:
        import arabic_reshaper  # noqa: F401
        from bidi.algorithm import get_display  # noqa: F401
    except ImportError:
        assert shaped == "لاہور"
    else:
        assert shaped != "لاہور"
