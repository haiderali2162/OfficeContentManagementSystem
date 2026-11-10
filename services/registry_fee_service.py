"""Safe persistence and evaluation for user-configured registry fee formulas."""

from __future__ import annotations

import ast
from datetime import date, datetime, timezone
from decimal import Decimal, DivisionByZero, InvalidOperation, ROUND_HALF_UP
import json
import re
from typing import Any, Iterable

from sqlalchemy import select

from core.database import SessionLocal
from core.models import RegistryFeeCustomField, RegistryFeeFormula


class RegistryFeeFormulaError(ValueError):
    """Raised when a saved registry-fee formula is invalid or unsafe."""


CENT = Decimal("0.01")
FORMULA_VARIABLES = {
    "area", "area_marla", "area_kanal", "area_acre", "area_square_feet",
    "dc_value", "fbr_value", "registry_written_value", "building_material_value",
}
FORMULA_MATCH_FIELDS = {
    "land_type", "mauza", "square_no", "kila_no", "khasra_no",
    "property_type", "classification", "transaction_type", "has_fbr_schedule",
}


def _date(value: Any, label: str, *, required: bool = False) -> date | None:
    if value in (None, ""):
        if required:
            raise RegistryFeeFormulaError(f"{label} is required.")
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise RegistryFeeFormulaError(f"{label} must use YYYY-MM-DD format.") from None


def _normalize_expression(expression: Any) -> str:
    value = str(expression or "").strip()
    if not value:
        raise RegistryFeeFormulaError("Enter a fee formula.")
    if len(value) > 500:
        raise RegistryFeeFormulaError("Fee formulas must be 500 characters or shorter.")
    # Accept common registry notation: x/× for multiply, ÷ for divide, and
    # 2.5% as 2.5 / 100. Brackets are treated as grouping parentheses.
    value = value.replace("×", "*").replace("÷", "/").replace("[", "(").replace("]", ")")
    value = re.sub(r"(?i)\bx\b", "*", value)
    value = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"(\1/100)", value)
    return value


def _parse_expression(expression: Any, allowed_variables: Iterable[str] = ()) -> ast.Expression:
    normalized = _normalize_expression(expression)
    try:
        parsed = ast.parse(normalized, mode="eval")
    except SyntaxError:
        raise RegistryFeeFormulaError("Use numbers, allowed value names, + - * /, and parentheses.") from None

    variable_names = FORMULA_VARIABLES | {str(key).casefold() for key in allowed_variables}

    def inspect_node(node: ast.AST) -> None:
        if isinstance(node, ast.Expression):
            inspect_node(node.body)
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise RegistryFeeFormulaError("Only numeric constants are allowed in a formula.")
        elif isinstance(node, ast.Name):
            if node.id.casefold() not in variable_names:
                raise RegistryFeeFormulaError(f"Unknown formula value '{node.id}'.")
        elif isinstance(node, ast.BinOp) and isinstance(
            node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)
        ):
            inspect_node(node.left)
            inspect_node(node.right)
        elif isinstance(node, ast.IfExp):
            inspect_node(node.test)
            inspect_node(node.body)
            inspect_node(node.orelse)
        elif isinstance(node, ast.Compare):
            if len(node.ops) != 1 or not isinstance(
                node.ops[0], (ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq)
            ):
                raise RegistryFeeFormulaError("Use one numeric comparison in a conditional formula.")
            inspect_node(node.left)
            inspect_node(node.comparators[0])
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            inspect_node(node.operand)
        else:
            raise RegistryFeeFormulaError(
                "Formula functions, attributes, and unsupported Python expressions are not allowed."
            )

    inspect_node(parsed)
    return parsed


def validate_formula(expression: Any, allowed_variables: Iterable[str] = ()) -> str:
    """Return a normalized formula after checking its safe arithmetic grammar."""
    return ast.unparse(_parse_expression(expression, allowed_variables))


