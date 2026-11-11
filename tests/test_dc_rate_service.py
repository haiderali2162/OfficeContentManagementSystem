"""DC-rate CRUD and workbook-import tests on temporary SQLite databases."""

from datetime import date
from decimal import Decimal
from io import BytesIO

import pytest
from openpyxl import Workbook
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy import inspect, text

from core.database import Base
from core.models import DCValuationRate, KhewatReference, PropertyKhewat, PropertyLocation
from services import dc_rate_service
from services.dc_rate_duplicate_service import DuplicateRuleError


@pytest.fixture
def rate_database(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'dc_rates.sqlite3').as_posix()}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    isolated_sessions = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False
    )
    monkeypatch.setattr(dc_rate_service, "SessionLocal", isolated_sessions)
    try:
        yield engine
    finally:
        engine.dispose()


def _rate_values(**overrides):
    values = {
        "property_type": "Residential",
        "classification": "Urban",
        "rate_value": "12500.25",
        "rate_unit": "per_marla",
        "effective_from": "2025-07-01",
        "effective_to": "",
        "source_reference": "Hypothetical test schedule",
        "source_date": "2025-06-15",
        "verification_status": "unverified",
        "verified_by": "",
    }
    values.update(overrides)
    return values


def _location_values(**overrides):
    values = {
        "name": "Sample Mauza",
        "province": "Punjab",
        "district": "Sample District",
        "tehsil": "Sample Tehsil",
        "mauza": "Sample Mauza",
        "hadbast_number": "123",
        "locality": "Urban",
    }
    values.update(overrides)
    return values


def _workbook_bytes(rows, headers=None):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(
        headers
        or [
            "Location",
            "Province",
            "District",
            "Tehsil",
            "Rural or Urban",
            "Mauza",
            "Hadbast Number",
            "Locality",
            "Property Type",
            "Classification",
            "Rate Value",
            "Rate Unit",
            "Effective From",
            "Effective To",
            "Source Reference",
            "Source Date",
            "Verification Status",
            "Verified By",
        ]
    )
    for row in rows:
        sheet.append(row)
    payload = BytesIO()
    workbook.save(payload)
    return payload.getvalue()


def _excel_row(location, rate, unit="per_marla", start="2025-07-01"):
    return [
        location,
        "Punjab",
        "Workbook District",
        "Workbook Tehsil",
        "Urban",
        location,
        "456",
        "Urban",
        "Residential",
        "Urban",
        rate,
        unit,
        start,
        None,
        "Hypothetical workbook source",
        "2025-06-30",
        "unverified",
        None,
    ]


def test_location_and_rate_crud_search_and_effective_filters(rate_database):
    location_id, created = dc_rate_service.create_location(**_location_values())
    assert created is True
    same_id, created_again = dc_rate_service.create_location(**_location_values())
    assert (same_id, created_again) == (location_id, False)

    rate_id = dc_rate_service.create_dc_rate(
        location_id=location_id, **_rate_values()
    )
    record = dc_rate_service.get_dc_rate(rate_id)
    assert record["rate_value"] == Decimal("12500.25000000")
    assert record["verification_status"] == "unverified"

    assert len(
        dc_rate_service.list_dc_rates(
            search="Sample District", effective_on=date(2025, 8, 1)
        )
    ) == 1
    assert dc_rate_service.list_dc_rates(effective_on=date(2024, 1, 1)) == []
    assert dc_rate_service.list_dc_rates(rate_unit="per_acre") == []

    dc_rate_service.update_dc_rate(
        rate_id=rate_id,
        location_id=location_id,
        **_rate_values(rate_value="13000.00", classification="Rural"),
    )
    updated = dc_rate_service.get_dc_rate(rate_id)
    assert updated["rate_value"] == Decimal("13000.00000000")
    assert updated["classification"] == "Rural"

    with pytest.raises(dc_rate_service.DCRateServiceError, match="conflicts"):
        dc_rate_service.create_dc_rate(
            location_id=location_id,
            **_rate_values(rate_value="999.00", classification="Rural"),
        )


def test_verified_rate_requires_source_and_verifier(rate_database):
    location_id, _ = dc_rate_service.create_location(**_location_values())
    with pytest.raises(dc_rate_service.DCRateServiceError, match="requires a source"):
        dc_rate_service.create_dc_rate(
            location_id=location_id,
            **_rate_values(
                verification_status="verified",
                source_reference="",
                verified_by="",
            ),
        )


def test_schedule_year_and_reference_are_optional(rate_database):
    location_id, _ = dc_rate_service.create_location(**_location_values())
    rate_id = dc_rate_service.create_dc_rate(
        location_id=location_id,
        **_rate_values(published_year="", effective_from="", source_reference=""),
    )
    record = dc_rate_service.get_dc_rate(rate_id)
    assert record["published_year"] is None
    assert record["source_reference"] is None
    # The legacy effective_from column remains populated while the actual
    # schedule publication year is honestly left unspecified.
    assert record["effective_from"] == date.today()


def test_excel_preview_reports_duplicates_invalid_rows_and_missing_headers(rate_database):
    existing_row = _excel_row("Already present", 6000)
    seeded = dc_rate_service.preview_dc_rate_workbook(_workbook_bytes([existing_row]))
    dc_rate_service.import_dc_rate_records(seeded["valid_records"])

    rows = [
        _excel_row("New Mauza", 5000),
        _excel_row("New Mauza", 5000),  # All columns identical to the prior row.
        _excel_row("Bad Rate", -50),
        _excel_row("Bad Unit", 20, unit="per_square_furlong"),
        existing_row,  # All columns identical to the persisted row.
    ]
    preview = dc_rate_service.preview_dc_rate_workbook(
        _workbook_bytes(rows), duplicate_rule="default:urban_dc_overlap"
    )
    assert len(preview["valid_records"]) == 1
    assert [issue["kind"] for issue in preview["issues"]] == [
        "exact duplicate in workbook",
        "invalid",
        "invalid",
        "exact duplicate in database",
    ]
    assert preview["duplicates_removed"] == 2
    assert preview["invalid_rows"] == 2

    with pytest.raises(dc_rate_service.DCRateServiceError, match="Missing required"):
        dc_rate_service.preview_dc_rate_workbook(
            _workbook_bytes([["x", "y"]], headers=["Name", "Amount"]),
            duplicate_rule="default:urban_dc_overlap",
        )


def test_excel_import_adds_only_unique_rows_and_never_overwrites(rate_database):
    location_id, _ = dc_rate_service.create_location(**_location_values())
    existing_id = dc_rate_service.create_dc_rate(
        location_id=location_id, **_rate_values()
    )
    preview = dc_rate_service.preview_dc_rate_workbook(
        _workbook_bytes([_excel_row("Imported Mauza", 8000)]),
        duplicate_rule="default:urban_dc_overlap",
    )
    outcome = dc_rate_service.import_dc_rate_records(
        preview["valid_records"], duplicate_rule="default:urban_dc_overlap"
    )

    assert outcome["imported_count"] == 1
    assert outcome["issues"] == []
    with Session(rate_database) as session:
        records = session.scalars(select(DCValuationRate)).all()
        assert len(records) == 2
        original = session.get(DCValuationRate, existing_id)
        assert original.rate_value == Decimal("12500.25000000")
        imported_location = session.scalar(
            select(PropertyLocation).where(PropertyLocation.name == "Imported Mauza")
        )
        assert imported_location is not None
        imported_rate = next(
            record for record in records if record.location_id == imported_location.id
        )
        assert imported_rate.rate_value == Decimal("8000.00000000")


def test_large_rate_import_uses_batched_locations_and_rate_inserts(rate_database):
    candidates = [{
        "row": row_number + 1,
        "location": {
            "name": f"Bulk Mauza {row_number}",
            "district": "Bulk District",
            "tehsil": "Bulk Tehsil",
        },
        **_rate_values(published_year="2025"),
    } for row_number in range(1200)]

    outcome = dc_rate_service.import_dc_rate_records(
        candidates, duplicate_rule="default:any_exact"
    )

    assert outcome["imported_count"] == 1200
    assert outcome["issues"] == []
    assert outcome["warnings"] == []
    assert len(dc_rate_service.list_dc_rates()) == 1200


def test_dc_and_fbr_records_are_separate_schedules(rate_database):
    location_id, _ = dc_rate_service.create_location(
        **_location_values(land_type="urban", committee_town="Town A", floor="Ground")
    )
    dc_id = dc_rate_service.create_dc_rate(
        location_id=location_id, **_rate_values(schedule_type="dc", floor="Ground")
    )
    fbr_id = dc_rate_service.create_dc_rate(
        location_id=location_id, **_rate_values(schedule_type="fbr", floor="Ground")
    )
    assert dc_id != fbr_id
    assert len(dc_rate_service.list_dc_rates(schedule_type="dc")) == 1
    assert len(dc_rate_service.list_dc_rates(schedule_type="fbr")) == 1


def test_read_schedule_sheets_reads_every_worksheet_with_row_numbers():
    workbook = Workbook()
    first = workbook.active
    first.title = "Mauza A"
    first.append(["Tehsil", "Mauza", "Classification", "Location", "DC Rate", "Unit"])
    first.append(["Tehsil A", "Mauza A", "Agricultural", "Farm", "100", "per_marla"])
    later = workbook.create_sheet("Mauza B")
    later.append(["Tehsil", "Mauza", "Classification", "Location", "DC Rate", "Unit"])
    later.append(["Tehsil B", "Mauza B", "Agricultural", "Farm", "200", "per_marla"])
    later.append(["Tehsil B", "Mauza B", "Agricultural", "Orchard", "300", "per_marla"])
    empty = workbook.create_sheet("Notes")
    empty.append([None, None])
    payload = BytesIO()
    workbook.save(payload)
    workbook.close()

    sheets = dc_rate_service.read_schedule_sheets(payload.getvalue(), "book.xlsx")
    assert [sheet["name"] for sheet in sheets] == ["Mauza A", "Mauza B"]
    assert sheets[1]["row_numbers"] == [2, 3]

    groups = dc_rate_service.group_schedule_sheets(sheets)
    assert len(groups) == 1
    assert groups[0]["sheets"] == ["Mauza A", "Mauza B"]
    assert groups[0]["rows"] == [
        ["Tehsil A", "Mauza A", "Agricultural", "Farm", "100", "per_marla"],
        ["Tehsil B", "Mauza B", "Agricultural", "Farm", "200", "per_marla"],
        ["Tehsil B", "Mauza B", "Agricultural", "Orchard", "300", "per_marla"],
    ]
    assert groups[0]["row_numbers"] == [2, 2, 3]
    assert groups[0]["row_sheets"] == ["Mauza A", "Mauza B", "Mauza B"]


