"""Configurable tax/fee rules and traceable charge calculations.

Rates are user-managed. This module does not ship jurisdictional defaults:
rules are unverified unless a user supplies authoritative source details and
explicitly verifies them.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from typing import Any, Iterable

from sqlalchemy import select

from core.database import SessionLocal
from core.models import TaxFeeRule


class TaxRuleError(ValueError):
    """Raised when a tax/fee rule or calculation input is invalid."""


CENT = Decimal("0.01")
CALCULATION_METHODS = {"percentage", "fixed_amount", "per_unit"}
PERCENTAGE_BASES = {
    "property_value", "transferred_value", "declared_consideration",
    "dc_value", "fbr_value", "registry_written_value", "building_material_value",
}
PAYER_ROLES = {"buyer", "seller", "other"}
CONDITION_FIELDS = {
    "transaction_type", "filer_status", "property_type", "classification",
    "buyer_filer_status", "seller_filer_status",
    "land_type", "buyer_is_overseas_resident", "seller_is_overseas_resident",
    "buyer_exempt", "seller_exempt",
}


def _decimal(value: Any, label: str, *, optional: bool = False) -> Decimal | None:
    if optional and value in (None, ""):
        return None
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError, AttributeError):
        raise TaxRuleError(f"{label} must be a valid decimal number.") from None
    if not result.is_finite():
        raise TaxRuleError(f"{label} must be finite.")
    return result


def _date(value: Any, label: str, *, required: bool = False) -> date | None:
    if value in (None, ""):
        if required:
            raise TaxRuleError(f"{label} is required.")
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise TaxRuleError(f"{label} must use YYYY-MM-DD format.") from None


def _conditions(value: Any) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            raise TaxRuleError("Eligibility conditions must be valid JSON.") from None
    if not isinstance(value, dict):
        raise TaxRuleError("Eligibility conditions must be a JSON object.")
    unknown = set(value) - {
        "base", "payer", "when", "exempt_when", "minimum_base",
        "maximum_base", "unit_key",
    }
    if unknown:
        raise TaxRuleError("Unsupported rule condition key(s): " + ", ".join(sorted(unknown)))
    if value.get("base") not in (None, *PERCENTAGE_BASES):
        raise TaxRuleError("Percentage rule base must name a supported valuation, registry-written, or material value.")
    if value.get("payer") not in (None, *PAYER_ROLES):
        raise TaxRuleError("Payer must be buyer, seller, or other.")
    for key in ("when",):
        conditions = value.get(key, {})
        if not isinstance(conditions, dict):
            raise TaxRuleError(f"{key} must be a JSON object.")
        invalid = set(conditions) - CONDITION_FIELDS
        if invalid:
            raise TaxRuleError("Unsupported eligibility field(s): " + ", ".join(sorted(invalid)))
    exemptions = value.get("exempt_when", [])
    if not isinstance(exemptions, list):
        raise TaxRuleError("exempt_when must be a list of field/value conditions.")
    for item in exemptions:
        if not isinstance(item, dict) or set(item) != {"field", "equals"}:
            raise TaxRuleError("Each exemption must contain exactly 'field' and 'equals'.")
        if item["field"] not in CONDITION_FIELDS:
            raise TaxRuleError(f"Unsupported exemption field: {item['field']}.")
    if "unit_key" in value and value["unit_key"] not in {"area_quantity", "plot_count", "property_count"}:
        raise TaxRuleError("unit_key must be area_quantity, plot_count, or property_count.")
    for key in ("minimum_base", "maximum_base"):
        if key in value and _decimal(value[key], key) < 0:
            raise TaxRuleError(f"{key} cannot be negative.")
    minimum = _decimal(value.get("minimum_base"), "minimum_base", optional=True)
    maximum = _decimal(value.get("maximum_base"), "maximum_base", optional=True)
    if minimum is not None and maximum is not None and maximum < minimum:
        raise TaxRuleError("maximum_base cannot be less than minimum_base.")
    return value


def _rule_data(values: dict[str, Any]) -> dict[str, Any]:
    name = str(values.get("name") or "").strip()
    category = str(values.get("category") or "").strip()
    jurisdiction = str(values.get("jurisdiction") or "").strip()
    if not name or not category or not jurisdiction:
        raise TaxRuleError("Rule name, category, and jurisdiction are required.")
    method = str(values.get("calculation_method") or "").strip().lower()
    if method not in CALCULATION_METHODS:
        raise TaxRuleError("Choose percentage, fixed_amount, or per_unit calculation.")
    rate = _decimal(values.get("rate_value"), "Rate", optional=True)
    fixed = _decimal(values.get("fixed_amount"), "Fixed amount", optional=True)
    if method == "fixed_amount":
        if fixed is None or fixed < 0 or rate is not None:
            raise TaxRuleError("A fixed rule needs a non-negative fixed amount and no rate.")
    elif rate is None or rate < 0 or fixed is not None:
        raise TaxRuleError("Percentage and per-unit rules need a non-negative rate and no fixed amount.")
    if method == "percentage":
        conditions = _conditions(values.get("eligibility_conditions_json"))
        if conditions.get("base") not in PERCENTAGE_BASES:
            raise TaxRuleError("Percentage rules must explicitly name their calculation base.")
    else:
        conditions = _conditions(values.get("eligibility_conditions_json"))
    if method == "per_unit" and (not conditions.get("unit_key") or not values.get("rate_unit")):
        raise TaxRuleError("Per-unit rules must explicitly name both unit_key and rate_unit.")
    minimum = _decimal(values.get("minimum_amount"), "Minimum amount", optional=True)
    maximum = _decimal(values.get("maximum_amount"), "Maximum amount", optional=True)
    if minimum is not None and minimum < 0 or maximum is not None and maximum < 0:
        raise TaxRuleError("Charge minimum and maximum cannot be negative.")
    if minimum is not None and maximum is not None and maximum < minimum:
        raise TaxRuleError("Maximum charge cannot be less than minimum charge.")
    start = _date(values.get("effective_from"), "Effective from", required=True)
    end = _date(values.get("effective_to"), "Effective to")
    if end and end < start:
        raise TaxRuleError("Effective to cannot be before Effective from.")
    status = str(values.get("verification_status") or "unverified").lower()
    if status not in {"unverified", "verified"}:
        raise TaxRuleError("Verification status must be unverified or verified.")
    source = str(values.get("source_reference") or "").strip() or None
    verifier = str(values.get("verified_by") or "").strip() or None
    if status == "verified" and (not source or not verifier):
        raise TaxRuleError("A verified rule requires its source reference and verifier name.")
    currency = str(values.get("currency_code") or "PKR").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise TaxRuleError("Currency code must be a three-letter code.")
    return {
        "name": name,
        "category": category,
        "jurisdiction": jurisdiction,
        "calculation_method": method,
        "rate_value": rate,
        "fixed_amount": fixed,
        "rate_unit": str(values.get("rate_unit") or "").strip() or None,
        "minimum_amount": minimum,
        "maximum_amount": maximum,
        "currency_code": currency,
        "effective_from": start,
        "effective_to": end,
        "eligibility_conditions_json": json.dumps(conditions, sort_keys=True),
        "source_reference": source,
        "source_date": _date(values.get("source_date"), "Source date"),
        "verification_status": status,
        "verified_by": verifier if status == "verified" else None,
    }


def _record(rule: TaxFeeRule) -> dict[str, Any]:
    return {
        field: getattr(rule, field)
        for field in (
            "id", "name", "category", "jurisdiction", "calculation_method",
            "rate_value", "fixed_amount", "rate_unit", "minimum_amount",
            "maximum_amount", "currency_code", "effective_from", "effective_to",
            "eligibility_conditions_json", "source_reference", "source_date",
            "verification_status", "verified_at", "verified_by", "created_at", "updated_at",
        )
    }


def create_tax_fee_rule(**values: Any) -> int:
    """Validate and persist one versioned rule; rules are never auto-merged."""
    data = _rule_data(values)
    with SessionLocal.begin() as session:
        rule = TaxFeeRule(**data)
        if data["verification_status"] == "verified":
            from datetime import datetime, timezone
            rule.verified_at = datetime.now(timezone.utc)
        session.add(rule)
        session.flush()
        return rule.id


def update_tax_fee_rule(*, rule_id: int, **values: Any) -> None:
    """Update a rule explicitly; old estimates retain their own snapshots."""
    data = _rule_data(values)
    with SessionLocal.begin() as session:
        rule = session.get(TaxFeeRule, rule_id)
        if rule is None:
            raise TaxRuleError("Tax or fee rule was not found.")
        for field, value in data.items():
            setattr(rule, field, value)
        if data["verification_status"] == "verified":
            from datetime import datetime, timezone
            rule.verified_at = datetime.now(timezone.utc)
        else:
            rule.verified_at = None


def list_tax_fee_rules(*, jurisdiction: str | None = None, category: str | None = None) -> list[dict[str, Any]]:
    statement = select(TaxFeeRule).order_by(TaxFeeRule.jurisdiction, TaxFeeRule.category, TaxFeeRule.name, TaxFeeRule.id)
    if jurisdiction:
        statement = statement.where(TaxFeeRule.jurisdiction == jurisdiction)
    if category:
        statement = statement.where(TaxFeeRule.category == category)
    with SessionLocal() as session:
        return [_record(rule) for rule in session.scalars(statement).all()]


def _snapshot(rule: dict[str, Any], conditions: dict[str, Any]) -> dict[str, Any]:
    return {
        "rule_id": rule.get("id"),
        "name": rule["name"],
        "category": rule["category"],
        "jurisdiction": rule["jurisdiction"],
        "calculation_method": rule["calculation_method"],
        "rate_value": str(rule["rate_value"]) if rule.get("rate_value") is not None else None,
        "fixed_amount": str(rule["fixed_amount"]) if rule.get("fixed_amount") is not None else None,
        "rate_unit": rule.get("rate_unit"),
        "minimum_amount": str(rule["minimum_amount"]) if rule.get("minimum_amount") is not None else None,
        "maximum_amount": str(rule["maximum_amount"]) if rule.get("maximum_amount") is not None else None,
        "currency_code": rule.get("currency_code", "PKR"),
        "effective_from": rule["effective_from"].isoformat(),
        "effective_to": rule["effective_to"].isoformat() if rule.get("effective_to") else None,
        "conditions": conditions,
        "source_reference": rule.get("source_reference"),
        "source_date": rule["source_date"].isoformat() if rule.get("source_date") else None,
        "verification_status": rule.get("verification_status", "unverified"),
        "verified_by": rule.get("verified_by"),
    }


def calculate_charges(
    *,
    rules: Iterable[dict[str, Any]],
    transaction_date: date,
    context: dict[str, Any],
    jurisdiction: str | Iterable[str] | None = None,
) -> dict[str, Any]:
    """Calculate applicable configured charges without guessing missing inputs.

    Each rule may specify `when` exact-match conditions, `exempt_when` clauses,
    payer, percent base, and optional charge caps in eligibility JSON.
    """
    if not isinstance(transaction_date, date):
        raise TaxRuleError("Transaction date must be a date.")
    charges: list[dict[str, Any]] = []
    warnings: list[str] = []
    skipped: list[dict[str, str]] = []
    total_by_currency: dict[str, Decimal] = {}
    if isinstance(jurisdiction, str):
        jurisdictions = {part.strip().casefold() for part in jurisdiction.split(",") if part.strip()}
    else:
        jurisdictions = {str(part).strip().casefold() for part in jurisdiction or () if str(part).strip()}
    for rule in rules:
        if jurisdictions and str(rule.get("jurisdiction", "")).casefold() not in jurisdictions:
            continue
        start, end = rule.get("effective_from"), rule.get("effective_to")
        if not start or start > transaction_date or (end and end < transaction_date):
            continue
        try:
            conditions = _conditions(rule.get("eligibility_conditions_json"))
        except TaxRuleError as exc:
            raise TaxRuleError(f"Rule {rule.get('id', rule.get('name'))}: {exc}") from None
        when = conditions.get("when", {})
        missing = [field for field in when
                   if field not in context or context[field] in (None, "", "unknown")]
        if missing:
            skipped.append({"rule": rule["name"], "reason": "Missing eligibility input(s): " + ", ".join(missing)})
            continue
        if any(str(context[field]).casefold() != str(expected).casefold() for field, expected in when.items()):
            continue
        exempt = next((item for item in conditions.get("exempt_when", [])
                       if item["field"] in context and context[item["field"]] == item["equals"]), None)
        if exempt:
            skipped.append({"rule": rule["name"], "reason": f"Exempt when {exempt['field']} equals {exempt['equals']}."})
            continue
        unknown_exemptions = [item["field"] for item in conditions.get("exempt_when", [])
                              if item["field"] not in context
                              or context[item["field"]] in (None, "", "unknown")]
        if unknown_exemptions:
            skipped.append({"rule": rule["name"], "reason": "Missing exemption input(s): " + ", ".join(unknown_exemptions)})
            continue

        method = rule.get("calculation_method")
        currency = rule.get("currency_code", "PKR")
        base = None
        applied_rate = None
        if method == "percentage":
            base_key = conditions.get("base")
            if base_key not in PERCENTAGE_BASES:
                raise TaxRuleError(f"Rule {rule['name']} has no valid percentage base.")
            if base_key not in context or context[base_key] in (None, "", "unknown"):
                skipped.append({"rule": rule["name"], "reason": f"Missing calculation base: {base_key}."})
                continue
            base = _decimal(context[base_key], base_key)
            applied_rate = _decimal(rule.get("rate_value"), "Rule rate")
            if base < 0 or applied_rate < 0:
                raise TaxRuleError("Charge base and rate cannot be negative.")
            threshold_min = _decimal(conditions.get("minimum_base"), "Minimum base", optional=True)
            threshold_max = _decimal(conditions.get("maximum_base"), "Maximum base", optional=True)
            if threshold_min is not None and base < threshold_min:
                continue
            if threshold_max is not None and base > threshold_max:
                continue
            exact = base * applied_rate / Decimal(100)
        elif method == "fixed_amount":
            exact = _decimal(rule.get("fixed_amount"), "Fixed rule amount")
            if exact < 0:
                raise TaxRuleError("Fixed charge cannot be negative.")
        elif method == "per_unit":
            unit_key = conditions.get("unit_key")
            if not unit_key or unit_key not in context:
                skipped.append({"rule": rule["name"], "reason": f"Missing per-unit quantity: {unit_key or 'unit_key'} ."})
                continue
            context_unit_key = {
                "area_quantity": "area_rate_unit",
                "plot_count": "plot_unit",
                "property_count": "property_unit",
            }[unit_key]
            if context.get(context_unit_key) in (None, "", "unknown"):
                skipped.append({"rule": rule["name"], "reason": f"Missing unit for quantity {unit_key}."})
                continue
            if str(context[context_unit_key]).casefold() != str(rule.get("rate_unit") or "").casefold():
                skipped.append({
                    "rule": rule["name"],
                    "reason": f"Configured rate unit {rule.get('rate_unit')} does not match {context[context_unit_key]}.",
                })
                continue
            base = _decimal(context[unit_key], unit_key)
            applied_rate = _decimal(rule.get("rate_value"), "Rule rate")
            if base < 0 or applied_rate < 0:
                raise TaxRuleError("Per-unit quantity and rate cannot be negative.")
            exact = base * applied_rate
        else:
            raise TaxRuleError(f"Rule {rule.get('name')} has an unsupported calculation method.")

        minimum = _decimal(rule.get("minimum_amount"), "Minimum amount", optional=True)
        maximum = _decimal(rule.get("maximum_amount"), "Maximum amount", optional=True)
        if minimum is not None:
            exact = max(exact, minimum)
        if maximum is not None:
            exact = min(exact, maximum)
        amount = exact.quantize(CENT, rounding=ROUND_HALF_UP)
        snapshot = _snapshot(rule, conditions)
        charges.append({
            "tax_fee_rule_id": rule.get("id"),
            "name": rule["name"],
            "category": rule["category"],
            "payer": conditions.get("payer"),
            "calculation_method": method,
            "calculation_base": base.quantize(CENT, rounding=ROUND_HALF_UP) if base is not None else None,
            "applied_rate": applied_rate,
            "amount": amount,
            "currency_code": currency,
            "rule_snapshot": snapshot,
        })
        total_by_currency[currency] = total_by_currency.get(currency, Decimal(0)) + amount
        if rule.get("verification_status", "unverified") != "verified":
            warnings.append(f"{rule['name']} is unverified; its charge is an estimate.")
        if not rule.get("source_reference"):
            warnings.append(f"{rule['name']} has no recorded source reference.")

    totals = {currency: amount.quantize(CENT, rounding=ROUND_HALF_UP)
              for currency, amount in total_by_currency.items()}
    if len(totals) > 1:
        warnings.append("Charges use multiple currencies; review each currency total separately.")
    single_total = next(iter(totals.values())) if len(totals) == 1 else Decimal("0.00")
    single_currency = next(iter(totals)) if len(totals) == 1 else "PKR"
    return {
        "charges": charges,
        "totals_by_currency": totals,
        "total_charges": single_total,
        "total_currency": single_currency,
        "warnings": warnings,
        "skipped_rules": skipped,
    }
