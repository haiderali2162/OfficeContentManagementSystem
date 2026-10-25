"""Database-foundation tests using isolated SQLite files."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import core.database as database
from core.database import Base
from core.models import (
    AreaCalculationHistory,
    Client,
    DCValuationRate,
    PropertyLocation,
    TaxFeeRule,
    ValuationEstimate,
    ValuationEstimateCharge,
)


@pytest.fixture
def database_engine(tmp_path):
    """Create all models in a temporary database with SQLite FKs enabled."""
    engine = create_engine(f"sqlite:///{(tmp_path / 'stage_8_1.sqlite3').as_posix()}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def test_schema_creation_is_additive_and_preserves_existing_module_data(database_engine):
    with Session(database_engine) as session:
        client = Client(name="Existing client")
        history = AreaCalculationHistory(
            operation="Add",
            input_format="Kanal-Marla-Foot",
            inputs_json='{"A":"1","B":"2"}',
            settings_json='{}',
            result="3-0-0",
            calculation_details="1 + 2",
        )
        session.add_all([client, history])
        session.commit()
        client_id, history_id = client.id, history.id

    # This is the same SQLAlchemy metadata operation used during app startup;
    # running it again must leave the prior records and tables intact.
    Base.metadata.create_all(database_engine)
    names = set(inspect(database_engine).get_table_names())
    assert {
        "property_locations",
        "dc_valuation_rates",
        "tax_fee_rules",
        "valuation_estimates",
        "valuation_estimate_charges",
    }.issubset(names)
    with Session(database_engine) as session:
        assert session.get(Client, client_id).name == "Existing client"
        assert session.get(AreaCalculationHistory, history_id).result == "3-0-0"


def test_valuation_records_use_decimal_and_keep_snapshots_after_link_deletion(database_engine):
    with Session(database_engine) as session:
        client = Client(name="Estimate client")
        location = PropertyLocation(
            name="Sample Mauza", district="Sample District", hadbast_number="123"
        )
        session.add_all([client, location])
        session.flush()

        rate = DCValuationRate(
            location=location,
            property_type="Residential",
            classification="Urban",
            rate_value=Decimal("12500.25000000"),
            rate_unit="per_marla",
            effective_from=date(2025, 7, 1),
            source_reference="Hypothetical test schedule",
        )
        rule = TaxFeeRule(
            name="Hypothetical fee",
            category="registration_fee",
            jurisdiction="Test jurisdiction",
            calculation_method="fixed_amount",
            fixed_amount=Decimal("100.25"),
            effective_from=date(2025, 7, 1),
            source_reference="Test only; not an official rate",
        )
        session.add_all([rate, rule])
        session.flush()
        assert rate.verification_status == "unverified"
        assert rule.verification_status == "unverified"

        estimate = ValuationEstimate(
            client=client,
            location=location,
            dc_rate_id=rate.id,
            transaction_date=date(2025, 8, 1),
            property_type="Residential",
            classification="Urban",
            area_value=Decimal("5.50000000"),
            area_unit="marla",
            share_numerator=1,
            share_denominator=2,
            declared_consideration=Decimal("2500000.00"),
            property_value=Decimal("2750055.00"),
            transferred_value=Decimal("1375027.50"),
            total_charges=Decimal("100.25"),
            input_snapshot_json='{"client":"Estimate client"}',
            client_snapshot_json='{"name":"Estimate client"}',
            location_snapshot_json='{"mauza":"Sample Mauza"}',
            rate_snapshot_json='{"rate":"12500.25","unit":"per_marla"}',
            result_snapshot_json='{"property_value":"2750055.00"}',
            warnings_json='["Hypothetical test data"]',
        )
        estimate.charges.append(
            ValuationEstimateCharge(
                tax_fee_rule_id=rule.id,
                name=rule.name,
                category=rule.category,
                calculation_method=rule.calculation_method,
                amount=Decimal("100.25"),
                rule_snapshot_json='{"name":"Hypothetical fee","amount":"100.25"}',
            )
        )
        session.add(estimate)
        session.commit()
        estimate_id = estimate.id

        loaded = session.get(ValuationEstimate, estimate_id)
        assert loaded.declared_consideration == Decimal("2500000.00")
        assert loaded.charges[0].amount == Decimal("100.25")
        assert session.get(DCValuationRate, rate.id).rate_value == Decimal(
            "12500.25000000"
        )

        # Deleting the optional client/location/rule links must not erase the
        # saved estimate or the charge/rate snapshots needed to explain it.
        session.delete(client)
        session.delete(location)
        session.delete(rate)
        session.delete(rule)
        session.commit()

        loaded = session.get(ValuationEstimate, estimate_id)
        assert loaded.client_id is None
        assert loaded.location_id is None
        assert loaded.dc_rate_id is None
        assert loaded.client_snapshot_json == '{"name":"Estimate client"}'
        assert loaded.location_snapshot_json == '{"mauza":"Sample Mauza"}'
        assert loaded.rate_snapshot_json == '{"rate":"12500.25","unit":"per_marla"}'
        assert loaded.charges[0].tax_fee_rule_id is None
        assert "Hypothetical fee" in loaded.charges[0].rule_snapshot_json


def test_dc_rate_rejects_unknown_units(database_engine):
    with Session(database_engine) as session:
        session.add(
            DCValuationRate(
                property_type="Residential",
                rate_value=Decimal("1.00"),
                rate_unit="unknown_unit",
                effective_from=date(2025, 7, 1),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_tax_rule_rejects_verified_status_outside_known_values(database_engine):
    with Session(database_engine) as session:
        session.add(
            TaxFeeRule(
                name="Invalid status rule",
                category="test",
                jurisdiction="Test",
                calculation_method="fixed_amount",
                effective_from=date(2025, 7, 1),
                verification_status="official",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_startup_initialization_adds_tables_and_preserves_legacy_client(
    tmp_path, monkeypatch
):
    """Exercise the real startup initializer without opening the live DB."""
    isolated_engine = create_engine(
        f"sqlite:///{(tmp_path / 'legacy_startup.sqlite3').as_posix()}"
    )
    with isolated_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE clients (
                id INTEGER PRIMARY KEY,
                name VARCHAR(200) NOT NULL,
                father_name VARCHAR(200),
                cnic VARCHAR(20),
                current_status VARCHAR(100),
                referred_by VARCHAR(200),
                password_ciphertext BLOB,
                pin_ciphertext BLOB,
                created_at DATETIME,
                updated_at DATETIME
            )
            """
        )
        connection.exec_driver_sql(
            "INSERT INTO clients (id, name) VALUES (17, 'Legacy client')"
        )
        AreaCalculationHistory.__table__.create(connection)
        connection.exec_driver_sql(
            """
            INSERT INTO area_calculation_history
                (id, operation, input_format, inputs_json, settings_json,
                 result, calculation_details, created_at)
            VALUES (23, 'Add', 'Kanal-Marla-Foot', '{}', '{}', '1-0-0',
                    'existing history', '2025-01-01 00:00:00')
            """
        )

    monkeypatch.setattr(database, "engine", isolated_engine)
    try:
        database.initialize_database()
        with isolated_engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT name FROM clients WHERE id=17"
            ).scalar_one() == "Legacy client"
            assert connection.exec_driver_sql(
                "SELECT calculation_details FROM area_calculation_history WHERE id=23"
            ).scalar_one() == "existing history"
            tables = set(inspect(isolated_engine).get_table_names())
            assert "valuation_estimates" in tables
            assert "tax_fee_rules" in tables
            client_columns = {
                column["name"]
                for column in inspect(isolated_engine).get_columns("clients")
            }
            assert "ntn_number" in client_columns
            assert "payment_amount" in client_columns
    finally:
        isolated_engine.dispose()
