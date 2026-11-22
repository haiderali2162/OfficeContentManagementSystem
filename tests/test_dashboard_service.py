"""Dashboard statistics service tests on a temporary SQLite database."""

from datetime import date

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from core.database import Base
from core.models import (
    AreaCalculationHistory,
    Client,
    DCValuationRate,
    NaqshaRecord,
    ValuationEstimate,
)
from services import dashboard_service


@pytest.fixture
def dashboard_database(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'dash.sqlite3').as_posix()}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    isolated = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(dashboard_service, "SessionLocal", isolated)
    try:
        yield engine
    finally:
        engine.dispose()


def test_dashboard_stats_counts_and_recent_activity(dashboard_database):
    with dashboard_service.SessionLocal.begin() as session:
        session.add(Client(name="Ali", current_status="Active",
                           referred_by="R"))
        session.add(Client(name="Sara", current_status="Pending",
                           referred_by="R"))
        session.add(Client(name="Nida", current_status="Inactive",
                           referred_by="R"))
        location = None  # rates can exist without a location row? no — use one
    # Rates need a location row.
    with dashboard_service.SessionLocal.begin() as session:
        from core.models import PropertyLocation
        location = PropertyLocation(name="L", mauza="M")
        session.add(location)
        session.flush()
        session.add(DCValuationRate(
            location_id=location.id, schedule_type="dc",
            property_type="Residential", rate_value="100",
            rate_unit="per_marla", effective_from=date(2025, 1, 1),
            source_reference="test", source_date=date(2025, 1, 1),
            verification_status="unverified",
        ))
        session.add(DCValuationRate(
            location_id=location.id, schedule_type="fbr",
            property_type="Residential", rate_value="200",
            rate_unit="per_marla", effective_from=date(2025, 1, 1),
            source_reference="test", source_date=date(2025, 1, 1),
            verification_status="unverified",
        ))
        session.add(ValuationEstimate(
            transaction_date=date(2026, 1, 1), property_type="Residential",
            area_value=1, area_unit="kanal", share_numerator=1,
            share_denominator=1, total_charges=100,
            input_snapshot_json="{}",
        ))
        session.add(AreaCalculationHistory(
            operation="Add", input_format="Kanal-Marla-Foot",
            inputs_json="{}", settings_json="{}", result="r",
            calculation_details="d",
        ))
        session.add(NaqshaRecord(mauza="M", khewat_no="12"))

    stats = dashboard_service.get_dashboard_stats()
    assert stats["clients_total"] == 3
    assert stats["clients_active"] == 1
    assert stats["clients_pending"] == 1
    assert stats["clients_inactive"] == 1
    assert stats["dc_rates"] == 1
    assert stats["fbr_rates"] == 1
    assert stats["estimates"] == 1
    assert stats["area_calculations"] == 1
    assert stats["naqsha_records"] == 1
    assert len(stats["recent_estimates"]) == 1
    assert stats["recent_estimates"][0]["id"] >= 1
    assert len(stats["recent_clients"]) == 3
    assert stats["recent_clients"][0]["name"] in {"Ali", "Sara", "Nida"}


def test_dashboard_stats_on_empty_database(dashboard_database):
    stats = dashboard_service.get_dashboard_stats()
    assert stats["clients_total"] == 0
    assert stats["estimates"] == 0
    assert stats["recent_clients"] == []
    assert stats["recent_estimates"] == []
