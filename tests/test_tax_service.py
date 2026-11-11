"""Tax/fee rule management and calculations on temporary SQLite."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base
from services import tax_service


@pytest.fixture
def tax_database(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'tax_rules.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(tax_service, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    try:
        yield engine
    finally:
        engine.dispose()


def _rule(**overrides):
    values = {
        "name": "Hypothetical transfer charge",
        "category": "hypothetical_tax",
        "jurisdiction": "Punjab test",
        "calculation_method": "percentage",
        "rate_value": "2.5",
        "fixed_amount": None,
        "effective_from": "2025-07-01",
        "effective_to": "",
        "eligibility_conditions_json": {
            "base": "transferred_value",
            "payer": "buyer",
            "when": {"transaction_type": "sale"},
        },
    }
    values.update(overrides)
    return values


def _record(**overrides):
    values = _rule()
    values.update({
        "id": 1,
        "effective_from": date(2025, 7, 1),
        "effective_to": None,
        "fixed_amount": None,
        "rate_unit": None,
        "minimum_amount": None,
        "maximum_amount": None,
        "currency_code": "PKR",
        "source_reference": "Hypothetical test source",
        "source_date": date(2025, 7, 1),
        "verification_status": "verified",
        "verified_by": "Test verifier",
    })
    values.update(overrides)
    return values


def test_rule_crud_defaults_to_unverified_and_filters(tax_database):
    rule_id = tax_service.create_tax_fee_rule(**_rule())
    rows = tax_service.list_tax_fee_rules(jurisdiction="Punjab test")
    assert len(rows) == 1
    assert rows[0]["id"] == rule_id
    assert rows[0]["verification_status"] == "unverified"
    assert tax_service.list_tax_fee_rules(category="missing") == []

    tax_service.update_tax_fee_rule(
        rule_id=rule_id,
        **_rule(name="Updated hypothetical charge", verification_status="verified",
                source_reference="Official source supplied by reviewer", verified_by="Reviewer"),
    )
    updated = tax_service.list_tax_fee_rules()[0]
    assert updated["name"] == "Updated hypothetical charge"
    assert updated["verification_status"] == "verified"
    assert updated["verified_at"] is not None


def test_rule_validation_requires_explicit_percentage_base_and_verified_source():
    with pytest.raises(tax_service.TaxRuleError, match="explicitly name"):
        tax_service._rule_data(_rule(eligibility_conditions_json={}))
    with pytest.raises(tax_service.TaxRuleError, match="requires its source"):
        tax_service._rule_data(_rule(
            verification_status="verified", source_reference="", verified_by=""
        ))


def test_percentage_fixed_and_per_unit_charges_keep_rule_snapshots():
    rules = [
        _record(),
        _record(id=2, name="Hypothetical fixed fee", category="fixed",
                calculation_method="fixed_amount", rate_value=None,
                fixed_amount=Decimal("125.50"), eligibility_conditions_json={
                    "payer": "other",
                }),
        _record(id=3, name="Hypothetical area fee", category="per_unit",
                calculation_method="per_unit", rate_value=Decimal("10"),
                rate_unit="per_marla",
                eligibility_conditions_json={"unit_key": "area_quantity", "payer": "seller"}),
    ]
    result = tax_service.calculate_charges(
        rules=rules,
        transaction_date=date(2026, 1, 1),
        context={"transaction_type": "sale", "transferred_value": "10000", "area_quantity": "3.5", "area_rate_unit": "per_marla"},
        jurisdiction="Punjab test",
    )
    assert [line["amount"] for line in result["charges"]] == [
        Decimal("250.00"), Decimal("125.50"), Decimal("35.00")
    ]
    assert result["total_charges"] == Decimal("410.50")
    assert result["charges"][0]["payer"] == "buyer"
    assert result["charges"][0]["rule_snapshot"]["source_reference"] == "Hypothetical test source"
    assert result["warnings"] == []


def test_charge_caps_and_base_thresholds_are_respected():
    rule = _record(
        minimum_amount=Decimal("50"), maximum_amount=Decimal("120"),
        eligibility_conditions_json={"base": "property_value", "minimum_base": "1000"},
    )
    capped = tax_service.calculate_charges(
        rules=[rule], transaction_date=date(2026, 1, 1),
        context={"property_value": "10000"},
    )
    assert capped["charges"][0]["amount"] == Decimal("120.00")

    below_threshold = tax_service.calculate_charges(
        rules=[rule], transaction_date=date(2026, 1, 1),
        context={"property_value": "500"},
    )
    assert below_threshold["charges"] == []


def test_missing_eligibility_or_exemption_inputs_are_reported_without_charge():
    conditional = _record(eligibility_conditions_json={
        "base": "transferred_value",
        "when": {"filer_status": "ATL"},
        "exempt_when": [{"field": "buyer_exempt", "equals": True}],
    })
    result = tax_service.calculate_charges(
        rules=[conditional], transaction_date=date(2026, 1, 1), context={
            "transferred_value": "1000", "filer_status": "ATL",
        },
    )
    assert result["charges"] == []
    assert "Missing exemption input" in result["skipped_rules"][0]["reason"]

    missing_when = tax_service.calculate_charges(
        rules=[conditional], transaction_date=date(2026, 1, 1), context={
            "transferred_value": "1000",
        },
    )
    assert "Missing eligibility input" in missing_when["skipped_rules"][0]["reason"]


def test_missing_declared_amount_skips_rules_that_require_it():
    declared_rule = _record(eligibility_conditions_json={
        "base": "declared_consideration", "payer": "seller",
    })
    result = tax_service.calculate_charges(
        rules=[declared_rule], transaction_date=date(2026, 1, 1),
        context={"declared_consideration": None},
    )
    assert result["charges"] == []
    assert "Missing calculation base" in result["skipped_rules"][0]["reason"]


def test_unverified_and_missing_source_warns_and_inactive_rules_are_ignored():
    rule = _record(
        verification_status="unverified", source_reference=None,
        effective_to=date(2025, 12, 31),
    )
    result = tax_service.calculate_charges(
        rules=[rule], transaction_date=date(2025, 8, 1),
        context={"transaction_type": "sale", "transferred_value": "1000"},
    )
    assert len(result["warnings"]) == 2
    later = tax_service.calculate_charges(
        rules=[rule], transaction_date=date(2026, 1, 1),
        context={"transaction_type": "sale", "transferred_value": "1000"},
    )
    assert later["charges"] == []


def test_invalid_rule_input_and_currency_are_rejected():
    with pytest.raises(tax_service.TaxRuleError, match="three-letter"):
        tax_service._rule_data(_rule(currency_code="PK"))
    with pytest.raises(tax_service.TaxRuleError, match="Maximum charge"):
        tax_service._rule_data(_rule(minimum_amount="200", maximum_amount="100"))
    with pytest.raises(tax_service.TaxRuleError, match="unit_key and rate_unit"):
        tax_service._rule_data(_rule(
            calculation_method="per_unit", rate_value="10",
            eligibility_conditions_json={"unit_key": "area_quantity"},
        ))


def test_per_unit_charge_skips_mismatched_measurement_units_and_separates_currencies():
    per_area = _record(
        id=2, name="Hypothetical area fee", calculation_method="per_unit",
        rate_value=Decimal("10"), rate_unit="per_marla",
        eligibility_conditions_json={"unit_key": "area_quantity"},
    )
    usd_fixed = _record(
        id=3, name="Hypothetical USD fee", calculation_method="fixed_amount",
        rate_value=None, fixed_amount=Decimal("3"), currency_code="USD",
        eligibility_conditions_json={},
    )
    result = tax_service.calculate_charges(
        rules=[per_area, usd_fixed], transaction_date=date(2026, 1, 1),
        context={"area_quantity": "4", "area_rate_unit": "per_acre"},
    )
    assert len(result["charges"]) == 1
    assert "does not match" in result["skipped_rules"][0]["reason"]
    assert result["totals_by_currency"] == {"USD": Decimal("3.00")}
    assert result["total_charges"] == Decimal("3.00")
    assert result["total_currency"] == "USD"


# ---------------------------------------------------------------------------
# fixed_or_percentage (hybrid) rules: fixed below a threshold, % at/above it
# ---------------------------------------------------------------------------


def test_hybrid_rule_validation_and_persistence(tax_database):
    """The hybrid method stores its threshold with the rule and rejects gaps."""
    rule_id = tax_service.create_tax_fee_rule(**_rule(
        calculation_method="fixed_or_percentage",
        rate_value="2", fixed_amount="5000", percentage_threshold="100000",
    ))
    stored = tax_service.list_tax_fee_rules()[0]
    assert stored["calculation_method"] == "fixed_or_percentage"
    assert stored["percentage_threshold"] == Decimal("100000.00")
    assert stored["fixed_amount"] == Decimal("5000.00")

    # Missing threshold cannot exist for the hybrid method.
    with pytest.raises(tax_service.TaxRuleError, match="threshold"):
        tax_service._rule_data(_rule(
            calculation_method="fixed_or_percentage",
            rate_value="2", fixed_amount="5000",
        ))
    # The same for the fixed amount and the percentage itself.
    with pytest.raises(tax_service.TaxRuleError, match="fixed amount"):
        tax_service._rule_data(_rule(
            calculation_method="fixed_or_percentage",
            rate_value="2", fixed_amount=None, percentage_threshold="100000",
        ))
    with pytest.raises(tax_service.TaxRuleError, match="percentage rate"):
        tax_service._rule_data(_rule(
            calculation_method="fixed_or_percentage",
            rate_value=None, fixed_amount="5000", percentage_threshold="100000",
        ))
    # A negative threshold is rejected like every other money field.
    with pytest.raises(tax_service.TaxRuleError, match="negative"):
        tax_service._rule_data(_rule(
            calculation_method="fixed_or_percentage",
            rate_value="2", fixed_amount="5000", percentage_threshold="-1",
        ))
    # Other methods must not carry a stale threshold value.
    with pytest.raises(tax_service.TaxRuleError, match="threshold"):
        tax_service._rule_data(_rule(percentage_threshold="50000"))
    with pytest.raises(tax_service.TaxRuleError, match="threshold"):
        tax_service._rule_data(_rule(
            calculation_method="fixed_amount", rate_value=None,
            fixed_amount="300", percentage_threshold="50000",
        ))
    # Hybrid rules, like percentages, require an explicit base.
    with pytest.raises(tax_service.TaxRuleError, match="explicitly name"):
        tax_service._rule_data(_rule(
            calculation_method="fixed_or_percentage",
            rate_value="2", fixed_amount="5000", percentage_threshold="100000",
            eligibility_conditions_json={"payer": "buyer"},
        ))
    assert stored["id"] == rule_id


def _hybrid_record(**overrides):
    """A verified hybrid rule: 2000 fixed below 50000, else 2 percent."""
    values = _record(
        id=4, name="Hypothetical hybrid fee", category="hybrid",
        calculation_method="fixed_or_percentage", rate_value=Decimal("2"),
        fixed_amount=Decimal("2000"), percentage_threshold=Decimal("50000"),
        eligibility_conditions_json={
            "base": "transferred_value", "payer": "buyer",
        },
    )
    values.update(overrides)
    return values


def test_hybrid_charge_uses_fixed_below_threshold_and_percent_at_or_above():
    """Literal rule behaviour: lower base takes fixed, at/above takes %."""
    below = tax_service.calculate_charges(
        rules=[_hybrid_record()], transaction_date=date(2026, 1, 1),
        context={"transferred_value": "49999.99"},
    )
    assert below["charges"][0]["amount"] == Decimal("2000.00")

    # Exactly at the threshold the percentage applies (base is not lower).
    at = tax_service.calculate_charges(
        rules=[_hybrid_record()], transaction_date=date(2026, 1, 1),
        context={"transferred_value": "50000"},
    )
    assert at["charges"][0]["amount"] == Decimal("1000.00")

    above = tax_service.calculate_charges(
        rules=[_hybrid_record()], transaction_date=date(2026, 1, 1),
        context={"transferred_value": "100000"},
    )
    assert above["charges"][0]["amount"] == Decimal("2000.00")
    # The calculation line records the base that drove the decision.
    assert above["charges"][0]["calculation_base"] == Decimal("100000.00")
    assert above["charges"][0]["calculation_method"] == "fixed_or_percentage"


def test_hybrid_snapshot_keeps_threshold_and_missing_base_skips():
    """Saved estimates embed the threshold so later edits cannot rewrite them."""
    result = tax_service.calculate_charges(
        rules=[_hybrid_record()], transaction_date=date(2026, 1, 1),
        context={"transferred_value": "100000"},
    )
    snapshot = result["charges"][0]["rule_snapshot"]
    assert snapshot["percentage_threshold"] == "50000"

    # Without the base value the hybrid rule is skipped with a reason.
    missing = tax_service.calculate_charges(
        rules=[_hybrid_record()], transaction_date=date(2026, 1, 1),
        context={},
    )
    assert missing["charges"] == []
    assert "Missing calculation base" in missing["skipped_rules"][0]["reason"]

def test_delete_tax_fee_rule_removes_row_and_reports_missing(tax_database):
    """Removal is explicit; saved estimates outside this table are untouched."""
    rule_id = tax_service.create_tax_fee_rule(**_rule())
    tax_service.delete_tax_fee_rule(rule_id)
    assert tax_service.list_tax_fee_rules() == []

    # Deleting an unknown id stays an explicit, catchable error.
    with pytest.raises(tax_service.TaxRuleError) as raised:
        tax_service.delete_tax_fee_rule(rule_id)
    assert "was not found" in str(raised.value)


def test_deleted_rule_id_survives_as_estimate_snapshot(tax_database):
    """estimate_charges references tax_fee_rules so removal keeps snapshots."""
    from sqlalchemy import inspect as sa_inspect

    inspector = sa_inspect(tax_database)
    foreign_keys = inspector.get_foreign_keys("valuation_estimate_charges")
    rule_links = [key for key in foreign_keys if key["referred_table"] == "tax_fee_rules"]
    assert rule_links, "estimate charges must reference tax fee rules"
    # The database is instructed to keep the charge row after a rule removal;
    # the snapshot column then explains what the original rule said.
    create_sql = None
    with tax_database.connect() as connection:
        create_sql = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='valuation_estimate_charges'"
        ).scalar()
    assert "ON DELETE SET NULL" in str(create_sql).upper()


def _buyer(name, area=None, status="ATL"):
    return {"role": "buyer", "name": name, "area_marla": area, "filer_status": status}


def _seller(name, area=None, status="ATL"):
    return {"role": "seller", "name": name, "area_marla": area, "filer_status": status}


def test_filer_rules_split_per_party_by_area_and_status():
    rule = _record()
    rule["eligibility_conditions_json"] = {
        "base": "transferred_value", "payer": "buyer",
        "when": {"buyer_filer_status": "ATL"},
    }
    context = {
        "transferred_value": Decimal("200000"), "area_marla": Decimal("20"),
        "transaction_type": "sale", "buyer_filer_status": "unknown",
    }
    parties = [_buyer("Ali", area="10", status="ATL"),
               _buyer("Usman", area="10", status="non_ATL"),
               _seller("Khalid", status="ATL")]

    result = tax_service.calculate_charges_for_parties(
        rules=[rule], transaction_date=date(2025, 7, 2),
        context=context, parties=parties,
    )

    # 2.5% applies only to the ATL buyer's half of the transferred value.
    amounts = [(charge["party"]["label"], charge["amount"]) for charge in result["charges"]]
    assert amounts == [("Ali", Decimal("2500.00"))]
    # The non-ATL buyer did not match the rule; nothing invented for him.
    assert result["totals_by_currency"] == {"PKR": Decimal("2500.00")}


def test_fixed_rules_apply_once_per_deed_not_per_party():
    rule = _record(calculation_method="fixed_amount", rate_value=None, fixed_amount="5000",
                   eligibility_conditions_json={"when": {"transaction_type": "sale"}})
    context = {"area_marla": Decimal("20"), "transaction_type": "sale"}
    parties = [_buyer("Ali", area="5"), _buyer("Usman", area="15"), _seller("Khalid")]

    result = tax_service.calculate_charges_for_parties(
        rules=[rule], transaction_date=date(2025, 7, 2),
        context=context, parties=parties,
    )

    assert len(result["charges"]) == 1
    assert result["charges"][0]["amount"] == Decimal("5000.00")
    assert "party" not in result["charges"][0]


def test_multi_party_role_without_areas_is_skipped_with_warning():
    rule = _record()
    rule["eligibility_conditions_json"] = {
        "base": "transferred_value", "when": {"buyer_filer_status": "ATL"}}
    context = {"transferred_value": Decimal("200000"), "area_marla": Decimal("20")}
    parties = [_buyer("Ali"), _buyer("Usman")]

    result = tax_service.calculate_charges_for_parties(
        rules=[rule], transaction_date=date(2025, 7, 2),
        context=context, parties=parties,
    )

    assert result["charges"] == []
    assert any("individual area (marla)" in warning for warning in result["warnings"])


def test_solo_party_without_area_covers_whole_side():
    rule = _record()
    rule["eligibility_conditions_json"] = {
        "base": "transferred_value", "when": {"seller_filer_status": "ATL"}}
    context = {"transferred_value": Decimal("100000"), "area_marla": Decimal("20")}
    parties = [_buyer("Ali", area="20"), _seller("Khalid")]

    result = tax_service.calculate_charges_for_parties(
        rules=[rule], transaction_date=date(2025, 7, 2),
        context=context, parties=parties,
    )

    amounts = [(charge["party"]["label"], charge["amount"]) for charge in result["charges"]]
    assert amounts == [("Khalid", Decimal("2500.00"))]


def test_party_area_totals_are_checked_against_the_deed():
    rule = _record()
    rule["eligibility_conditions_json"] = {
        "base": "transferred_value", "when": {"buyer_filer_status": "ATL"}}
    context = {"transferred_value": Decimal("200000"), "area_marla": Decimal("20")}
    parties = [_buyer("Ali", area="10", status="ATL"), _seller("Khalid", area="5")]

    result = tax_service.calculate_charges_for_parties(
        rules=[rule], transaction_date=date(2025, 7, 2),
        context=context, parties=parties,
    )

    assert any("total 15 marla but the deed area is 20 marla" in warning
               for warning in result["warnings"])


def test_exempt_when_filer_status_is_evaluated_per_party():
    rule = _record()
    rule["eligibility_conditions_json"] = {
        "base": "transferred_value", "when": {"transaction_type": "sale"},
        "exempt_when": [{"field": "buyer_filer_status", "equals": "non_ATL"}],
    }
    context = {"transferred_value": Decimal("200000"), "area_marla": Decimal("20"),
               "transaction_type": "sale"}
    parties = [_buyer("Ali", area="10", status="ATL"),
               _buyer("Usman", area="10", status="non_ATL")]

    result = tax_service.calculate_charges_for_parties(
        rules=[rule], transaction_date=date(2025, 7, 2),
        context=context, parties=parties,
    )

    # Only the ATL buyer is charged; the non-ATL buyer's exemption is applied
    # on his own share.
    labels = [charge["party"]["label"] for charge in result["charges"]]
    assert labels == ["Ali"]
    skips = [skip for skip in result["skipped_rules"] if skip.get("party") == "Usman"]
    assert skips, "Usman's exemption should be reported as a party-tagged skip"


def test_seller_rule_never_charges_a_buyer_party():
    # 236C-style: a seller tax keyed on the seller's status. It must apply to
    # the seller's share only, never a second time on a buyer's share.
    seller_rule = _record()
    seller_rule["eligibility_conditions_json"] = {
        "base": "transferred_value", "when": {"seller_filer_status": "non_ATL"}}
    buyer_rule = _record(name="Buyer preset", category="hypothetical_tax")
    buyer_rule["eligibility_conditions_json"] = {
        "base": "transferred_value", "when": {"buyer_filer_status": "ATL"}}
    context = {"transferred_value": Decimal("100000"), "area_marla": Decimal("20")}
    parties = [_buyer("Ali", area="10", status="ATL"),
               _seller("Khalid", area="10", status="non_ATL")]

    result = tax_service.calculate_charges_for_parties(
        rules=[seller_rule, buyer_rule], transaction_date=date(2025, 7, 2),
        context=context, parties=parties,
    )

    charges = [(charge["name"], charge["party"]["label"], charge["amount"])
               for charge in result["charges"]]
    # Buyer rule on Ali's half (2.5% of 50,000) and seller rule on Khalid's
    # half only -- the seller rule must not repeat on the buyer's share.
    assert charges == [
        ("Buyer preset", "Ali", Decimal("1250.00")),
        ("Hypothetical transfer charge", "Khalid", Decimal("1250.00")),
    ]


def test_rule_naming_both_roles_runs_per_pair():
    rule = _record()
    rule["eligibility_conditions_json"] = {
        "base": "transferred_value",
        "when": {"buyer_filer_status": "ATL", "seller_filer_status": "ATL"},
    }
    context = {"transferred_value": Decimal("100000"), "area_marla": Decimal("20")}
    parties = [_buyer("Ali", area="10", status="ATL"),
               _seller("Khalid", area="10", status="ATL")]

    result = tax_service.calculate_charges_for_parties(
        rules=[rule], transaction_date=date(2025, 7, 2),
        context=context, parties=parties,
    )

    charges = [(charge["party"]["label"], charge["amount"]) for charge in result["charges"]]
    # One buyer x one seller: the pair covers the whole deed (1 x 1), so the
    # charge applies once on the full transferred value.
    assert charges == [("Ali × Khalid", Decimal("2500.00"))]