def test_group_schedule_sheets_keeps_different_layouts_separate():
    sheets = [
        {"name": "Rural", "headers": ["Mauza", "Square No", "DC Rate"],
         "rows": [["Mauza A", "1", "100"]], "row_numbers": [2]},
        {"name": "Urban", "headers": ["Town", "Property Area", "DC Rate"],
         "rows": [["Town A", "5 marla", "200"]], "row_numbers": [2]},
    ]
    groups = dc_rate_service.group_schedule_sheets(sheets)
    assert len(groups) == 2
    assert groups[0]["sheets"] == ["Rural"]
    assert groups[1]["sheets"] == ["Urban"]


def test_preview_keeps_worksheet_provenance_for_each_row(rate_database):
    headers = [
        "Tehsil", "Qanoongoi", "Mauza", "Classification", "Location",
        "Square No", "Kila No", "DC Rate", "Unit",
    ]
    rows = [
        ["Tehsil A", "Qanoongoi A", "Mauza A", "Agricultural", "Farm", "1", "3", "100", "per_marla"],
        ["Tehsil B", "Qanoongoi B", "Mauza B", "Agricultural", "Farm", "2", "4", "200", "per_marla"],
    ]
    mapping = dc_rate_service.suggested_column_mapping(headers)
    preview = dc_rate_service.preview_mapped_schedule(
        headers,
        rows,
        mapping,
        "rural_dc",
        row_numbers=[7, 12],
        row_sheets=["Mauza A", "Mauza B"],
    )
    assert preview["rows_read"] == 2
    assert [
        (record["row"], record["sheet"])
        for record in preview["valid_records"]
    ] == [(7, "Mauza A"), (12, "Mauza B")]


def test_csv_column_mapping_imports_schedule_dimensions(rate_database):
    payload = (
        "District,Tehsil,Rural or Urban,Qanoongoi,Patwar Circle,Committee/Town,Revenue Circle,Floor,"
        "Property Area,Mauza,Square No,Kila No,Khasra No,Land Classification,Location,Rate,Rate Unit,Published Year\n"
        "Example District,Example Tehsil,Urban,Qanoongoi 1,,Town A,Circle 1,Ground,Area 1,Mauza 1,12,3,K-12,Residential,Main Road,15000,per_marla,2025\n"
    ).encode("utf-8")
    headers, rows = dc_rate_service.read_schedule_spreadsheet(payload, "rates.csv")
    mapping = dc_rate_service.suggested_column_mapping(headers)
    mapping["location_name"] = headers.index("Mauza")
    preview = dc_rate_service.preview_mapped_schedule(
        headers, rows, mapping, "fbr", duplicate_rule="default:fbr_overlap"
    )
    assert len(preview["valid_records"]) == 1
    record = preview["valid_records"][0]
    assert record["schedule_type"] == "fbr"
    assert record["location"]["revenue_circle"] == "Circle 1"
    assert record["published_year"] == 2025
    assert record["location"]["qanoongoi"] == "Qanoongoi 1"
    assert record["location"]["square_no"] == "12"
    result = dc_rate_service.import_dc_rate_records(
        preview["valid_records"], duplicate_rule="default:fbr_overlap"
    )
    assert result["imported_count"] == 1
    saved = dc_rate_service.list_dc_rates(schedule_type="fbr")[0]
    assert saved["committee_town"] == "Town A"
    assert saved["floor"] == "Ground"


@pytest.mark.parametrize("profile,headers,row,expected", [
    (
        "rural_dc",
        ["Serial No", "District", "Tehsil", "Qanoongoi", "Mauza", "Classification", "Location", "Square No", "Killa No", "DC Rate", "Unit"],
        ["18", "District A", "Tehsil A", "Qanoongoi A", "Mauza A", "Agricultural", "Canal side", "12", "4", "5000", "per_marla"],
        {"schedule_type": "dc", "land_type": "rural", "serial_no": "18", "mauza": "Mauza A", "square_no": "12", "rate_unit": "per_marla"},
    ),
    (
        "urban_dc",
        ["District", "Tehsil", "Qanoongoi", "Town", "Revenue Circle", "Property Area", "Classification", "Location", "Square No", "Killa No", "Khasra No", "DC Rate", "Unit", "Structure Rate"],
        ["District B", "Tehsil B", "Qanoongoi B", "Town B", "Circle B", "Block B", "Residential", "Main Road", "", "", "", "7000", "per_marla", "9000"],
        {"schedule_type": "dc", "land_type": "urban", "committee_town": "Town B", "locality": "Main Road", "structure_rate": Decimal("9000"), "rate_unit": "per_marla"},
    ),
    (
        "fbr",
        ["District", "Tehsil", "Serial No", "Revenue Circle", "Property Area", "Classification", "Location", "Location (Square No.)", "Value of property per Square Ft (in Rs.)", "Per marla Valuation"],
        ["District C", "Tehsil C", "9", "Circle C", "Sector C", "Commercial", "Market Road", "22", "123.50", ""],
        {"schedule_type": "fbr", "land_type": "urban", "serial_no": "9", "square_no": "22", "rate_unit": "per_square_foot", "per_marla_value": Decimal("33592.00")},
    ),
])
def test_schedule_specific_mapping_profiles(rate_database, profile, headers, row, expected):
    duplicate_rule = {
        "rural_dc": "default:rural_dc_overlap",
        "urban_dc": "default:urban_dc_overlap",
        "fbr": "default:fbr_overlap",
    }[profile]
    mapping = dc_rate_service.suggested_column_mapping(headers)
    preview = dc_rate_service.preview_mapped_schedule(
        headers, [row], mapping, profile, duplicate_rule=duplicate_rule
    )
    assert preview["issues"] == []
    assert len(preview["valid_records"]) == 1
    record = preview["valid_records"][0]
    location_fields = set(dc_rate_service.LOCATION_FIELDS)
    for field, expected_value in expected.items():
        if field in location_fields:
            actual = record["location"][field]
        else:
            actual = record.get(field)
        assert actual == expected_value
    assert dc_rate_service.import_dc_rate_records(
        preview["valid_records"], duplicate_rule=duplicate_rule
    )["imported_count"] == 1
    persisted = dc_rate_service.list_dc_rates(schedule_type=expected["schedule_type"])[0]
    assert persisted["land_type"] == expected["land_type"]
    if "structure_rate" in expected:
        assert persisted["structure_rate"] == expected["structure_rate"]
    if "per_marla_value" in expected:
        assert persisted["per_marla_value"] == expected["per_marla_value"]
    if "serial_no" in expected:
        assert persisted["serial_no"] == expected["serial_no"]


def test_schedule_mapping_reports_required_blank_values(rate_database):
    headers = ["Tehsil", "Qanoongoi", "Mauza", "Classification", "Location", "Square No", "Killa No", "DC Rate", "Unit"]
    mapping = dc_rate_service.suggested_column_mapping(headers)
    row = ["", "Qanoongoi", "Mauza", "Agricultural", "Farm", "1", "2", "1000", "per_marla"]
    preview = dc_rate_service.preview_mapped_schedule(
        headers, [row], mapping, "rural_dc", duplicate_rule="default:rural_dc_overlap"
    )
    assert preview["valid_records"] == []
    assert preview["issues"][0]["row"] == 2
    assert "tehsil" in preview["issues"][0]["message"]


def test_rural_schedule_accepts_khasra_without_square_or_kila(rate_database):
    headers = ["Tehsil", "Qanoongoi", "Mauza", "Classification", "Location", "Square No", "Killa No", "Khasra No", "DC Rate", "Unit"]
    row = ["Tehsil A", "Qanoongoi A", "Mauza A", "Agricultural", "Farm", "", "", "1-5,6;7-9", "1000", "per_marla"]
    mapping = dc_rate_service.suggested_column_mapping(headers)
    preview = dc_rate_service.preview_mapped_schedule(
        headers, [row], mapping, "rural_dc", duplicate_rule="default:rural_dc_overlap",
        storage_mode="individual",
    )
    assert preview["issues"] == []
    assert len(preview["valid_records"]) == 9
    assert preview["expanded_rows"] == 9
    assert sorted(
        candidate["location"]["khasra_no"]
        for candidate in preview["valid_records"]
    ) == ["1", "2", "3", "4", "5", "6", "7", "8", "9"]
    for candidate in preview["valid_records"]:
        assert candidate["location"]["square_no"] is None
        assert candidate["location"]["kila_no"] is None
    dc_rate_service.import_dc_rate_records(
        preview["valid_records"], duplicate_rule="default:rural_dc_overlap",
        storage_mode="individual",
    )
    assert len(dc_rate_service.list_dc_rates()) == 9
    matches = dc_rate_service.list_dc_rates(search="4")
    assert len(matches) == 1
    assert matches[0]["khasra_no"] == "4"


def test_schedule_import_persists_each_expanded_cadastral_combination(rate_database):
    headers = [
        "Tehsil", "Qanoongoi", "Mauza", "Classification", "Location",
        "Square No", "Killa No", "Khasra No", "DC Rate", "Unit",
    ]
    row = [
        "Tehsil A", "Qanoongoi A", "Mauza A", "Agricultural", "Farm",
        "1-3", "1-5", "", "5000", "per_marla",
    ]
    mapping = dc_rate_service.suggested_column_mapping(headers)
    preview = dc_rate_service.preview_mapped_schedule(
        headers, [row], mapping, "rural_dc", storage_mode="individual"
    )
    assert preview["issues"] == []
    assert preview["expanded_rows"] == 15
    assert len(preview["valid_records"]) == 15
    assert {
        (record["location"]["square_no"], record["location"]["kila_no"])
        for record in preview["valid_records"]
    } == {
        (str(square), str(kila))
        for square in range(1, 4)
        for kila in range(1, 6)
    }
    assert all(record["rate_value"] == Decimal("5000") for record in preview["valid_records"])

    outcome = dc_rate_service.import_dc_rate_records(
        preview["valid_records"], storage_mode="individual"
    )
    assert outcome["imported_count"] == 15
    assert outcome["expanded_count"] == 15
    persisted = dc_rate_service.list_dc_rates()
    assert len(persisted) == 15
    assert {
        (record["square_no"], record["kila_no"])
        for record in persisted
    } == {
        (str(square), str(kila))
        for square in range(1, 4)
        for kila in range(1, 6)
    }


