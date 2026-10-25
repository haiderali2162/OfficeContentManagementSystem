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
        schedule_column = next(c for c in inspect(engine).get_columns("dc_valuation_rates") if c["name"] == "schedule_type")
        assert location == ("Old Mauza", None)
        assert schedule_column["default"] == "'dc'"
        year = connection.execute(text("SELECT published_year FROM dc_valuation_rates WHERE id=1")).scalar_one()
        assert year == 2024
    engine.dispose()

