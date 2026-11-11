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


def test_build_current_export_record_shapes_unsaved_result_for_exporters():
    from services.valuation_report_service import build_current_export_record

    inputs = {
        "transaction_date": "2026-11-10", "transaction_type": "sale",
        "area_value": "2-0-0", "area_unit": "kmf", "square_no": "2", "kila_no": "12",
        "property_type": "Rural", "classification": "Rural", "registry_written_value": "50000",
        "share_numerator": 1, "share_denominator": 1,
        "declared_consideration": "50000", "buyer_filer_status": "ATL",
    }
    valuation = {
        "property_value": "100000.00", "transferred_value": "100000.00",
        "explanation": ["Area: 2-0-0 K-M-F."],
    }
    charges = {
        "totals_by_currency": {"PKR": "150.00"},
        "total_currency": "PKR",
        "warnings": ["Test warning."],
        "charges": [{
            "name": "Unverified test rule", "category": "test", "payer": "buyer",
            "calculation_method": "fixed_amount", "calculation_base": None,
            "applied_rate": None, "amount": "150.00", "currency_code": "PKR",
            "rule_snapshot": {"source_reference": "Test source",
                              "verification_status": "unverified"},
        }],
    }
    dc_rate = {"mauza": "Chak 1", "square_no": "2", "kila_no": "12",
               "rate_value": "100000", "rate_unit": "per_acre",
               "verification_status": "unverified", "_hidden": "skip me"}
    record = build_current_export_record(
        inputs=inputs, valuation=valuation, charges=charges,
        location=dc_rate, dc_rate=dc_rate, fbr_rate=None,
    )

    assert record["id"] is None
    assert record["client_name"] is None
    assert record["total_charges"] == "150.00"
    assert record["currency_code"] == "PKR"
    assert record["warnings"] == ["Test warning."]
    assert record["location_snapshot"]["mauza"] == "Chak 1"
    # Underscore-prefixed internal keys never reach the snapshot.
    assert "_hidden" not in record["location_snapshot"]
    assert record["rate_snapshot"]["dc"]["rate_value"] == "100000"
    assert record["rate_snapshot"]["fbr"] == {}
    assert record["charges"][0]["rule_snapshot"]["verification_status"] == "unverified"

    # The record must export without touching any database: id None prints
    # as "Unsaved" in every format.
    csv_content = export_estimate_csv(record).decode("utf-8-sig")
    assert "Unsaved" in csv_content
    assert "Unverified test rule" in csv_content
    workbook = load_workbook(BytesIO(export_estimate_xlsx(record)), read_only=True)
    assert "Chak 1" in [cell for row in workbook["Estimate"].iter_rows(values_only=True) for cell in row]
    workbook.close()
    assert export_estimate_pdf(record).startswith(b"%PDF-")


def test_export_estimate_ods_creates_valid_opendocument_spreadsheet():
    import zipfile

    from services.valuation_report_service import export_estimate_ods
    from odf.opendocument import load

    ods_bytes = export_estimate_ods(_estimate())
    archive = zipfile.ZipFile(BytesIO(ods_bytes))
    mimetype = archive.read("mimetype").decode("ascii")
    assert mimetype == "application/vnd.oasis.opendocument.spreadsheet"
    content = archive.read("content.xml").decode("utf-8")
    assert "Hypothetical fee" in content
    assert "احمد خان" in content
    assert "Unverified hypothetical" in content
    archive.close()
    # odfpy can also round-trip the document structure.
    document = load(BytesIO(ods_bytes))
    assert document is not None


def test_export_estimate_ods_uses_unsaved_label_for_current_results():
    from services.valuation_report_service import build_current_export_record, export_estimate_ods
    import zipfile

    record = build_current_export_record(
        inputs={"transaction_date": "2026-11-10", "area_unit": "kmf"},
        valuation={"property_value": "1"}, charges={"totals_by_currency": {}, "charges": [], "warnings": []},
    )
    content = zipfile.ZipFile(BytesIO(export_estimate_ods(record))).read("content.xml").decode("utf-8")
    assert "Unsaved" in content


def test_json_export_round_trips_the_full_record():
    import json as json_module

    from services.valuation_report_service import export_estimate_json

    estimate = _estimate()
    payload = export_estimate_json(estimate)
    parsed = json_module.loads(payload.decode("utf-8"))
    assert parsed["id"] == 12
    assert parsed["charges"][0]["name"] == "Hypothetical fee"
    assert parsed["result_snapshot"]["charges"]["totals_by_currency"] == {"PKR": "250.00"}


def test_estimate_rows_include_parties_fbr_and_deficient_lines():
    from services.valuation_report_service import _estimate_rows

    estimate = _estimate()
    estimate["input_snapshot"]["parties"] = [
        {"role": "buyer", "name": None, "area_marla": "22", "filer_status": "ATL"},
        {"role": "seller", "name": "Khalid", "area_marla": "20", "filer_status": "non_ATL"},
    ]
    estimate["result_snapshot"]["valuation"].update({
        "fbr_value": None,
        "comparison": {
            "declared_consideration": "70000.00",
            "transferred_valuation": "80000.00",
            "difference_declared_minus_valuation": "-10000.00",
            "declared_is_below_valuation": True,
        },
    })
    rows = dict(_estimate_rows(estimate))
    assert rows["Deed parties (area marla, filer status)"] == (
        "buyer: 22 marla (ATL); Khalid: 20 marla (non_ATL)"
    )
    assert rows["FBR valuation"] == "Schedule unavailable"
    assert rows["Deficient amount"] == "PKR 10000.00"


def test_deficient_line_reports_none_when_written_value_covers():
    from services.valuation_report_service import _estimate_rows

    estimate = _estimate()
    estimate["result_snapshot"]["valuation"]["comparison"] = {
        "declared_consideration": "90000.00",
        "transferred_valuation": "80000.00",
        "difference_declared_minus_valuation": "10000.00",
        "declared_is_below_valuation": False,
    }
    rows = dict(_estimate_rows(estimate))
    assert rows["Deficient amount"] == "None (written value covers the calculated valuation)"


def test_charge_rows_carry_party_labels_and_json_history_export():
    import json as json_module

    from services.valuation_report_service import (
        _charge_rows, export_estimate_history, export_estimate_json,
    )

    estimate = _estimate()
    estimate["charges"][0]["party"] = {"label": "Buyer 1", "role": "buyer"}
    rows = _charge_rows(estimate)
    assert rows[0][3] == "Party"
    assert rows[1][3] == "Buyer 1 (buyer)"

    history = export_estimate_history([estimate], "json")
    parsed = json_module.loads(history.decode("utf-8"))
    assert parsed[0]["id"] == 12

    record = export_estimate_json(estimate)
    assert record.startswith(b"{")
