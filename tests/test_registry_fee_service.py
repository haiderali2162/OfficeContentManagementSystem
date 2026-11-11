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
    "__import__('os')", "area.__class__", "unknown_value + 1", "1 / 0", "len(area)",
])
def test_formula_rejects_unsafe_or_invalid_expressions(expression):
    with pytest.raises(formula_service.RegistryFeeFormulaError):
        formula_service.evaluate_formula(expression, {"area": 10})


def test_formula_supports_guide_documented_variables_and_capping():
    # The Settings guide documents transferred_value and min()/max() capping:
    # both must actually evaluate, not just parse.
    capped = formula_service.evaluate_formula(
        "transferred_value x 2% + 500", {"transferred_value": Decimal("50000")}
    )
    assert capped == Decimal("1500")

    ceiling = formula_service.evaluate_formula(
        "min(transferred_value * 1%, 2000)", {"transferred_value": Decimal("900000")}
    )
    assert ceiling == Decimal("2000")

    floor = formula_service.evaluate_formula(
        "max(transferred_value - 2%, 100)", {"transferred_value": Decimal("50")}
    )
    assert floor == Decimal("100")


def test_saved_formula_accepts_transfer_and_capping_expression(formula_database):
    expression = "transferred_value x 1% + min(area, 10)"
    validated = formula_service.validate_formula(expression)
    assert "transferred_value" in validated
    assert "min" in validated

    formula_service.create_registry_fee_formula(
        name="Transfer fee", category="stamp", formula_expression=expression,
        jurisdiction="Punjab", effective_from="2025-01-01",
    )
    saved = formula_service.list_registry_fee_formulas()[0]
    assert saved["verification_status"] == "unverified"

    result = formula_service.calculate_registry_fee_formulas(
        formulas=[saved], effective_on=date(2026, 1, 1),
        context={"area": Decimal("25"), "transferred_value": Decimal("200000")},
    )
    # 1% of 200000 + min(25, 10) = 2000.00 + 10 = 2010.00
    assert result["charges"][0]["amount"] == Decimal("2010.00")


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


def test_custom_numeric_fields_can_be_used_in_saved_formulas(formula_database):
    field_id = formula_service.create_registry_fee_custom_field(
        label="Additional plots", field_key="additional_plots"
    )
    formula_id = formula_service.create_registry_fee_formula(
        name="Additional plot fee", category="custom",
        formula_expression="additional_plots * 500", jurisdiction="Punjab",
        effective_from="2025-01-01",
    )

    formula = formula_service.list_registry_fee_formulas()[0]
    result = formula_service.calculate_registry_fee_formulas(
        formulas=[formula], effective_on=date(2026, 1, 1),
        context={"additional_plots": Decimal("3")},
        custom_variables=["additional_plots"],
    )

    assert formula_id > 0
    assert result["totals_by_currency"]["PKR"] == Decimal("1500.00")
    assert formula_service.list_registry_fee_custom_fields()[0]["id"] == field_id
    with pytest.raises(formula_service.RegistryFeeFormulaError, match="referenced"):
        formula_service.delete_registry_fee_custom_field(field_id)


@pytest.mark.parametrize(
    ("dc_value", "expected"),
    [(Decimal("2999999"), Decimal("3300.00")),
     (Decimal("3000000"), Decimal("3000.00"))],
)
def test_user_supplied_plra_formula_condition(dc_value, expected):
    result = formula_service.evaluate_formula(
        "3300 if dc_value < 3000000 else dc_value * 0.1%",
        {"dc_value": dc_value},
    )
    assert result == expected


def test_delete_registry_fee_formula_removes_record(formula_database):
    formula_id = formula_service.create_registry_fee_formula(
        name="Deletable fee", category="stamp", formula_expression="area * 10",
        jurisdiction="Punjab", effective_from="2025-07-01",
    )
    assert len(formula_service.list_registry_fee_formulas()) == 1
    formula_service.delete_registry_fee_formula(formula_id)
    assert formula_service.list_registry_fee_formulas() == []

    with pytest.raises(formula_service.RegistryFeeFormulaError, match="not found"):
        formula_service.delete_registry_fee_formula(formula_id)
