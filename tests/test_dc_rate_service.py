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
from core.models import DCValuationRate, PropertyLocation
from services import dc_rate_service


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
    location_id, _ = dc_rate_service.create_location(
        **_location_values(
            name="Already present",
            mauza="Already present",
            district="Workbook District",
            tehsil="Workbook Tehsil",
            hadbast_number="456",
        )
    )
    dc_rate_service.create_dc_rate(
        location_id=location_id, **_rate_values()
    )

    rows = [
        _excel_row("New Mauza", 5000),
        _excel_row("New Mauza", 9000),  # Duplicate key inside the workbook.
        _excel_row("Bad Rate", -50),
        _excel_row("Bad Unit", 20, unit="per_square_furlong"),
        _excel_row("Already present", 6000),  # Conflicts with persisted row.
    ]
    preview = dc_rate_service.preview_dc_rate_workbook(_workbook_bytes(rows))
    assert len(preview["valid_records"]) == 1
    assert [issue["kind"] for issue in preview["issues"]] == [
        "duplicate in workbook",
        "invalid",
        "invalid",
        "duplicate in database",
    ]

    with pytest.raises(dc_rate_service.DCRateServiceError, match="Missing required"):
        dc_rate_service.preview_dc_rate_workbook(
            _workbook_bytes([["x", "y"]], headers=["Name", "Amount"])
        )


def test_excel_import_adds_only_unique_rows_and_never_overwrites(rate_database):
    location_id, _ = dc_rate_service.create_location(**_location_values())
    existing_id = dc_rate_service.create_dc_rate(
        location_id=location_id, **_rate_values()
    )
    preview = dc_rate_service.preview_dc_rate_workbook(
        _workbook_bytes([_excel_row("Imported Mauza", 8000)])
    )
    outcome = dc_rate_service.import_dc_rate_records(preview["valid_records"])

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

    outcome = dc_rate_service.import_dc_rate_records(candidates)

    assert outcome == {"imported_count": 1200, "issues": []}
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


def test_csv_column_mapping_imports_schedule_dimensions(rate_database):
    payload = (
        "District,Tehsil,Rural or Urban,Qanoongoi,Patwar Circle,Committee/Town,Revenue Circle,Floor,"
        "Property Area,Mauza,Square No,Kila No,Khasra No,Land Classification,Location,Rate,Rate Unit,Published Year\n"
        "Example District,Example Tehsil,Urban,Qanoongoi 1,,Town A,Circle 1,Ground,Area 1,Mauza 1,12,3,K-12,Residential,Main Road,15000,per_marla,2025\n"
    ).encode("utf-8")
    headers, rows = dc_rate_service.read_schedule_spreadsheet(payload, "rates.csv")
    mapping = dc_rate_service.suggested_column_mapping(headers)
    mapping["location_name"] = headers.index("Mauza")
    preview = dc_rate_service.preview_mapped_schedule(headers, rows, mapping, "fbr")
    assert len(preview["valid_records"]) == 1
    record = preview["valid_records"][0]
    assert record["schedule_type"] == "fbr"
    assert record["location"]["revenue_circle"] == "Circle 1"
    assert record["published_year"] == 2025
    assert record["location"]["qanoongoi"] == "Qanoongoi 1"
    assert record["location"]["square_no"] == "12"
    result = dc_rate_service.import_dc_rate_records(preview["valid_records"])
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
    mapping = dc_rate_service.suggested_column_mapping(headers)
    preview = dc_rate_service.preview_mapped_schedule(headers, [row], mapping, profile)
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
    assert dc_rate_service.import_dc_rate_records(preview["valid_records"])["imported_count"] == 1
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
    preview = dc_rate_service.preview_mapped_schedule(headers, [row], mapping, "rural_dc")
    assert preview["valid_records"] == []
    assert preview["issues"][0]["row"] == 2
    assert "tehsil" in preview["issues"][0]["message"]


def test_rural_schedule_accepts_khasra_without_square_or_kila(rate_database):
    headers = ["Tehsil", "Qanoongoi", "Mauza", "Classification", "Location", "Square No", "Killa No", "Khasra No", "DC Rate", "Unit"]
    row = ["Tehsil A", "Qanoongoi A", "Mauza A", "Agricultural", "Farm", "", "", "1-5,6;7-9", "1000", "per_marla"]
    mapping = dc_rate_service.suggested_column_mapping(headers)
    preview = dc_rate_service.preview_mapped_schedule(headers, [row], mapping, "rural_dc")
    assert preview["issues"] == []
    candidate = preview["valid_records"][0]
    assert candidate["location"]["square_no"] is None
    assert candidate["location"]["kila_no"] is None
    assert candidate["location"]["khasra_no"] == "1, 2, 3, 4, 5, 6, 7, 8, 9"
    dc_rate_service.import_dc_rate_records([candidate])
    assert dc_rate_service.list_dc_rates(search="4")[0]["khasra_no"] == "1, 2, 3, 4, 5, 6, 7, 8, 9"


def test_reference_range_normalization_validates_direction_and_size():
    assert dc_rate_service._normalize_reference("1-5,6;7-9") == "1, 2, 3, 4, 5, 6, 7, 8, 9"
    assert dc_rate_service._normalize_reference("25-21") == "21, 22, 23, 24, 25"
    assert dc_rate_service._normalize_reference("12; 14, 16-17") == "12, 14, 16, 17"
    assert dc_rate_service._normalize_reference("9-4") == "4, 5, 6, 7, 8, 9"
    with pytest.raises(dc_rate_service.DCRateServiceError, match="5,001"):
        dc_rate_service._normalize_reference("1-6000")


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
    preview = dc_rate_service.preview_mapped_schedule(headers, rows, mapping, "rural_dc")
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