def test_import_defaults_to_compact_range_storage(rate_database):
    headers = [
        "Tehsil", "Qanoongoi", "Mauza", "Classification", "Location",
        "Square No", "Killa No", "Khasra No", "DC Rate", "Unit",
    ]
    row = [
        "Tehsil A", "Qanoongoi A", "Mauza A", "Agricultural", "Farm",
        "1-3", "1-5", "7; 9-10", "5000", "per_marla",
    ]
    mapping = dc_rate_service.suggested_column_mapping(headers)
    preview = dc_rate_service.preview_mapped_schedule(
        headers, [row], mapping, "rural_dc"
    )
    assert preview["issues"] == []
    # Ranges stay compact: one stored record for this source row.
    assert preview["expanded_rows"] == 1
    assert len(preview["valid_records"]) == 1
    stored = preview["valid_records"][0]["location"]
    assert stored["square_no"] == "1-3"
    assert stored["kila_no"] == "1-5"
    assert stored["khasra_no"] == "7, 9-10"

    outcome = dc_rate_service.import_dc_rate_records(preview["valid_records"])
    assert outcome["imported_count"] == 1
    persisted = dc_rate_service.list_dc_rates()
    assert len(persisted) == 1
    assert persisted[0]["square_no"] == "1-3"
    assert persisted[0]["kila_no"] == "1-5"
    assert persisted[0]["khasra_no"] == "7, 9-10"


def test_search_matches_individual_values_inside_stored_ranges(rate_database):
    headers = [
        "Tehsil", "Qanoongoi", "Mauza", "Classification", "Location",
        "Square No", "Killa No", "Khasra No", "DC Rate", "Unit",
    ]
    row = [
        "Tehsil A", "Qanoongoi A", "Mauza A", "Agricultural", "Farm",
        "1-9", "1-5", "", "5000", "per_marla",
    ]
    mapping = dc_rate_service.suggested_column_mapping(headers)
    preview = dc_rate_service.preview_mapped_schedule(
        headers, [row], mapping, "rural_dc"
    )
    dc_rate_service.import_dc_rate_records(preview["valid_records"])

    # An individual value inside the stored square range matches...
    assert len(dc_rate_service.list_dc_rates(square_no="5")) == 1
    # ...but a value that is only a substring (e.g. "10") does not.
    assert dc_rate_service.list_dc_rates(square_no="10") == []
    # An overlapping stored range matches an overlapping range query.
    assert len(dc_rate_service.list_dc_rates(square_no="4-12")) == 1
    assert dc_rate_service.list_dc_rates(square_no="10-12") == []
    # The free-text search uses the same range-aware matching.
    assert len(dc_rate_service.list_dc_rates(search="5")) == 1
    assert dc_rate_service.list_dc_rates(search="12") == []


def test_export_reference_mode_keeps_or_expands_ranges(rate_database):
    record = {
        "square_no": "1-3",
        "kila_no": "1-2",
        "khasra_no": None,
        "property_type": "Agricultural",
    }
    stored = dc_rate_service.export_schedule_records(
        [record], "csv", ["square_no", "kila_no"], reference_mode="stored"
    ).decode("utf-8-sig")
    assert stored.splitlines()[1] == "1-3,1-2"

    individual = dc_rate_service.export_schedule_records(
        [record], "csv", ["square_no", "kila_no"], reference_mode="individual"
    ).decode("utf-8-sig")
    rows = individual.splitlines()
    assert len(rows) == 7  # header + 3 squares x 2 kilas
    assert "1,1" in rows

    compact = dc_rate_service.export_schedule_records(
        [record], "csv", ["square_no", "kila_no"], reference_mode="ranges"
    ).decode("utf-8-sig")
    assert compact.splitlines()[1] == "1-3,1-2"


def test_export_import_issues_reports_sheet_and_row(rate_database):
    payload = dc_rate_service.export_import_issues([
        {
            "sheet": "Rural 2026",
            "row": 12,
            "kind": "invalid",
            "message": "Rate must be a valid decimal number.",
            "fix": "Correct the rate.",
            "source_values": ["Mauza A", "abc"],
        }
    ]).decode("utf-8-sig")
    rows = payload.splitlines()
    assert rows[0].startswith("Worksheet,Source row")
    assert "Rural 2026,12,invalid" in rows[1]
    assert "Mauza A | abc" in rows[1]


def test_import_removes_only_rows_identical_in_every_column(rate_database):
    base = _excel_row("Diff Mauza", 9000)
    changed_rate = list(base)
    changed_rate[10] = 9001
    changed_source = list(base)
    changed_source[14] = "Different workbook source"
    changed_date = list(base)
    changed_date[12] = "2025-08-01"
    preview = dc_rate_service.preview_dc_rate_workbook(
        _workbook_bytes([base, changed_rate, changed_source, changed_date])
    )
    assert preview["duplicates_removed"] == 0
    assert len(preview["valid_records"]) == 4
    outcome = dc_rate_service.import_dc_rate_records(preview["valid_records"])
    assert outcome["imported_count"] == 4
    assert outcome["duplicates_removed"] == 0


def test_verified_rows_remain_exact_duplicates_despite_generated_timestamp(rate_database):
    row = _excel_row("Verified Mauza", 7000)
    row[16] = "verified"
    row[17] = "Inspector"
    first = dc_rate_service.preview_dc_rate_workbook(_workbook_bytes([row]))
    assert len(first["valid_records"]) == 1
    assert first["valid_records"][0]["verification_status"] == "verified"
    dc_rate_service.import_dc_rate_records(first["valid_records"])

    # verified_at is generated at each validation/commit, so it is excluded from
    # the all-column comparison; every source-provided and persisted value matches.
    second = dc_rate_service.preview_dc_rate_workbook(_workbook_bytes([row]))
    assert second["valid_records"] == []
    assert second["issues"][0]["kind"] == "exact duplicate in database"
    assert second["duplicates_removed"] == 1


@pytest.mark.parametrize("column_header", [
    "Village",
    "Mauza / Village",
    "Name of Village",
    "Mouza",
])
def test_village_schedule_headers_map_to_mauza(column_header):
    headers = [
        "Tehsil", "Qanoongoi", column_header, "Classification", "Location",
        "Square No", "Killa No", "DC Rate", "Unit",
    ]
    mapping = dc_rate_service.suggested_column_mapping(headers)
    assert mapping["mauza"] == 2
    assert mapping["location_name"] == 2


def test_duplicate_mauza_headers_choose_the_village_column_for_search(rate_database):
    """A repeated district column must not hide the real Mauza values.

    Published schedules sometimes contain two columns labelled "Mauza": the
    first repeats the district and the second holds the villages. Selecting the
    first column silently loses the villages, so they cannot be searched later.
    """
    headers = [
        "Serial No", "District", "Tehsil", "Mauza", "Qanoongoi", "Mauza",
        "Classification", "Location", "Square No", "Killa No", "DC Rate", "Unit",
    ]
    rows = [
        ["1", "District A", "Tehsil A", "District A", "Qanoongoi A", "Village One",
         "Residential", "Off road", "16", "8", "181600", "per_marla"],
        ["2", "District A", "Tehsil A", "District A", "Qanoongoi A", "Village Two",
         "Residential", "Off road", "17", "9", "181600", "per_marla"],
    ]
    mapping = dc_rate_service.suggested_column_mapping(headers, rows)
    assert mapping["mauza"] == 5
    assert mapping["location_name"] == 5

    preview = dc_rate_service.preview_mapped_schedule(
        headers,
        rows,
        mapping,
        "rural_dc",
        duplicate_rule="default:rural_dc_overlap",
    )
    assert [
        record["location"]["mauza"] for record in preview["valid_records"]
    ] == ["Village One", "Village Two"]
    dc_rate_service.import_dc_rate_records(
        preview["valid_records"], duplicate_rule="default:rural_dc_overlap"
    )

    assert dc_rate_service.list_mauza_names() == ["Village One", "Village Two"]
    assert dc_rate_service.list_mauza_names(search="one") == ["Village One"]
    assert len(dc_rate_service.list_dc_rates(mauza="Village Two")) == 1


def test_duplicate_mauza_mapping_without_rows_keeps_legacy_first_column():
    headers = ["District", "Tehsil", "Mauza", "Qanoongoi", "Mauza", "DC Rate"]
    mapping = dc_rate_service.suggested_column_mapping(headers)
    assert mapping["mauza"] == 2


def test_year_suffixed_rate_header_maps_to_rate_value(rate_database):
    """A fiscal year in the rate header must not unmap the rate column."""
    headers = [
        "Tehsil", "Qanoongoi", "Mauza", "Classification", "Location",
        "Square No", "Killa No", "DC Rate (2026-2027)", "Unit",
    ]
    rows = [
        ["Tehsil A", "Qanoongoi A", "Mauza A", "Agricultural", "Farm",
         "16", "8", "1000", "per_marla"],
    ]
    mapping = dc_rate_service.suggested_column_mapping(headers, rows)
    assert mapping["rate_value"] == 7

    preview = dc_rate_service.preview_mapped_schedule(
        headers, rows, mapping, "rural_dc",
        duplicate_rule="default:rural_dc_overlap",
    )
    assert preview["issues"] == []
    assert len(preview["valid_records"]) == 1
    dc_rate_service.import_dc_rate_records(
        preview["valid_records"], duplicate_rule="default:rural_dc_overlap"
    )
    saved = dc_rate_service.list_dc_rates()[0]
    assert saved["rate_value"] == Decimal("1000")
    assert saved["rate_unit"] == "per_marla"