def evaluate_formula(
    expression: Any, values: dict[str, Any], allowed_variables: Iterable[str] = (),
) -> Decimal:
    """Evaluate whitelisted arithmetic with Decimal and no Python eval()."""
    tree = _parse_expression(expression, allowed_variables)
    normalized_values = {str(key).casefold(): value for key, value in values.items()}

    def evaluate(node: ast.AST) -> Decimal:
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant):
            return Decimal(str(node.value))
        if isinstance(node, ast.Name):
            key = node.id.casefold()
            raw = normalized_values.get(key)
            if raw in (None, "", "unknown"):
                raise RegistryFeeFormulaError(f"Formula value '{key}' is not available for this estimate.")
            try:
                number = Decimal(str(raw))
            except (InvalidOperation, ValueError, TypeError):
                raise RegistryFeeFormulaError(f"Formula value '{key}' must be numeric.") from None
            if not number.is_finite():
                raise RegistryFeeFormulaError(f"Formula value '{key}' must be finite.")
            return number
        if isinstance(node, ast.UnaryOp):
            value = evaluate(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.IfExp):
            return evaluate(node.body) if evaluate_condition(node.test) else evaluate(node.orelse)
        if isinstance(node, ast.Compare):
            left, right = evaluate(node.left), evaluate(node.comparators[0])
            operation = node.ops[0]
            return Decimal(1) if (
                left < right if isinstance(operation, ast.Lt) else
                left <= right if isinstance(operation, ast.LtE) else
                left > right if isinstance(operation, ast.Gt) else
                left >= right if isinstance(operation, ast.GtE) else
                left == right if isinstance(operation, ast.Eq) else left != right
            ) else Decimal(0)
        if isinstance(node, ast.BinOp):
            left, right = evaluate(node.left), evaluate(node.right)
            try:
                if isinstance(node.op, ast.Add):
                    return left + right
                if isinstance(node.op, ast.Sub):
                    return left - right
                if isinstance(node.op, ast.Mult):
                    return left * right
                if isinstance(node.op, ast.Div):
                    return left / right
                if isinstance(node.op, ast.Pow):
                    if right != right.to_integral_value() or abs(right) > 12:
                        raise RegistryFeeFormulaError("Formula powers must be whole numbers between -12 and 12.")
                    return left ** int(right)
            except (DivisionByZero, ZeroDivisionError):
                raise RegistryFeeFormulaError("Formula divides by zero.") from None
            except InvalidOperation:
                raise RegistryFeeFormulaError("Formula arithmetic is outside the supported numeric range.") from None
        raise RegistryFeeFormulaError("Unsupported formula expression.")

    def evaluate_condition(node: ast.AST) -> bool:
        if not isinstance(node, ast.Compare):
            return evaluate(node) != 0
        left, right = evaluate(node.left), evaluate(node.comparators[0])
        operation = node.ops[0]
        if isinstance(operation, ast.Lt):
            return left < right
        if isinstance(operation, ast.LtE):
            return left <= right
        if isinstance(operation, ast.Gt):
            return left > right
        if isinstance(operation, ast.GtE):
            return left >= right
        if isinstance(operation, ast.Eq):
            return left == right
        return left != right

    result = evaluate(tree)
    if not result.is_finite():
        raise RegistryFeeFormulaError("Formula result must be finite.")
    if result < 0:
        raise RegistryFeeFormulaError("A registry fee formula cannot produce a negative charge.")
    return result.quantize(CENT, rounding=ROUND_HALF_UP)


def _conditions(value: Any) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            raise RegistryFeeFormulaError("Formula conditions must be valid JSON.") from None
    if not isinstance(value, dict) or set(value) - {"when"}:
        raise RegistryFeeFormulaError("Formula conditions must be an object containing an optional 'when' object.")
    when = value.get("when", {})
    if not isinstance(when, dict):
        raise RegistryFeeFormulaError("Formula 'when' must be a JSON object.")
    unknown = set(when) - FORMULA_MATCH_FIELDS
    if unknown:
        raise RegistryFeeFormulaError("Unsupported formula match field(s): " + ", ".join(sorted(unknown)))
    return {"when": when} if when else {}


