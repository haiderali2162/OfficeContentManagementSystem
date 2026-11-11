"""Estimate persistence tests use isolated SQLite, never the office database."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from core.database import Base
from core.models import Client, DCValuationRate, PropertyLocation
from services import estimate_service


@pytest.fixture
def estimate_database(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'estimates.sqlite3').as_posix()}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(estimate_service, "SessionLocal", sessions)
    with sessions.begin() as session:
        client = Client(name="Test client")
        location = PropertyLocation(name="Test Mauza", district="Test District")
        session.add_all([client, location])
        session.flush()
        rate = DCValuationRate(
            location_id=location.id,
            property_type="Residential",
            classification="Urban",
            rate_value=Decimal("800000"),
            rate_unit="per_acre",
            effective_from=date(2025, 7, 1),
            published_year=2025,
            source_reference="Hypothetical test schedule",
        )
        session.add(rate)
        session.flush()
        ids = {"client": client.id, "location": location.id, "rate": rate.id}
    try:
        yield engine, ids
    finally:
        engine.dispose()


def test_save_and_reopen_estimate_keeps_inputs_and_charge_snapshots(estimate_database):
    _engine, ids = estimate_database
    inputs = {
        "location_id": ids["location"],
        "transaction_date": date(2026, 1, 1),
        "transaction_type": "sale",
        "property_type": "Residential",
        "classification": "Urban",
        "area_value": "4-0-0",
        "area_unit": "kmf",
        "share_numerator": 1,
        "share_denominator": 5,
    }
    valuation = {
        "rate_id": ids["rate"],
        "area_square_feet": Decimal("21760"),
        "property_value": Decimal("400000.00"),
        "transferred_value": Decimal("80000.00"),
        "declared_consideration": None,
        "warnings": ["Hypothetical warning"],
        "rate": {"rate_unit": "per_acre"},
    }
    charges = {
        "totals_by_currency": {"PKR": Decimal("250.00")},
        "warnings": [],
        "charges": [{
            "tax_fee_rule_id": None,
            "name": "Hypothetical fee",
            "category": "test_fee",
            "payer": "buyer",
            "calculation_method": "fixed_amount",
            "calculation_base": None,
            "applied_rate": None,
            "amount": Decimal("250.00"),
            "currency_code": "PKR",
            "rule_snapshot": {"source_reference": "Test only"},
        }],
    }
    estimate_id = estimate_service.save_estimate(
        inputs=inputs, valuation=valuation, charges=charges, client_id=ids["client"]
    )

    saved = estimate_service.get_estimate(estimate_id)
    assert saved["area_value"] == Decimal("21760.00000000")
    assert saved["input_snapshot"]["area_value"] == "4-0-0"
    assert saved["client_name"] == "Test client"
    assert saved["location_snapshot"]["name"] == "Test Mauza"
    assert saved["rate_snapshot"]["source_reference"] == "Hypothetical test schedule"
    assert saved["charges"][0]["rule_snapshot"]["source_reference"] == "Test only"
    assert saved["warnings"] == ["Hypothetical warning"]
    assert estimate_service.list_estimates()[0]["id"] == estimate_id


def test_estimate_snapshots_survive_optional_link_deletion(estimate_database):
    engine, ids = estimate_database
    estimate_id = estimate_service.save_estimate(
        inputs={"location_id": ids["location"], "transaction_date": date(2026, 1, 1),
                "property_type": "Residential"},
        valuation={"rate_id": ids["rate"], "area_square_feet": Decimal("100"),
                   "property_value": Decimal("10"), "transferred_value": Decimal("10"),
                   "warnings": [], "rate": {}},
        charges={"totals_by_currency": {}, "warnings": [], "charges": []},
        client_id=ids["client"],
    )
    with Session(engine) as session:
        session.delete(session.get(Client, ids["client"]))
        session.delete(session.get(DCValuationRate, ids["rate"]))
        session.delete(session.get(PropertyLocation, ids["location"]))
        session.commit()
    saved = estimate_service.get_estimate(estimate_id)
    assert saved["client_name"] is None
    assert saved["location_snapshot"]["name"] == "Test Mauza"
    assert saved["rate_snapshot"]["id"] == ids["rate"]



def test_reopened_estimate_charges_carry_party_labels_from_snapshot(estimate_database):
    """Per-party filer charges keep their buyer/seller labels after saving."""
    _engine, ids = estimate_database
    inputs = {
        "location_id": ids["location"],
        "transaction_date": date(2026, 1, 1),
        "transaction_type": "sale",
        "property_type": "Residential",
        "classification": "Urban",
        "area_value": "4-0-0",
        "area_unit": "kmf",
        "share_numerator": 1,
        "share_denominator": 5,
    }
    valuation = {
        "rate_id": ids["rate"],
        "area_square_feet": Decimal("21760"),
        "property_value": Decimal("400000.00"),
        "transferred_value": Decimal("80000.00"),
        "declared_consideration": None,
        "warnings": [],
        "rate": {"rate_unit": "per_acre"},
    }
    charges = {
        "totals_by_currency": {"PKR": Decimal("250.00")},
        "warnings": [],
        "charges": [
            {
                "tax_fee_rule_id": None,
                "name": "Hypothetical fee",
                "category": "test_fee",
                "payer": "buyer",
                "calculation_method": "fixed_amount",
                "calculation_base": None,
                "applied_rate": None,
                "amount": Decimal("250.00"),
                "currency_code": "PKR",
                "rule_snapshot": {},
            },
            {
                "tax_fee_rule_id": None,
                "name": "236K (Active filer)",
                "category": "236k",
                "payer": "buyer",
                "calculation_method": "percentage",
                "calculation_base": None,
                "applied_rate": Decimal("1.25"),
                "amount": Decimal("208.28"),
                "currency_code": "PKR",
                "party": {"label": "Buyer 1", "role": "buyer"},
                "rule_snapshot": {},
            },
        ],
    }
    estimate_id = estimate_service.save_estimate(
        inputs=inputs, valuation=valuation, charges=charges, client_id=ids["client"]
    )

    saved = estimate_service.get_estimate(estimate_id)
    assert saved["charges"][0].get("party") is None
    assert saved["charges"][1]["party"] == {"label": "Buyer 1", "role": "buyer"}