def test_urban_and_fbr_schedules_capture_optional_mauza_for_search(rate_database):
    """Urban DC and FBR rows keep their Mauza column when one exists."""
    urban_headers = [
        "District", "Tehsil", "Mauza", "Town", "Revenue Circle",
        "Property Area", "Classification", "Location", "Square No",
        "Killa No", "Khasra No", "DC Rate", "Unit", "Structure Rate",
    ]
    urban_row = [
        "District B", "Tehsil B", "Test Village Urban", "Town B", "Circle B",
        "Block B", "Residential", "Main Road", "12", "3", "K-4",
        "7000", "per_marla", "9000",
    ]
    urban_mapping = dc_rate_service.suggested_column_mapping(urban_headers, [urban_row])
    assert urban_mapping["mauza"] == 2
    urban_preview = dc_rate_service.preview_mapped_schedule(
        urban_headers, [urban_row], urban_mapping, "urban_dc",
        duplicate_rule="default:urban_dc_overlap",
    )
    assert urban_preview["issues"] == []
    dc_rate_service.import_dc_rate_records(
        urban_preview["valid_records"], duplicate_rule="default:urban_dc_overlap"
    )
    assert dc_rate_service.list_mauza_names(search="Test Village Urban") == [
        "Test Village Urban"
    ]
    assert len(dc_rate_service.list_dc_rates(
        mauza="Test Village Urban", schedule_type="dc",
    )) == 1

    fbr_headers = [
        "District", "Tehsil", "Mauza", "Serial No", "Revenue Circle",
        "Property Area", "Classification", "Location", "Location (Square No.)",
        "Value of property per Square Ft (in Rs.)", "Per marla Valuation",
    ]
    fbr_row = [
        "District C", "Tehsil C", "Test Village FBR", "9", "Circle C",
        "Sector C", "Commercial", "Market Road", "22", "123.50", "",
    ]
    fbr_mapping = dc_rate_service.suggested_column_mapping(fbr_headers, [fbr_row])
    assert fbr_mapping["mauza"] == 2
    fbr_preview = dc_rate_service.preview_mapped_schedule(
        fbr_headers, [fbr_row], fbr_mapping, "fbr",
        duplicate_rule="default:fbr_overlap",
    )
    assert fbr_preview["issues"] == []
    dc_rate_service.import_dc_rate_records(
        fbr_preview["valid_records"], duplicate_rule="default:fbr_overlap"
    )
    assert dc_rate_service.list_mauza_names(search="Test Village FBR") == [
        "Test Village FBR"
    ]
    fbr_saved = dc_rate_service.list_dc_rates(schedule_type="fbr")[0]
    assert fbr_saved["mauza"] == "Test Village FBR"
    assert fbr_saved["rate_unit"] == "per_square_foot"


def test_reference_range_normalization_validates_direction_and_size():
    assert dc_rate_service._normalize_reference("1-5,6;7-9") == "1, 2, 3, 4, 5, 6, 7, 8, 9"
    assert dc_rate_service._normalize_reference("25-21") == "21, 22, 23, 24, 25"
    assert dc_rate_service._normalize_reference("12; 14, 16-17") == "12, 14, 16, 17"
    assert dc_rate_service._normalize_reference("9-4") == "4, 5, 6, 7, 8, 9"
    with pytest.raises(dc_rate_service.DCRateServiceError, match="5,001"):
        dc_rate_service._normalize_reference("1-6000")


def test_reference_range_parser_handles_mixed_and_reversed_segments():
    assert dc_rate_service._reference_values(
        "1-5;6;7-9,10,11-13,15"
    ) == ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "15"]
    assert dc_rate_service._reference_values("5-1") == ["1", "2", "3", "4", "5"]


def test_range_expansion_uses_cartesian_product_and_preserves_other_fields():
    location = {
        "name": "Chak 1",
        "mauza": "Chak 1",
        "square_no": "1-3",
        "kila_no": "1-5",
        "khasra_no": None,
    }
    expanded = dc_rate_service._expand_location_values(location, source_row=7)
    assert len(expanded) == 15
    assert [
        (item["square_no"], item["kila_no"], item["khasra_no"])
        for item in expanded
    ] == [
        (str(square), str(kila), None)
        for square in range(1, 4)
        for kila in range(1, 6)
    ]
    assert all(item["name"] == "Chak 1" and item["mauza"] == "Chak 1" for item in expanded)


def test_range_expansion_includes_khasra_in_the_product():
    expanded = dc_rate_service._expand_location_values({
        "square_no": "1-2",
        "kila_no": "1-2",
        "khasra_no": "9-10",
    })
    assert len(expanded) == 8
    assert len({
        (item["square_no"], item["kila_no"], item["khasra_no"])
        for item in expanded
    }) == 8


def test_range_expansion_rejects_oversized_source_rows():
    with pytest.raises(dc_rate_service.DCRateServiceError, match="expands to"):
        dc_rate_service._expand_location_values({
            "square_no": "1-100",
            "kila_no": "1-100",
            "khasra_no": None,
        })



@pytest.mark.parametrize("unit,expected", [
    ("per kanal", "per_kanal"),
    ("Kanal", "per_kanal"),
    ("Rs. per Kanal Rate", "per_kanal"),
    ("Per marla", "per_marla"),
    ("per sq. ft.", "per_square_foot"),
    ("per acre", "per_acre"),
])
def test_import_accepts_common_rate_unit_wording(rate_database, unit, expected):
    location_id, _ = dc_rate_service.create_location(**_location_values())
    rate_id = dc_rate_service.create_dc_rate(
        location_id=location_id,
        **_rate_values(rate_unit=unit),
    )
    saved = dc_rate_service.get_dc_rate(rate_id)
    assert saved["rate_unit"] == expected


def test_preview_keeps_valid_rows_and_explains_invalid_rate_unit(rate_database):
    headers = ["Tehsil", "Qanoongoi", "Mauza", "Classification", "Location", "Square No", "Killa No", "DC Rate", "Unit"]
    rows = [
        ["Tehsil A", "Qanoongoi A", "Mauza A", "Agricultural", "Farm A", "1", "2", "1000", "per Kanal"],
        ["Tehsil B", "Qanoongoi B", "Mauza B", "Agricultural", "Farm B", "3", "4", "2000", "per hectare"],
    ]
    mapping = dc_rate_service.suggested_column_mapping(headers)
    preview = dc_rate_service.preview_mapped_schedule(
        headers, rows, mapping, "rural_dc", duplicate_rule="default:rural_dc_overlap"
    )
    assert len(preview["valid_records"]) == 1
    assert preview["valid_records"][0]["rate_unit"] == "per_kanal"
    assert len(preview["issues"]) == 1
    issue = preview["issues"][0]
    assert issue["row"] == 3
    assert "per hectare" in issue["source_values"]
    assert "not recognized" in issue["message"]
    assert "per kanal" in issue["fix"]


def test_cadastral_search_matches_exact_numbers_inside_existing_ranges(rate_database):
    location_id, _ = dc_rate_service.create_location(**_location_values(
        square_no="1-5,6;7-9", kila_no="2, 12", khasra_no="20-22",
    ))
    # Simulate legacy rows saved before the importer began canonicalizing lists.
    with Session(rate_database) as session:
        location = session.get(PropertyLocation, location_id)
        location.square_no = "1-5,6;7-9"
        session.commit()
    dc_rate_service.create_dc_rate(location_id=location_id, **_rate_values())

    assert len(dc_rate_service.list_dc_rates(search="4")) == 1
    assert dc_rate_service.list_dc_rates(search="10") == []
    assert len(dc_rate_service.list_dc_rates(search="12")) == 1


def test_numeric_search_page_skips_substring_only_cadastral_candidates(rate_database):
    # "12" contains "2" as text but is not a complete cadastral token, so the
    # newer non-matching row must not fill a limited numeric-search page.
    substring_id, _ = dc_rate_service.create_location(**_location_values(
        name="North Field", mauza="North Field", land_type="rural",
        hadbast_number="", square_no="12",
    ))
    dc_rate_service.create_dc_rate(
        location_id=substring_id,
        **_rate_values(effective_from="2025-08-01"),
    )
    token_id, _ = dc_rate_service.create_location(**_location_values(
        name="South Field", mauza="South Field", land_type="rural",
        hadbast_number="", square_no="2",
    ))
    dc_rate_service.create_dc_rate(
        location_id=token_id,
        **_rate_values(effective_from="2025-07-01"),
    )

    assert [
        record["location_id"]
        for record in dc_rate_service.list_dc_rates(search="2", limit=1)
    ] == [token_id]
    assert [
        record["location_id"]
        for record in dc_rate_service.list_dc_rates(search="2", limit=1, offset=1)
    ] == []
    assert {
        record["location_id"]
        for record in dc_rate_service.list_dc_rates(search="2")
    } == {token_id}


def test_primary_schedule_filters_match_mauza_and_cadastral_tokens(rate_database):
    matching_id, _ = dc_rate_service.create_location(**_location_values(
        mauza="Canal View", square_no="12, 18", kila_no="4, 7",
    ))
    other_id, _ = dc_rate_service.create_location(**_location_values(
        mauza="Canal View Extension", square_no="120", kila_no="14",
    ))
    dc_rate_service.create_dc_rate(location_id=matching_id, **_rate_values())
    dc_rate_service.create_dc_rate(location_id=other_id, **_rate_values())

    assert [item["location_id"] for item in dc_rate_service.list_dc_rates(
        mauza="canal", square_no="12", kila_no="7"
    )] == [matching_id]
    assert [item["location_id"] for item in dc_rate_service.list_dc_rates(
        location_ids=[matching_id, other_id]
    )] == [other_id, matching_id]
    assert [
        item["location_id"]
        for item in dc_rate_service.list_dc_rates(location_ids=[matching_id])
    ] == [matching_id]


def test_mauza_choices_and_rate_summary_include_both_schedules(rate_database):
    mauza_id, _ = dc_rate_service.create_location(**_location_values(
        mauza="Example Mauza", square_no="2-4", kila_no="7, 8",
    ))
    dc_rate_service.create_dc_rate(
        location_id=mauza_id, **_rate_values(schedule_type="dc", rate_value="100", rate_unit="per_marla")
    )
    dc_rate_service.create_dc_rate(
        location_id=mauza_id, **_rate_values(schedule_type="fbr", rate_value="250", rate_unit="per_square_foot")
    )

    assert dc_rate_service.list_mauza_names() == ["Example Mauza"]
    assert dc_rate_service.list_mauza_names(search="example") == ["Example Mauza"]
    all_units = dc_rate_service.summarize_dc_rates(mauza="Example")
    assert all_units["count"] == 2
    assert Decimal(str(all_units["lowest"])) == Decimal("100")
    assert all_units["lowest_schedule"] == "dc"
    assert Decimal(str(all_units["highest"])) == Decimal("250")
    assert all_units["highest_schedule"] == "fbr"
    assert (all_units["lowest_square"], all_units["highest_square"]) == ("2", "4")
    assert (all_units["lowest_kila"], all_units["highest_kila"]) == ("7", "8")
    one_unit = dc_rate_service.summarize_dc_rates(mauza="Example", rate_unit="per_marla")
    assert one_unit["count"] == 1


def test_schedule_summary_reports_empty_results(rate_database):
    assert dc_rate_service.summarize_dc_rates(mauza="No such Mauza") == {
        "count": 0, "lowest": None, "highest": None,
        "lowest_square": None, "highest_square": None,
        "lowest_kila": None, "highest_kila": None,
    }