def _formula_data(values: dict[str, Any]) -> dict[str, Any]:
    name = str(values.get("name") or "").strip()
    category = str(values.get("category") or "").strip()
    jurisdiction = str(values.get("jurisdiction") or "Punjab").strip()
    if not name or not category or not jurisdiction:
        raise RegistryFeeFormulaError("Formula name, fee category, and jurisdiction are required.")
    formula = str(values.get("formula_expression") or "").strip()
    _parse_expression(formula, _registered_custom_field_keys())
    start = _date(values.get("effective_from") or date.today(), "Effective from", required=True)
    end = _date(values.get("effective_to"), "Effective to")
    if end and end < start:
        raise RegistryFeeFormulaError("Effective to cannot be before Effective from.")
    status = str(values.get("verification_status") or "unverified").strip().lower()
    if status not in {"unverified", "verified"}:
        raise RegistryFeeFormulaError("Verification status must be unverified or verified.")
    source = str(values.get("source_reference") or "").strip() or None
    verifier = str(values.get("verified_by") or "").strip() or None
    if status == "verified" and (not source or not verifier):
        raise RegistryFeeFormulaError("A verified formula requires a source reference and verifier name.")
    currency = str(values.get("currency_code") or "PKR").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise RegistryFeeFormulaError("Currency code must be a three-letter code.")
    payer = str(values.get("payer") or "").strip() or None
    if payer and payer not in {"buyer", "seller", "other"}:
        raise RegistryFeeFormulaError("Payer must be buyer, seller, or other.")
    return {
        "name": name,
        "category": category,
        "jurisdiction": jurisdiction,
        "formula_expression": formula,
        "payer": payer,
        "currency_code": currency,
        "effective_from": start,
        "effective_to": end,
        "eligibility_conditions_json": json.dumps(
            _conditions(values.get("eligibility_conditions_json")), sort_keys=True
        ),
        "source_reference": source,
        "source_date": _date(values.get("source_date"), "Source date"),
        "verification_status": status,
        "verified_by": verifier if status == "verified" else None,
    }


def _record(formula: RegistryFeeFormula) -> dict[str, Any]:
    return {
        field: getattr(formula, field)
        for field in (
            "id", "name", "category", "jurisdiction", "formula_expression", "payer",
            "currency_code", "effective_from", "effective_to",
            "eligibility_conditions_json", "source_reference", "source_date",
            "verification_status", "verified_at", "verified_by", "created_at", "updated_at",
        )
    }


def create_registry_fee_formula(**values: Any) -> int:
    data = _formula_data(values)
    with SessionLocal.begin() as session:
        record = RegistryFeeFormula(**data)
        if data["verification_status"] == "verified":
            record.verified_at = datetime.now(timezone.utc)
        session.add(record)
        session.flush()
        return record.id


def update_registry_fee_formula(*, formula_id: int, **values: Any) -> None:
    data = _formula_data(values)
    with SessionLocal.begin() as session:
        record = session.get(RegistryFeeFormula, formula_id)
        if record is None:
            raise RegistryFeeFormulaError("Registry fee formula was not found.")
        for field, value in data.items():
            setattr(record, field, value)
        record.verified_at = datetime.now(timezone.utc) if data["verification_status"] == "verified" else None


def delete_registry_fee_formula(formula_id: int) -> None:
    """Remove one saved formula explicitly."""
    with SessionLocal.begin() as session:
        record = session.get(RegistryFeeFormula, formula_id)
        if record is None:
            raise RegistryFeeFormulaError("Registry fee formula was not found.")
        session.delete(record)


def list_registry_fee_formulas() -> list[dict[str, Any]]:
    statement = select(RegistryFeeFormula).order_by(
        RegistryFeeFormula.category, RegistryFeeFormula.name, RegistryFeeFormula.id
    )
    with SessionLocal() as session:
        return [_record(item) for item in session.scalars(statement).all()]


def _registered_custom_field_keys() -> set[str]:
    with SessionLocal() as session:
        return set(session.scalars(select(RegistryFeeCustomField.field_key)).all())


def list_registry_fee_custom_fields() -> list[dict[str, Any]]:
    """List numeric formula inputs in stable display order."""
    with SessionLocal() as session:
        rows = session.scalars(select(RegistryFeeCustomField).order_by(
            RegistryFeeCustomField.label, RegistryFeeCustomField.id
        )).all()
        return [{"id": row.id, "field_key": row.field_key, "label": row.label}
                for row in rows]


def create_registry_fee_custom_field(*, label: str, field_key: str) -> int:
    """Create one numeric custom input available to calculator formulas."""
    clean_label = str(label or "").strip()
    key = str(field_key or "").strip().casefold()
    if not clean_label:
        raise RegistryFeeFormulaError("Enter a label for the custom field.")
    if len(clean_label) > 120:
        raise RegistryFeeFormulaError("Custom field labels must be 120 characters or shorter.")
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,63}", key):
        raise RegistryFeeFormulaError("Use a formula key with lowercase letters, numbers, and underscores.")
    if key in FORMULA_VARIABLES:
        raise RegistryFeeFormulaError("That key is already reserved by a built-in formula value.")
    with SessionLocal.begin() as session:
        if session.scalar(select(RegistryFeeCustomField.id).where(
            RegistryFeeCustomField.field_key == key
        )):
            raise RegistryFeeFormulaError(f"The custom field '{key}' already exists.")
        field = RegistryFeeCustomField(field_key=key, label=clean_label)
        session.add(field)
        session.flush()
        return field.id


