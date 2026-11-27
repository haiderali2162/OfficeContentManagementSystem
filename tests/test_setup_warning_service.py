"""Read-only app-wide setup warning tests (v0.2.30).

All records and settings here live in a temporary SQLite file. The collector
is deliberately read-only, so these tests also verify that an unreadable
database produces a safe message without leaking connection details.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base
from core.models import (
    Client,
    DCValuationRate,
    RegistryFeeFormula,
    TaxFeeRule,
)
from services import backup_service, settings_service, setup_warning_service


@pytest.fixture
def warning_environment(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'warnings.sqlite3').as_posix()}"
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False
    )
    monkeypatch.setattr(settings_service, "SessionLocal", sessions)
    monkeypatch.setattr(setup_warning_service, "SessionLocal", sessions)

    backup_folder = tmp_path / "backups"
    backup_folder.mkdir()
    monkeypatch.setattr(
        backup_service,
        "get_backup_settings",
        lambda: {
            "enabled": True,
            "frequency_hours": 24,
            "location": str(backup_folder),
            "keep_count": 5,
        },
    )
    monkeypatch.setattr(
        setup_warning_service,
        "get_default_file_location",
        lambda: str(tmp_path),
    )
    try:
        yield sessions
    finally:
        engine.dispose()


def _by_code(warnings: list[dict]) -> dict[str, dict]:
    return {item["code"]: item for item in warnings}


def _visible_codes() -> set[str]:
    return {
        item["code"]
        for item in setup_warning_service.visible_setup_warnings()
    }


def test_default_warning_settings_are_enabled_and_unfiltered(warning_environment):
    assert settings_service.get_setup_warning_settings() == (
        settings_service.DEFAULT_SETUP_WARNING_SETTINGS
    )


def test_empty_database_reports_missing_data_without_folder_noise(
    warning_environment,
):
    warnings = setup_warning_service.collect_setup_warnings()
    by_code = _by_code(warnings)

    assert {
        "no_clients",
        "no_dc_rates",
        "no_fbr_rates",
        "no_tax_rules",
        "no_registry_formulas",
    }.issubset(by_code)
    assert "backups_disabled" not in by_code
    assert "no_default_folder" not in by_code
    assert by_code["no_clients"]["severity"] == "info"
    assert by_code["no_dc_rates"]["severity"] == "warning"
    assert by_code["no_tax_rules"]["severity"] == "warning"
    assert all(item["visible"] for item in warnings)


def test_verified_records_clear_missing_data_and_verification_warnings(
    warning_environment,
):
    sessions = warning_environment
    with sessions() as session:
        session.add(Client(name="Synthetic client"))
        session.add_all([
            DCValuationRate(
                property_type="Residential",
                rate_value=Decimal("1"),
                rate_unit="per_marla",
                effective_from=date(2026, 1, 1),
                schedule_type="dc",
                verification_status="verified",
            ),
            DCValuationRate(
                property_type="Residential",
                rate_value=Decimal("2"),
                rate_unit="per_marla",
                effective_from=date(2026, 1, 1),
                schedule_type="fbr",
                verification_status="verified",
            ),
        ])
        session.add(TaxFeeRule(
            name="Synthetic test rule",
            category="test_only",
            jurisdiction="Test only",
            calculation_method="fixed_amount",
            fixed_amount=Decimal("1"),
            effective_from=date(2026, 1, 1),
            verification_status="verified",
        ))
        session.add(RegistryFeeFormula(
            name="Synthetic test formula",
            category="test_only",
            formula_expression="1",
            effective_from=date(2026, 1, 1),
            verification_status="verified",
        ))
        session.commit()

    assert setup_warning_service.collect_setup_warnings() == []


def test_unverified_records_are_described_as_estimates(warning_environment):
    sessions = warning_environment
    with sessions() as session:
        session.add(DCValuationRate(
            property_type="Residential",
            rate_value=Decimal("1"),
            rate_unit="per_marla",
            effective_from=date(2026, 1, 1),
            source_reference="Synthetic test value; not an official rate",
        ))
        session.commit()

    warning = _by_code(setup_warning_service.collect_setup_warnings())[
        "unverified_dc_rates"
    ]
    assert warning["severity"] == "warning"
    assert "estimates" in warning["message"]
    assert "official" not in warning["message"].lower()


def test_visibility_applies_master_check_severity_and_ignored_codes(
    warning_environment,
):
    sessions = warning_environment
    with sessions() as session:
        session.add(DCValuationRate(
            property_type="Residential",
            rate_value=Decimal("1"),
            rate_unit="per_marla",
            effective_from=date(2026, 1, 1),
        ))
        session.commit()

    checks = dict(settings_service.DEFAULT_SETUP_WARNING_SETTINGS["checks"])
    checks["tax_rules"] = False
    settings_service.set_setup_warning_settings({
        "enabled": True,
        "show_info": False,
        "checks": checks,
        "ignored_codes": ["no_clients"],
    })

    warnings = setup_warning_service.collect_setup_warnings()
    by_code = _by_code(warnings)
    assert by_code["no_clients"]["ignored"] is True
    assert by_code["no_clients"]["visible"] is False
    assert by_code["no_fbr_rates"]["severity"] == "info"
    assert by_code["no_fbr_rates"]["ignored"] is False
    assert by_code["no_fbr_rates"]["visible"] is False
    assert by_code["no_tax_rules"]["check_enabled"] is False
    assert by_code["no_tax_rules"]["visible"] is False
    assert by_code["unverified_dc_rates"]["visible"] is True
    assert _visible_codes() == {"unverified_dc_rates"}


def test_master_switch_hides_every_warning_but_keeps_them_reviewable(
    warning_environment,
):
    settings_service.set_setup_warning_settings({
        "enabled": False,
    })

    warnings = setup_warning_service.collect_setup_warnings()

    assert warnings
    assert all(item["visible"] is False for item in warnings)
    assert _visible_codes() == set()


def test_missing_backup_folder_is_reported(warning_environment, monkeypatch):
    monkeypatch.setattr(
        backup_service,
        "get_backup_settings",
        lambda: {
            "enabled": True,
            "frequency_hours": 24,
            "location": "C:/definitely/missing/land-office-backups",
            "keep_count": 5,
        },
    )

    warning = _by_code(setup_warning_service.collect_setup_warnings())[
        "backup_folder_missing"
    ]

    assert warning["severity"] == "warning"
    assert warning["visible"] is True
    assert warning["nav_key"] == "settings"


def test_database_read_failure_is_safe_and_suppressible(
    warning_environment, monkeypatch
):
    def broken_session():
        raise RuntimeError(
            "postgresql://user:secret-password@db.example/land_office"
        )

    monkeypatch.setattr(setup_warning_service, "SessionLocal", broken_session)
    warnings = setup_warning_service.collect_setup_warnings()
    by_code = _by_code(warnings)

    assert set(by_code) == {"database_read_failed"}
    assert by_code["database_read_failed"]["visible"] is True
    assert "secret-password" not in json.dumps(warnings)
    assert "postgresql://" not in json.dumps(warnings)

    settings_service.set_setup_warning_settings({
        "ignored_codes": ["database_read_failed"],
        "checks": {"database": False},
    })
    assert _visible_codes() == set()
    assert "database_read_failed" in _by_code(
        setup_warning_service.collect_setup_warnings()
    )