def test_qanoongoi_filters_and_location_hierarchy_summary(rate_database):
    alpha_id, _ = dc_rate_service.create_location(**_location_values(
        name="Alpha", mauza="Alpha", district="District A", tehsil="Tehsil A",
        qanoongoi="Qanoongoi One",
    ))
    beta_id, _ = dc_rate_service.create_location(**_location_values(
        name="Beta", mauza="Beta", district="District A", tehsil="Tehsil A",
        qanoongoi="Qanoongoi Two",
    ))
    gamma_id, _ = dc_rate_service.create_location(**_location_values(
        name="Gamma", mauza="Gamma", district="District A", tehsil="Tehsil B",
        qanoongoi="qanoongoi one",
    ))
    unassigned_id, _ = dc_rate_service.create_location(**_location_values(
        name="Delta", mauza="Delta", district="District A", tehsil="Tehsil B",
        qanoongoi="",
    ))
    for location_id in (alpha_id, beta_id, gamma_id, unassigned_id):
        dc_rate_service.create_dc_rate(location_id=location_id, **_rate_values())

    one_rates = dc_rate_service.list_dc_rates(qanoongoi="ONE")
    assert {record["location_id"] for record in one_rates} == {alpha_id, gamma_id}
    assert dc_rate_service.list_mauza_names(qanoongoi="one") == ["Alpha", "Gamma"]
    assert dc_rate_service.list_mauza_names(qanoongoi="Two") == ["Beta"]
    assert dc_rate_service.summarize_dc_rates(qanoongoi="one")["count"] == 2
    assert dc_rate_service.list_dc_rates(qanoongoi="not present") == []

    hierarchy = dc_rate_service.summarize_location_hierarchy()
    assert len(hierarchy) == 4
    tehsil_a = next(
        row for row in hierarchy
        if row["tehsil"] == "Tehsil A" and row["qanoongoi"] == "Qanoongoi One"
    )
    assert tehsil_a["mauza_count"] == 1
    assert tehsil_a["tehsil_count"] == 2
    assert tehsil_a["qanoongoi_count"] == 2
    unassigned = next(
        row for row in hierarchy
        if row["tehsil"] == "Tehsil B" and row["qanoongoi"] == ""
    )
    assert unassigned["mauza_count"] == 1
    assert unassigned["qanoongoi_count"] == 1


def test_list_location_picker_bounds_and_pins_locations(rate_database):
    alpha_id, _ = dc_rate_service.create_location(**_location_values(
        name="Alpha", mauza="Alpha", qanoongoi="Qanoongoi One",
    ))
    beta_id, _ = dc_rate_service.create_location(**_location_values(
        name="Beta", mauza="Beta", qanoongoi="qanoongoi one",
    ))
    gamma_id, _ = dc_rate_service.create_location(**_location_values(
        name="Gamma", mauza="Gamma",
    ))

    bounded = dc_rate_service.list_location_picker(limit=2)
    assert [item["id"] for item in bounded["locations"]] == [alpha_id, beta_id]
    assert bounded["has_more"] is True
    # Qanoongoi suggestions are case-insensitively unique, sorted, and non-empty.
    assert bounded["qanoongoi"] == ["Qanoongoi One"]

    pinned = dc_rate_service.list_location_picker(
        limit=2, include_ids=[gamma_id, alpha_id]
    )
    pinned_ids = [item["id"] for item in pinned["locations"]]
    # The first page keeps its order; pinned records outside it are appended.
    assert pinned_ids[:2] == [alpha_id, beta_id]
    assert pinned_ids[2] == gamma_id


def test_location_subquery_filter_matches_mauza_qanoongoi_and_land_type(rate_database):
    rural_id, _ = dc_rate_service.create_location(**_location_values(
        name="Ring Road", mauza="Ring Road", qanoongoi="Qanoongoi North",
        land_type="rural",
    ))
    dc_rate_service.create_dc_rate(
        location_id=rural_id, **_rate_values(rate_value="900")
    )
    urban_id, _ = dc_rate_service.create_location(**_location_values(
        name="Ring Road Urban", mauza="Ring Road", qanoongoi="Qanoongoi South",
        land_type="urban", square_no="5",
    ))
    dc_rate_service.create_dc_rate(
        location_id=urban_id, **_rate_values(rate_value="2500", rate_unit="per_square_foot")
    )

    assert [item["location_id"] for item in dc_rate_service.list_dc_rates(
        mauza="ring"
    )] == [urban_id, rural_id]
    assert [item["location_id"] for item in dc_rate_service.list_dc_rates(
        mauza="ring", qanoongoi="south"
    )] == [urban_id]
    assert [item["location_id"] for item in dc_rate_service.list_dc_rates(
        mauza="ring", land_type="rural"
    )] == [rural_id]
    assert [item["location_id"] for item in dc_rate_service.list_dc_rates(
        mauza="ring", land_type="urban", square_no="5"
    )] == [urban_id]
    summary = dc_rate_service.summarize_dc_rates(mauza="ring", qanoongoi="north")
    assert summary["count"] == 1
    assert summary["lowest"] == Decimal("900")
    type_summary = dc_rate_service.summarize_dc_rates(mauza="ring", land_type="urban")
    assert type_summary["highest_unit"] == "per_square_foot"
    # Free-text search keeps the joined candidate path and stays consistent.
    assert [item["location_id"] for item in dc_rate_service.list_dc_rates(
        mauza="ring", search="north"
    )] == [rural_id]


def test_large_mauza_search_pages_and_summaries_stay_consistent(rate_database):
    """Enough locations to force real pagination while filtering by Mauza."""
    created_ids = []
    for index in range(1, 8):
        location_id, _ = dc_rate_service.create_location(**_location_values(
            name=f"Village {index}", mauza="Spread Village",
            square_no=str(index),
        ))
        created_ids.append(location_id)
    for offset, location_id in enumerate(created_ids, start=1):
        dc_rate_service.create_dc_rate(
            location_id=location_id, **_rate_values(rate_value=str(offset * 100))
        )

    page_one = dc_rate_service.list_dc_rates(mauza="spread", limit=3)
    page_two = dc_rate_service.list_dc_rates(mauza="spread", limit=3, offset=3)
    assert len(page_one) == 3 and len(page_two) == 3
    assert page_one != page_two
    summary = dc_rate_service.summarize_dc_rates(mauza="spread")
    assert summary["count"] == 7
    assert summary["lowest"] == Decimal("100")
    assert summary["highest"] == Decimal("700")
    assert (summary["lowest_square"], summary["highest_square"]) == ("1", "7")


def test_mauza_cadastral_selection_summary_and_khewat_assignment(rate_database):
    context_values = _location_values(
        mauza="Selection Mauza", name="Selection Mauza", district="Selection District",
        qanoongoi="Qanoongoi A", patwar_circle="PC A", square_no="10, 12",
        kila_no="2-4",
    )
    location_id, _ = dc_rate_service.create_location(**context_values)
    dc_rate_service.create_dc_rate(location_id=location_id, **_rate_values(rate_value="100"))
    context = next(
        item for item in dc_rate_service.list_mauza_contexts()
        if item["mauza_name"] == "Selection Mauza"
    )

    assert dc_rate_service.list_mauza_cadastral_values(context, "square_no") == ["10", "12"]
    assert dc_rate_service.list_mauza_cadastral_values(context, "kila_no", search="3") == ["3"]
    result = dc_rate_service.summarize_mauza_cadastral_selection(
        context=context, square_numbers=["12"], kila_numbers=["3"],
    )
    assert result == {
        "count": 1,
        "lowest_square": "12", "highest_square": "12",
        "lowest_kila": "3", "highest_kila": "3",
        "lowest_rate": Decimal("100.00000000"),
        "highest_rate": Decimal("100.00000000"),
    }

    assignment = dc_rate_service.assign_cadastral_references_to_khewat(
        context=context,
        khewat_number="30/2/1",
        rhz_year="2019-20",
        square_numbers=["10", "12"],
        kila_numbers=["3", "4"],
    )
    assert assignment["added"] == 4
    assert assignment["existing"] == 0
    duplicate_assignment = dc_rate_service.assign_cadastral_references_to_khewat(
        context=context,
        khewat_number="30/2/1",
        rhz_year="2019-20",
        square_numbers=["10", "12"],
        kila_numbers=["3", "4"],
    )
    assert duplicate_assignment["added"] == 0
    assert duplicate_assignment["existing"] == 4
    saved_khewat = dc_rate_service.list_khewats_for_mauza(context)[0]
    assert saved_khewat["id"] == 1
    assert saved_khewat["khewat_number"] == "30/2/1"
    assert saved_khewat["rhz_year"] == "2019-20"
    assert saved_khewat["squares"] == ["10", "12"]
    assert saved_khewat["kilas"] == ["3", "4"]

    with Session(rate_database) as session:
        khewat = session.scalar(select(PropertyKhewat))
        session.delete(khewat)
        session.commit()
        assert session.scalars(select(KhewatReference)).all() == []


def test_khewat_year_normalization_search_and_edit(rate_database):
    context = {
        "province": "Punjab", "district": "District A", "tehsil": "Tehsil A",
        "qanoongoi": None, "patwar_circle": None, "mauza_name": "Year Mauza",
        "hadbast_number": "55",
    }
    location_id, _ = dc_rate_service.create_location(**_location_values(
        name="Year Mauza", mauza="Year Mauza", district="District A", tehsil="Tehsil A",
        hadbast_number="55", square_no="1, 2", kila_no="7, 8",
    ))
    dc_rate_service.create_dc_rate(location_id=location_id, **_rate_values())
    assert dc_rate_service.normalize_rhz_year("2000 - 2001") == "2000-01"
    assert dc_rate_service.normalize_rhz_year("2019/20") == "2019-20"
    assert dc_rate_service.normalize_khewat_size(" 12 - 4 - 0 ") == "12-4-0"
    with pytest.raises(dc_rate_service.DCRateServiceError, match="consecutive"):
        dc_rate_service.normalize_rhz_year("2019-22")

    dc_rate_service.assign_cadastral_references_to_khewat(
        context=context, khewat_number="30/2/1", rhz_year="2019-20",
        khewat_size="12-4-0", square_numbers=["1"], kila_numbers=["7"],
    )
    dc_rate_service.assign_cadastral_references_to_khewat(
        context=context, khewat_number="30/2/1", rhz_year="2020-21",
        square_numbers=["2"], kila_numbers=["8"],
    )
    assert dc_rate_service.latest_rhz_year(context) == "2020-21"
    assert [item["khewat_number"] for item in dc_rate_service.find_khewat_year_conflicts(
        context=context, rhz_year="2019 - 2020",
    )] == ["30/2/1"]
    assert dc_rate_service.find_khewat_year_conflicts(
        context=context, rhz_year="2021-22",
    ) == []
    assert len(dc_rate_service.search_khewats(
        mauza="Year Mauza", district="District A", tehsil="Tehsil A",
        khewat_number="30/2/1",
    )) == 2

    old_year_record = next(
        item for item in dc_rate_service.list_khewats_for_mauza(context)
        if item["rhz_year"] == "2019-20"
    )
    outcome = dc_rate_service.update_khewat_record(
        khewat_id=old_year_record["id"], khewat_number="30/2/1",
        rhz_year="2000 - 2001", khewat_size="13-0-0",
        square_numbers=["2"], kila_numbers=[],
    )
    assert outcome["rhz_year"] == "2000-01"
    assert outcome["removed"] == 2
    updated = dc_rate_service.get_khewat_details(old_year_record["id"])
    assert updated["khewat_size"] == "13-0-0"
    assert updated["squares"] == ["2"]
    assert updated["kilas"] == []