def delete_registry_fee_custom_field(field_id: int) -> None:
    """Delete an unused custom formula input; protect fields referenced by formulas."""
    with SessionLocal.begin() as session:
        field = session.get(RegistryFeeCustomField, field_id)
        if field is None:
            raise RegistryFeeFormulaError("Custom field was not found.")
        formula_expressions = session.scalars(select(RegistryFeeFormula.formula_expression)).all()
        pattern = re.compile(rf"\b{re.escape(field.field_key)}\b", re.IGNORECASE)
        if any(pattern.search(expression or "") for expression in formula_expressions):
            raise RegistryFeeFormulaError("This field is referenced by a saved formula and cannot be removed.")
        session.delete(field)


def calculate_registry_fee_formulas(
    *,
    formulas: Iterable[dict[str, Any]],
    effective_on: date,
    context: dict[str, Any],
    jurisdictions: Iterable[str] = ("Punjab",),
    custom_variables: Iterable[str] = (),
) -> dict[str, Any]:
    """Calculate date-valid, matching formulas and preserve their source snapshots."""
    accepted_jurisdictions = {str(item).strip().casefold() for item in jurisdictions}
    charges: list[dict[str, Any]] = []
    warnings: list[str] = []
    skipped: list[dict[str, str]] = []
    totals: dict[str, Decimal] = {}
    for formula in formulas:
        start, end = formula.get("effective_from"), formula.get("effective_to")
        if start and start > effective_on or end and end < effective_on:
            continue
        if accepted_jurisdictions and str(formula.get("jurisdiction", "")).casefold() not in accepted_jurisdictions:
            continue
        conditions = _conditions(formula.get("eligibility_conditions_json"))
        when = conditions.get("when", {})
        missing = [key for key in when if context.get(key) in (None, "", "unknown")]
        if missing:
            skipped.append({"rule": formula["name"], "reason": "Missing formula match input(s): " + ", ".join(missing)})
            continue
        if any(str(context.get(key)).casefold() != str(value).casefold() for key, value in when.items()):
            continue
        try:
            amount = evaluate_formula(
                formula["formula_expression"], context, allowed_variables=custom_variables
            )
        except RegistryFeeFormulaError as exc:
            skipped.append({"rule": formula["name"], "reason": str(exc)})
            continue
        currency = formula.get("currency_code", "PKR")
        snapshot = {
            "formula_id": formula.get("id"),
            "name": formula["name"],
            "category": formula["category"],
            "jurisdiction": formula.get("jurisdiction"),
            "formula_expression": formula["formula_expression"],
            "payer": formula.get("payer"),
            "currency_code": currency,
            "effective_from": start.isoformat() if start else None,
            "effective_to": end.isoformat() if end else None,
            "conditions": conditions,
            "source_reference": formula.get("source_reference"),
            "source_date": formula["source_date"].isoformat() if formula.get("source_date") else None,
            "verification_status": formula.get("verification_status", "unverified"),
            "verified_by": formula.get("verified_by"),
        }
        charges.append({
            "tax_fee_rule_id": None,
            "name": formula["name"],
            "category": formula["category"],
            "payer": formula.get("payer"),
            "calculation_method": "formula",
            "calculation_base": None,
            "applied_rate": None,
            "amount": amount,
            "currency_code": currency,
            "rule_snapshot": snapshot,
        })
        totals[currency] = totals.get(currency, Decimal(0)) + amount
        if formula.get("verification_status", "unverified") != "verified":
            warnings.append(f"{formula['name']} is unverified; the calculated fee is an estimate.")
        if not formula.get("source_reference"):
            warnings.append(f"{formula['name']} has no recorded source reference.")
    totals = {currency: value.quantize(CENT, rounding=ROUND_HALF_UP) for currency, value in totals.items()}
    return {"charges": charges, "totals_by_currency": totals, "warnings": warnings, "skipped_rules": skipped}
