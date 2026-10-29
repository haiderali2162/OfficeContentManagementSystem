"""Registry formula tests use an isolated SQLite database."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base
from services import registry_fee_service as formula_service


@pytest.fixture
def formula_database(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'fee-formulas.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(formula_service, "SessionLocal", sessions)
    try:
        yield engine
    finally:
        engine.dispose()


def test_formula_supports_percentages_brackets_and_saved_rules(formula_database):
    formula_id = formula_service.create_registry_fee_formula(
        name="Test registry fee", category="stamp", formula_expression="(area x 2.5%) + 2000",
        jurisdiction="Punjab", effective_from="2025-07-01",
    )
    saved = formula_service.list_registry_fee_formulas()[0]
    assert saved["id"] == formula_id
    assert saved["verification_status"] == "unverified"

    result = formula_service.calculate_registry_fee_formulas(
        formulas=[saved], effective_on=date(2026, 1, 1),
        context={"area": Decimal("10000"), "mauza": "Example"},
    )
    assert result["charges"][0]["amount"] == Decimal("2250.00")
    assert result["charges"][0]["rule_snapshot"]["formula_expression"] == "(area x 2.5%) + 2000"
    assert result["warnings"]


@pytest.mark.parametrize("expression", [
    "__import__('os')", "area.__class__", "unknown_value + 1", "1 / 0",
])
def test_formula_rejects_unsafe_or_invalid_expressions(expression):
    with pytest.raises(formula_service.RegistryFeeFormulaError):
        formula_service.evaluate_formula(expression, {"area": 10})


def test_formulas_match_mauza_and_square_scope():
    result = formula_service.calculate_registry_fee_formulas(
        formulas=[{
            "id": 1, "name": "Scoped fee", "category": "committee", "jurisdiction": "Punjab",
            "formula_expression": "dc_value * 1%", "effective_from": date(2025, 1, 1),
            "effective_to": None, "eligibility_conditions_json": '{"when":{"mauza":"Example","square_no":"7"}}',
        }],
        effective_on=date(2026, 1, 1), context={"mauza": "Example", "square_no": "7", "dc_value": 10000},
    )
    assert result["totals_by_currency"]["PKR"] == Decimal("100.00")