def test_saved_khewats_are_in_mauza_choices_and_have_searchable_summaries(rate_database):
    location_id, _ = dc_rate_service.create_location(**_location_values(
        name="Saved Only Mauza", mauza="Saved Only Mauza", district="District S",
        tehsil="Tehsil S", square_no="8", kila_no="2", khasra_no="14/2",
    ))
    dc_rate_service.create_dc_rate(location_id=location_id, **_rate_values(rate_unit="per_marla"))
    context = next(item for item in dc_rate_service.list_mauza_contexts()
                   if item["mauza_name"] == "Saved Only Mauza")
    dc_rate_service.assign_cadastral_references_to_khewat(
        context=context, khewat_number="K-88", rhz_year="2023-24",
        square_numbers=["8"], kila_numbers=["2"],
    )
    rows = dc_rate_service.search_khewats(mauza="Saved Only", khewat_number="K-88")
    assert len(rows) == 1
    summary = dc_rate_service.list_khewat_summary(rows[0]["id"])
    square_summary = next(item for item in summary if item["square_no"] == "8")
    assert square_summary["khasra_no"] == "14/2"
    assert square_summary["schedule"].startswith("DC valuation: Rs. ")
    assert square_summary["unit"] == "Marla"
    with Session(rate_database) as session:
        session.query(DCValuationRate).delete()
        session.commit()
    assert "Saved Only Mauza" in dc_rate_service.list_saved_khewat_mauzas(search="Only")
    assert "K-88" in dc_rate_service.list_saved_khewat_numbers(mauza="Saved Only", search="88")
    assert dc_rate_service.list_mauza_contexts(search="Saved Only")[0]["mauza_name"] == "Saved Only Mauza"


def test_khewat_import_preview_export_and_atomic_reference_validation(rate_database):
    preview = dc_rate_service.preview_khewat_import(
        ["Mauza", "Khewat no", "RHZ year", "Square", "Kila"],
        [["Import Mauza", "K-1", "2000 - 2001", "1", ""],
         ["", "K-2", "bad", "", ""]],
        {"mauza": 0, "khewat_number": 1, "rhz_year": 2, "square_no": 3, "kila_no": 4},
    )
    assert preview["valid_records"][0]["rhz_year"] == "2000-01"
    assert preview["issues"][0]["row"] == 3
    template = dc_rate_service.export_khewat_import_template("xlsx")
    headers, rows = dc_rate_service.read_schedule_spreadsheet(template, "template.xlsx")
    assert headers[:4] == ["Mauza", "District", "Tehsil", "Khewat number"]
    assert rows == []

    context = {
        "province": "Punjab", "district": "", "tehsil": "", "qanoongoi": "",
        "patwar_circle": "", "mauza_name": "Import Mauza", "hadbast_number": "",
    }
    with pytest.raises(dc_rate_service.DCRateServiceError, match="not listed"):
        dc_rate_service.import_khewat_assignments([{
            "context": context, "khewat_number": "K-1", "rhz_year": "2000-01",
            "square_numbers": ["unknown"], "kila_numbers": [],
        }])
    assert dc_rate_service.search_khewats(mauza="Import Mauza") == []
    location_id, _ = dc_rate_service.create_location(**_location_values(
        name="Import Mauza", mauza="Import Mauza", district="Import District",
        tehsil="Import Tehsil", square_no="5", kila_no="9",
    ))
    dc_rate_service.create_dc_rate(location_id=location_id, **_rate_values())
    import_context = next(item for item in dc_rate_service.list_mauza_contexts()
                          if item["mauza_name"] == "Import Mauza")
    outcome = dc_rate_service.import_khewat_assignments([{
        "context": import_context, "khewat_number": "K-1", "rhz_year": "2000-01",
        "square_numbers": ["5"], "kila_numbers": ["9"],
    }])
    assert outcome == {"created": 1, "references_added": 2, "assignments": 1}
    for extension in ("pdf", "csv", "xlsx", "ods"):
        payload = dc_rate_service.export_khewat_summary([{
            "serial_no": 1, "mauza": "M", "square_no": "1", "kila_no": "2",
            "khasra_no": "3", "unit": "per_marla", "schedule": "DC",
        }], extension)
        assert payload.startswith(b"%PDF-" if extension == "pdf" else b"PK" if extension == "xlsx" else b"\x50\x4b" if extension == "ods" else b"\xef\xbb\xbf")


def _insert_raw_dc_rate(location_id, **overrides):
    values = dc_rate_service._rate_data(_rate_values(**overrides))
    with Session(dc_rate_service.SessionLocal.kw["bind"]) as session:
        rate = DCValuationRate(location_id=location_id, **values)
        session.add(rate)
        session.flush()
        record_id = rate.id
        session.commit()
    return record_id


def test_rural_duplicate_scan_and_export_before_removal(rate_database, tmp_path, monkeypatch):
    location_id, _ = dc_rate_service.create_location(**_location_values(
        land_type="rural", qanoongoi="Qanoongoi 1", square_no="4",
        kila_no="8", khasra_no="10-12", locality="North field",
    ))
    survivor_id = _insert_raw_dc_rate(location_id, rate_value="100")
    duplicate_id = _insert_raw_dc_rate(location_id, rate_value="120")
    monkeypatch.setattr(dc_rate_service, "_documents_directory", lambda: tmp_path / "Documents")

    preview = dc_rate_service.find_duplicate_dc_rates("default:rural_dc_overlap")
    assert [record["id"] for record in preview] == [duplicate_id]
    assert preview[0]["duplicate_of"] == survivor_id

    removed_count, report_path = dc_rate_service.remove_duplicate_dc_rates_with_report(
        "csv", "default:rural_dc_overlap"
    )

    assert removed_count == 1
    assert report_path.parent == tmp_path / "Documents"
    assert report_path.exists()
    report = report_path.read_text(encoding="utf-8-sig")
    assert "Record ID" in report and "Duplicate of record ID" in report
    assert str(duplicate_id) in report and str(survivor_id) in report
    assert str(Decimal("120")) in report
    assert [record["id"] for record in dc_rate_service.list_dc_rates()] == [survivor_id]


def test_urban_dc_and_fbr_duplicate_scan_requires_all_values_to_match(rate_database):
    urban_id, _ = dc_rate_service.create_location(**_location_values(
        name="Urban Block", mauza=None, land_type="urban", locality="Block A",
    ))
    urban_ids = [
        _insert_raw_dc_rate(urban_id, schedule_type="dc", rate_value=value)
        for value in ("200", "200", "250")
    ]
    fbr_ids = [
        _insert_raw_dc_rate(urban_id, schedule_type="fbr", rate_value="25", rate_unit="per_square_foot")
        for _ in range(2)
    ]

    duplicate_ids = [
        record["id"]
        for record in dc_rate_service.find_duplicate_dc_rates("default:any_exact")
    ]

    assert duplicate_ids == [urban_ids[1], fbr_ids[1]]


@pytest.mark.parametrize("file_format", ["xlsx", "ods"])
def test_duplicate_reports_support_spreadsheet_formats(rate_database, tmp_path, monkeypatch, file_format):
    location_id, _ = dc_rate_service.create_location(**_location_values(
        land_type="urban", name="Format Test", mauza=None,
    ))
    _insert_raw_dc_rate(location_id)
    _insert_raw_dc_rate(location_id)
    monkeypatch.setattr(dc_rate_service, "_documents_directory", lambda: tmp_path / "Documents")

    removed_count, report_path = dc_rate_service.remove_duplicate_dc_rates_with_report(
        file_format, "default:urban_dc_overlap"
    )

    assert removed_count == 1
    assert report_path.suffix == f".{file_format}"
    headers, rows = dc_rate_service.read_schedule_spreadsheet(report_path.read_bytes(), report_path.name)
    assert "Duplicate of record ID" in headers
    assert len(rows) == 1


def test_duplicate_removal_rejects_unsupported_report_before_deleting(rate_database):
    location_id, _ = dc_rate_service.create_location(**_location_values(land_type="urban"))
    first = _insert_raw_dc_rate(location_id)
    _insert_raw_dc_rate(location_id)

    with pytest.raises(dc_rate_service.DCRateServiceError, match="Choose CSV"):
        dc_rate_service.remove_duplicate_dc_rates_with_report(
            "pdf", "default:urban_dc_overlap"
        )

    assert len(dc_rate_service.list_dc_rates()) == 2


def test_rural_urban_schedule_filters_and_cadastral_suggestions(rate_database):
    rural_id, _ = dc_rate_service.create_location(**_location_values(
        land_type="rural", square_no="12, 13", kila_no="4, 8", khasra_no="100-102",
    ))
    urban_id, _ = dc_rate_service.create_location(**_location_values(
        name="Urban Locality", mauza="Urban Locality", land_type="urban",
        square_no="22", kila_no="2", khasra_no="200",
    ))
    dc_rate_service.create_dc_rate(location_id=rural_id, **_rate_values(schedule_type="dc"))
    dc_rate_service.create_dc_rate(location_id=urban_id, **_rate_values(schedule_type="dc"))
    dc_rate_service.create_dc_rate(location_id=urban_id, **_rate_values(
        schedule_type="fbr", rate_value="250", rate_unit="per_square_foot",
    ))

    rural_rates = dc_rate_service.list_dc_rates(schedule_type="dc", land_type="rural")
    urban_rates = dc_rate_service.list_dc_rates(schedule_type="dc", land_type="urban")
    assert [row["location_id"] for row in rural_rates] == [rural_id]
    assert [row["location_id"] for row in urban_rates] == [urban_id]
    assert dc_rate_service.list_dc_rates(schedule_type="fbr")[0]["schedule_type"] == "fbr"
    assert dc_rate_service.list_cadastral_values("square_no") == ["12", "13", "22"]
    assert dc_rate_service.list_cadastral_values("square_no", search="1") == ["12", "13"]
    assert dc_rate_service.list_cadastral_values("kila_no") == ["2", "4", "8"]
    assert dc_rate_service.list_cadastral_values("khasra_no") == ["100", "101", "102", "200"]


def test_cadastral_suggestions_respect_location_and_square_context(rate_database):
    locations = [
        _location_values(
            name="Madar Square 10 Kila 1", mauza="Madar", qanoongoi="Jandiala",
            square_no="10", kila_no="1", khasra_no="101",
        ),
        _location_values(
            name="Madar Square 10 Kila 2", mauza="Madar", qanoongoi="Jandiala",
            square_no="10", kila_no="2", khasra_no="102",
        ),
        _location_values(
            name="Madar Square 10 Kila 3", mauza="Madar", qanoongoi="Jandiala",
            square_no="10", kila_no="3", khasra_no="103",
        ),
        _location_values(
            name="Madar Square 11", mauza="Madar", qanoongoi="Jandiala",
            square_no="11", kila_no="9", khasra_no="109",
        ),
        _location_values(
            name="Madar Other Qanoongoi", mauza="Madar", qanoongoi="Other",
            square_no="10", kila_no="8", khasra_no="108",
        ),
        _location_values(
            name="Other Mauza", mauza="Other Mauza", qanoongoi="Jandiala",
            square_no="12", kila_no="7", khasra_no="107",
        ),
    ]
    for values in locations:
        location_id, _ = dc_rate_service.create_location(**values)
        dc_rate_service.create_dc_rate(location_id=location_id, **_rate_values())

    assert dc_rate_service.list_cadastral_values(
        "square_no", qanoongoi="jandiala", mauza="madar",
    ) == ["10", "11"]
    assert dc_rate_service.list_cadastral_values(
        "kila_no", qanoongoi="Jandiala", mauza="Madar", square_no="10",
    ) == ["1", "2", "3"]
    assert dc_rate_service.list_cadastral_values(
        "kila_no", qanoongoi="Jandiala", mauza="Madar", square_no="11",
    ) == ["9"]
    assert dc_rate_service.list_cadastral_values(
        "kila_no", qanoongoi="Jandiala", mauza="Madar", square_no="10", search="2",
    ) == ["2"]
    assert dc_rate_service.list_cadastral_values(
        "khasra_no", qanoongoi="Jandiala", mauza="Madar", square_no="10", kila_no="2",
    ) == ["102"]


def test_schedule_export_supports_csv_xlsx_and_ods(rate_database):
    location_id, _ = dc_rate_service.create_location(**_location_values(
        land_type="rural", qanoongoi="Qanoongoi 2", patwar_circle="PC 4",
        square_no="9", kila_no="17", khasra_no="88",
    ))
    dc_rate_service.create_dc_rate(
        location_id=location_id,
        **_rate_values(schedule_type="dc", published_year="2025", classification="Agricultural"),
    )
    record = dc_rate_service.list_dc_rates()[0]
    for extension in ("csv", "xlsx", "ods"):
        payload = dc_rate_service.export_schedule_records([record], extension)
        assert payload
        if extension == "csv":
            assert "Qanoongoi" in payload.decode("utf-8-sig")
            assert "2025" in payload.decode("utf-8-sig")
        elif extension == "xlsx":
            headers, rows = dc_rate_service.read_schedule_spreadsheet(payload, "export.xlsx")
            assert "Qanoongoi" in headers
            assert "2025" in rows[0]
        else:
            headers, rows = dc_rate_service.read_schedule_spreadsheet(payload, "export.ods")
            assert "Qanoongoi" in headers
            assert "2025" in rows[0]


def test_ods_schedule_file_is_read_for_column_mapping():
    from odf.opendocument import OpenDocumentSpreadsheet
    from odf.table import Table, TableRow, TableCell
    from odf.text import P

    document = OpenDocumentSpreadsheet()
    sheet = Table(name="Rates")
    for values in (["Mauza", "Rate", "Rate Unit"], ["Example Mauza", "25", "per_marla"]):
        row = TableRow()
        for value in values:
            cell = TableCell()
            cell.addElement(P(text=value))
            row.addElement(cell)
        sheet.addElement(row)
    document.spreadsheet.addElement(sheet)
    payload = BytesIO()
    document.write(payload)

    headers, rows = dc_rate_service.read_schedule_spreadsheet(payload.getvalue(), "rates.ods")
    assert headers == ["Mauza", "Rate", "Rate Unit"]
    assert rows == [["Example Mauza", "25", "per_marla"]]


def test_additive_valuation_migration_preserves_legacy_location(tmp_path, monkeypatch):
    import core.database as database

    engine = create_engine(f"sqlite:///{(tmp_path / 'legacy.sqlite3').as_posix()}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE property_locations (id INTEGER PRIMARY KEY, name VARCHAR(200) NOT NULL)"))
        connection.execute(text("INSERT INTO property_locations (id, name) VALUES (1, 'Old Mauza')"))
        connection.execute(text("CREATE TABLE dc_valuation_rates (id INTEGER PRIMARY KEY, property_type VARCHAR(100) NOT NULL, effective_from DATE)"))
        connection.execute(text("INSERT INTO dc_valuation_rates (id, property_type, effective_from) VALUES (1, 'Residential', '2024-05-02')"))
    monkeypatch.setattr(database, "engine", engine)
    database.migrate_valuation_columns()
    with engine.connect() as connection:
        location = connection.execute(text("SELECT name, land_type FROM property_locations WHERE id=1")).one()
        location_columns = {column["name"] for column in inspect(engine).get_columns("property_locations")}
        rate_columns = {column["name"] for column in inspect(engine).get_columns("dc_valuation_rates")}
        schedule_column = next(c for c in inspect(engine).get_columns("dc_valuation_rates") if c["name"] == "schedule_type")
        assert location == ("Old Mauza", None)
        assert "serial_no" in location_columns
        assert {"structure_rate", "per_marla_value"} <= rate_columns
        assert schedule_column["default"] == "'dc'"
        year = connection.execute(text("SELECT published_year FROM dc_valuation_rates WHERE id=1")).scalar_one()
        assert year == 2024
    engine.dispose()



def test_duplicate_rule_profiles_are_builtin_or_persisted_custom_rows(rate_database):
    profiles = dc_rate_service.list_duplicate_rule_profiles()
    references = {profile["reference"] for profile in profiles}
    assert references == {
        "default:rural_dc_overlap",
        "default:rural_dc_exact",
        "default:urban_dc_overlap",
        "default:fbr_overlap",
        "default:any_exact",
        "default:all_columns_exact",
    }
    assert inspect(rate_database).has_table("dc_rate_duplicate_profiles")

    created = dc_rate_service.create_duplicate_rule_profile(
        name="My custom rule",
        schedule_scope="any",
        match_fields=["name", "rate_unit"],
        date_mode="ignore",
        import_action="import",
        description="Synthetic test rule",
    )
    assert created["reference"].startswith("custom:")
    assert created["is_builtin"] is False
    assert created["match_fields"] == ["name", "rate_unit"]

    with pytest.raises(DuplicateRuleError, match="already exists"):
        dc_rate_service.create_duplicate_rule_profile(
            name="my custom rule",
            schedule_scope="any",
            match_fields=["name"],
            date_mode="exact",
            import_action="skip",
        )
    with pytest.raises(DuplicateRuleError, match="at least one"):
        dc_rate_service.create_duplicate_rule_profile(
            name="No fields",
            schedule_scope="any",
            match_fields=[],
            date_mode="exact",
            import_action="skip",
        )

    updated = dc_rate_service.update_duplicate_rule_profile(
        created["custom_id"],
        name="Renamed custom rule",
        schedule_scope="urban_dc",
        match_fields=["name", "district"],
        date_mode="exact",
        import_action="skip",
        description="Updated",
    )
    assert updated["name"] == "Renamed custom rule"
    assert updated["schedule_scope"] == "urban_dc"
    assert updated["import_action"] == "skip"

    dc_rate_service.delete_duplicate_rule_profile(created["custom_id"])
    remaining = dc_rate_service.list_duplicate_rule_profiles()
    assert created["reference"] not in {profile["reference"] for profile in remaining}
    with pytest.raises(DuplicateRuleError, match="not found"):
        dc_rate_service.delete_duplicate_rule_profile(created["custom_id"])


def _duplicate_match_fields():
    return [
        "name", "province", "district", "tehsil", "mauza", "hadbast_number",
        "locality", "property_type", "classification", "floor", "rate_unit",
    ]


def test_import_always_uses_exact_all_columns_and_ignores_rule_actions(rate_database):
    workbook_row = _excel_row("Duplicate Mauza", 9000)
    seeded = dc_rate_service.preview_dc_rate_workbook(
        _workbook_bytes([workbook_row])
    )
    assert dc_rate_service.import_dc_rate_records(
        seeded["valid_records"]
    )["imported_count"] == 1

    # A custom rule that would otherwise allow duplicates must not override the
    # automatic all-column exact rule used by schedule imports.
    rule = dc_rate_service.create_duplicate_rule_profile(
        name="Permissive import rule",
        schedule_scope="any",
        match_fields=_duplicate_match_fields(),
        date_mode="ignore",
        import_action="import",
    )
    preview = dc_rate_service.preview_dc_rate_workbook(
        _workbook_bytes([workbook_row]),
        duplicate_rule=rule["reference"],
    )
    assert preview["valid_records"] == []
    assert preview["warnings"] == []
    assert preview["issues"][0]["kind"] == "exact duplicate in database"
    assert preview["duplicates_removed"] == 1
    outcome = dc_rate_service.import_dc_rate_records(
        preview["valid_records"], duplicate_rule=rule["reference"]
    )
    assert outcome["imported_count"] == 0
    assert len(dc_rate_service.list_dc_rates()) == 1


def test_duplicate_rule_date_modes_select_exact_overlap_or_ignore(rate_database):
    location_id, _ = dc_rate_service.create_location(**_location_values(
        land_type="rural",
        qanoongoi="Qanoongoi 1",
        square_no="4",
        kila_no="8",
        khasra_no="10",
        locality="North field",
    ))
    survivor_id = _insert_raw_dc_rate(
        location_id, rate_value="100",
        effective_from="2024-01-01", effective_to="2024-12-31",
    )
    duplicate_id = _insert_raw_dc_rate(
        location_id, rate_value="100",
        effective_from="2025-01-01", effective_to="2025-12-31",
    )

    assert dc_rate_service.find_duplicate_dc_rates("default:rural_dc_exact") == []
    assert dc_rate_service.find_duplicate_dc_rates("default:rural_dc_overlap") == []

    ignore_rule = dc_rate_service.create_duplicate_rule_profile(
        name="Rural same row ignoring dates",
        schedule_scope="rural_dc",
        match_fields=[
            "name", "province", "district", "tehsil", "qanoongoi",
            "patwar_circle", "mauza", "hadbast_number", "square_no",
            "kila_no", "khasra_no", "locality", "property_type",
            "classification", "floor", "rate_unit",
        ],
        date_mode="ignore",
        import_action="skip",
    )
    duplicates = dc_rate_service.find_duplicate_dc_rates(ignore_rule["reference"])
    assert [record["id"] for record in duplicates] == [duplicate_id]
    assert duplicates[0]["duplicate_of"] == survivor_id


def test_import_ignores_a_mismatched_legacy_duplicate_rule(rate_database):
    headers = [
        "District", "Tehsil", "Serial No", "Revenue Circle", "Property Area",
        "Classification", "Location", "Location (Square No.)",
        "Value of property per Square Ft (in Rs.)",
    ]
    mapping = dc_rate_service.suggested_column_mapping(headers)
    row = [
        "District C", "Tehsil C", "9", "Circle C", "Sector C", "Commercial",
        "Market Road", "22", "123.50",
    ]
    preview = dc_rate_service.preview_mapped_schedule(
        headers, [row], mapping, "fbr",
        duplicate_rule="default:rural_dc_overlap",
    )
    assert len(preview["valid_records"]) == 1
    assert dc_rate_service.import_dc_rate_records(
        preview["valid_records"],
        duplicate_rule="default:rural_dc_overlap",
    )["imported_count"] == 1

    preview = dc_rate_service.preview_dc_rate_workbook(
        _workbook_bytes([_excel_row("Scope mismatch", 5000)]),
        duplicate_rule="default:rural_dc_overlap",
    )
    assert len(preview["valid_records"]) == 1


def test_db_backed_location_hints_and_matching_use_current_rows(rate_database):
    first_id, _created = dc_rate_service.create_location(**_location_values(
        name="Chak 1", mauza="Chak 1", land_type="rural",
        district="District A", tehsil="Tehsil A",
        square_no="2", kila_no="12", locality="Farm 1",
    ))
    dc_rate_service.create_dc_rate(location_id=first_id, **_rate_values())
    second_id, _created = dc_rate_service.create_location(**_location_values(
        name="Chak 2", mauza="Chak 2", land_type="rural",
        district="District A", tehsil="Tehsil A",
        square_no="12", kila_no="2", locality="Farm 2",
    ))
    dc_rate_service.create_dc_rate(location_id=second_id, **_rate_values())
    blank_type_id, _created = dc_rate_service.create_location(**_location_values(
        name="Town One", mauza="Town One", land_type=None,
        district="District U", tehsil="Tehsil U", locality="Market",
    ))
    dc_rate_service.create_dc_rate(location_id=blank_type_id, **_rate_values())

    hints = dc_rate_service.list_location_hint_values(
        land_type="rural",
        context={"district": "District A", "mauza": "Chak"},
        fields=["mauza", "square_no", "locality"],
        limit=10,
    )
    assert hints["mauza"] == ["Chak 1", "Chak 2"]
    assert set(hints["square_no"]) == {"2", "12"}
    assert {"Chak 1", "Chak 2", "Farm 1", "Farm 2"}.issubset(hints["locality"])

    matches = dc_rate_service.find_schedule_location_matches(
        land_type="rural",
        context={"mauza": "Chak 1", "square_no": "2"},
        limit=10,
    )
    assert [record["id"] for record in matches] == [first_id]

    # Cadastral values match complete tokens, so square 2 must not match 12.
    assert dc_rate_service.find_schedule_location_matches(
        land_type="rural",
        context={"mauza": "Chak 2", "square_no": "2"},
        limit=10,
    ) == []

    # Legacy blank land types retain the calculator's urban convention.
    urban_matches = dc_rate_service.find_schedule_location_matches(
        land_type="urban",
        context={"mauza": "Town One"},
        limit=10,
    )
    assert [record["id"] for record in urban_matches] == [blank_type_id]
    assert dc_rate_service.find_schedule_location_matches(
        land_type="rural",
        context={"mauza": "Town One"},
        limit=10,
    ) == []

    # Hints are queried from the current database, not a page-load snapshot.
    new_id, _created = dc_rate_service.create_location(**_location_values(
        name="New Chak", mauza="New Chak", land_type="rural",
        district="District A", tehsil="Tehsil A",
    ))
    dc_rate_service.create_dc_rate(location_id=new_id, **_rate_values())
    refreshed = dc_rate_service.list_location_hint_values(
        land_type="rural",
        context={"mauza": "New"},
        fields=["mauza"],
        limit=10,
    )
    assert refreshed["mauza"] == ["New Chak"]


def test_kila_match_ignores_case_and_separators(rate_database):
    """Alphanumeric Kila references in a/b/c/d format match typed shortcuts.

    Published rural schedules write Kila values like "3/B"; the calculator
    search must find them from typed "3b" (and "3-B") without altering the
    stored schedule text.
    """
    location_id, _created = dc_rate_service.create_location(**_location_values(
        name="Kila Chak", mauza="Kila Chak", land_type="rural",
        district="District A", tehsil="Tehsil A",
        square_no="3", kila_no="3/B",
    ))
    dc_rate_service.create_dc_rate(location_id=location_id, **_rate_values())

    location_row = {
        "name": "Kila Chak", "square_no": "3", "kila_no": "3/B", "khasra_no": "",
        "locality": None, "mauza": "Kila Chak",
    }
    # Direct matcher: stored "3/B" matches typed "3b" and "3 - B", stored
    # "3/b" matches typed "3/B", and unrelated Kila values stay rejected.
    assert dc_rate_service._location_matches_context(
        location_row, {"kila_no": "3b"})
    assert dc_rate_service._location_matches_context(
        location_row, {"kila_no": "3 - B"})
    assert dc_rate_service._location_matches_context(
        dict(location_row, kila_no="3/b"), {"kila_no": "3/B"})
    assert not dc_rate_service._location_matches_context(
        location_row, {"kila_no": "4b"})

    for typed_kila in ("3b", "3/B", "3/b"):
        matches = dc_rate_service.find_schedule_location_matches(
            land_type="rural",
            context={"mauza": "Kila Chak", "kila_no": typed_kila},
            limit=10,
        )
        assert [record["id"] for record in matches] == [location_id]


def test_expand_references_breaks_lists_and_ranges_to_individual_values():
    """The public display helper expands stored references like the importer.

    Display code may call it to show one choice per individual value while
    the stored record keeps the compact imported form.
    """
    assert dc_rate_service.expand_references("1-3, 5") == ["1", "2", "3", "5"]
    assert dc_rate_service.expand_references("3/B") == ["3/B"]
    assert dc_rate_service.expand_references("") == []
    with pytest.raises(dc_rate_service.DCRateServiceError):
        dc_rate_service.expand_references("1-9000")


def test_rural_location_hints_follow_hierarchy_parents(rate_database):
    """With hierarchy=True each level is filtered only by its parents.

    A typed Square must not hide valid Mauza suggestions, and Kila
    suggestions must not depend on a Mauza the user has not typed.
    """
    for values in (
        dict(name="Chak 1", mauza="Chak 1", land_type="rural",
             district="District A", tehsil="Tehsil A", qanoongoi="Q A",
             square_no="2", kila_no="12"),
        dict(name="Chak 2", mauza="Chak 2", land_type="rural",
             district="District A", tehsil="Tehsil A", qanoongoi="Q B",
             square_no="12", kila_no="2"),
        dict(name="Chak 3", mauza="Chak 3", land_type="rural",
             district="District A", tehsil="Tehsil A", qanoongoi="Q C",
             square_no="5", kila_no="9"),
        dict(name="Chak 9", mauza="Chak 9", land_type="rural",
             district="District B", tehsil="Tehsil B", qanoongoi="Q D",
             square_no="7", kila_no="7"),
    ):
        location_id, _created = dc_rate_service.create_location(
            **_location_values(**values))
        dc_rate_service.create_dc_rate(location_id=location_id, **_rate_values())

    context = {
        "district": "District A", "tehsil": "Tehsil A",
        "mauza": "Chak 1", "square_no": "2",
    }
    hints = dc_rate_service.list_location_hint_values(
        land_type="rural", context=context, limit=10, hierarchy=True)
    # Tehsil suggestions come from the district only.
    assert hints["tehsil"] == ["Tehsil A"]
    # Qanoongoi suggestions cover the whole Tehsil, ignoring Mauza/Square.
    assert hints["qanoongoi"] == ["Q A", "Q B", "Q C"]
    # Mauza suggestions cover the whole Tehsil as candidates; the typed
    # Square (not a parent of Mauza) must not filter them. The typed Mauza
    # text itself still narrows its own type-ahead suggestions.
    assert hints["mauza"] == ["Chak 1"]
    # Square suggestions are scoped to the selected Mauza.
    assert hints["square_no"] == ["2"]
    # Kila suggestions are scoped to the selected Square.
    assert hints["kila_no"] == ["12"]

    # Without the hierarchy flag the legacy behaviour applies: a typed child
    # value (the Square) also filters the levels above it, so only mauzas
    # holding that square are suggested. The hierarchy flag keeps Mauza
    # suggestions covering the whole Tehsil.
    legacy_context = {
        "district": "District A", "tehsil": "Tehsil A", "square_no": "2",
    }
    stale = dc_rate_service.list_location_hint_values(
        land_type="rural", context=legacy_context,
        fields=["mauza"], limit=10,
    )
    assert stale["mauza"] == ["Chak 1", "Chak 2"]
    fresh = dc_rate_service.list_location_hint_values(
        land_type="rural", context=legacy_context,
        fields=["mauza"], limit=10, hierarchy=True,
    )
    assert fresh["mauza"] == ["Chak 1", "Chak 2", "Chak 3"]
