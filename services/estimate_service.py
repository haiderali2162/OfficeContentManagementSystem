"""Persist and reopen valuation estimates with immutable calculation snapshots."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from core.database import SessionLocal
from core.models import (
    Client,
    DCValuationRate,
    PropertyLocation,
    ValuationEstimate,
    ValuationEstimateCharge,
)


class EstimateServiceError(ValueError):
    """Raised when a valuation estimate cannot be saved or loaded."""


def list_client_choices() -> list[dict[str, Any]]:
    """Read only client IDs and names for the optional estimate selector."""
    with SessionLocal() as session:
        rows = session.execute(
            select(Client.id, Client.name).order_by(func.lower(Client.name), Client.id)
        ).all()
        return [{"id": row.id, "name": row.name} for row in rows]


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Cannot serialize {type(value).__name__} to JSON.")


def _snapshot_json(value: Any) -> str:
    return json.dumps(value, default=_json_default, ensure_ascii=False, sort_keys=True)


def save_estimate(
    *,
    inputs: dict[str, Any],
    valuation: dict[str, Any],
    charges: dict[str, Any],
    client_id: int | None = None,
) -> int:
    """Save inputs, outputs, and source snapshots in one transaction."""
    location_id = inputs.get("location_id")
    rate_id = valuation.get("rate_id")
    if not location_id:
        raise EstimateServiceError("A property location is required to save an estimate.")
    if client_id is None:
        raise EstimateServiceError("Select a client before saving the estimate.")
    transaction_date = inputs.get("transaction_date")
    if isinstance(transaction_date, str):
        try:
            transaction_date = date.fromisoformat(transaction_date)
        except ValueError:
            raise EstimateServiceError("Transaction date must use YYYY-MM-DD format.") from None
    if not isinstance(transaction_date, date):
        raise EstimateServiceError("A valid transaction date is required to save an estimate.")

    with SessionLocal.begin() as session:
        client = session.get(Client, client_id) if client_id is not None else None
        if client_id is not None and client is None:
            raise EstimateServiceError("The selected client record no longer exists.")
        location = session.get(PropertyLocation, location_id)
        if location is None:
            raise EstimateServiceError("The selected property location no longer exists.")
        # Older location profiles may store their Mauza only as the profile
        # name. Use that value as the display snapshot when no separate field
        # exists, while still rejecting a genuinely blank location.
        mauza_name = str(inputs.get("mauza") or location.mauza or location.name or "").strip()
        if not mauza_name:
            raise EstimateServiceError("A Mauza is required to save the estimate.")
        rate = session.get(DCValuationRate, rate_id) if rate_id is not None else None
        if rate_id is not None and rate is None:
            raise EstimateServiceError("The selected valuation rate no longer exists.")

        # Persist the canonical area plus original input text in the snapshot;
        # this supports K-M-F inputs and keeps the database numeric column valid.
        estimate = ValuationEstimate(
            client_id=client_id,
            location_id=location.id,
            dc_rate_id=rate.id if rate else None,
            transaction_date=transaction_date,
            property_type=str(inputs.get("property_type") or "Property"),
            classification=inputs.get("classification"),
            area_value=Decimal(str(valuation["area_square_feet"])),
            area_unit="square_foot",
            share_numerator=int(inputs.get("share_numerator", 1)),
            share_denominator=int(inputs.get("share_denominator", 1)),
            declared_consideration=valuation.get("declared_consideration"),
            property_value=valuation["property_value"],
            transferred_value=valuation["transferred_value"],
            # The estimate summary column is PKR. Other currencies remain
            # separately itemized in the result snapshot and charge rows.
            total_charges=charges.get("totals_by_currency", {}).get("PKR", Decimal("0.00")),
            currency_code="PKR",
            input_snapshot_json=_snapshot_json({**inputs, "mauza": mauza_name}),
            client_snapshot_json=_snapshot_json({"name": client.name}) if client else None,
            location_snapshot_json=_snapshot_json({
                "name": location.name,
                "district": location.district,
                "tehsil": location.tehsil,
                "land_type": location.land_type,
                "mauza": mauza_name,
                "committee_town": location.committee_town,
                "revenue_circle": location.revenue_circle,
                "property_area": location.property_area,
                "locality": location.locality,
            }),
            rate_snapshot_json=_snapshot_json(valuation.get("rate_snapshot") or {
                "id": rate.id,
                "schedule_type": rate.schedule_type,
                "property_type": rate.property_type,
                "classification": rate.classification,
                "floor": rate.floor,
                "rate_value": rate.rate_value,
                "rate_unit": rate.rate_unit,
                "published_year": rate.published_year,
                "effective_from": rate.effective_from,
                "effective_to": rate.effective_to,
                "source_reference": rate.source_reference,
                "verification_status": rate.verification_status,
            }) if rate else _snapshot_json(valuation.get("rate_snapshot") or valuation.get("rate", {})),
            result_snapshot_json=_snapshot_json({"valuation": valuation, "charges": charges}),
            warnings_json=_snapshot_json(valuation.get("warnings", []) + charges.get("warnings", [])),
        )
        for charge in charges.get("charges", []):
            estimate.charges.append(ValuationEstimateCharge(
                tax_fee_rule_id=charge.get("tax_fee_rule_id"),
                name=charge["name"],
                category=charge["category"],
                payer=charge.get("payer"),
                calculation_method=charge["calculation_method"],
                calculation_base=charge.get("calculation_base"),
                applied_rate=charge.get("applied_rate"),
                amount=charge["amount"],
                currency_code=charge.get("currency_code", "PKR"),
                rule_snapshot_json=_snapshot_json(charge["rule_snapshot"]),
            ))
        session.add(estimate)
        session.flush()
        return estimate.id


def list_estimates(*, limit: int = 100) -> list[dict[str, Any]]:
    """Return recent saved estimates for the history list."""
    safe_limit = max(1, min(int(limit), 500))
    statement = (
        select(ValuationEstimate)
        .options(joinedload(ValuationEstimate.client))
        .order_by(ValuationEstimate.transaction_date.desc(), ValuationEstimate.id.desc())
        .limit(safe_limit)
    )
    with SessionLocal() as session:
        return [
            {
                "id": item.id,
                "transaction_date": item.transaction_date,
                "client_name": item.client.name if item.client else None,
                "property_type": item.property_type,
                "classification": item.classification,
                "transferred_value": item.transferred_value,
                "total_charges": item.total_charges,
                "currency_code": item.currency_code,
            }
            for item in session.scalars(statement).all()
        ]


def get_estimate(estimate_id: int) -> dict[str, Any] | None:
    """Load the complete saved result and charge snapshots for review."""
    statement = (
        select(ValuationEstimate)
        .options(
            joinedload(ValuationEstimate.client),
            joinedload(ValuationEstimate.charges),
        )
        .where(ValuationEstimate.id == estimate_id)
    )
    with SessionLocal() as session:
        item = session.execute(statement).unique().scalar_one_or_none()
        if item is None:
            return None
        charges_rows = _charge_rows_with_party(item)
        return {
            "id": item.id,
            "transaction_date": item.transaction_date,
            "client_name": item.client.name if item.client else None,
            "property_type": item.property_type,
            "classification": item.classification,
            "area_value": item.area_value,
            "area_unit": item.area_unit,
            "share_numerator": item.share_numerator,
            "share_denominator": item.share_denominator,
            "declared_consideration": item.declared_consideration,
            "property_value": item.property_value,
            "transferred_value": item.transferred_value,
            "total_charges": item.total_charges,
            "currency_code": item.currency_code,
            "input_snapshot": json.loads(item.input_snapshot_json),
            "location_snapshot": json.loads(item.location_snapshot_json) if item.location_snapshot_json else None,
            "rate_snapshot": json.loads(item.rate_snapshot_json) if item.rate_snapshot_json else None,
            "result_snapshot": json.loads(item.result_snapshot_json) if item.result_snapshot_json else None,
            "warnings": json.loads(item.warnings_json) if item.warnings_json else [],
            "charges": charges_rows,
        }


def _charge_rows_with_party(item: ValuationEstimate) -> list[dict[str, Any]]:
    """Rebuild charge rows and restore per-party labels from the snapshot.

    The charge table has no party column; the full result snapshot recorded
    at save time does. Both lists were written in the same order from one
    calculation, so rows are matched by position to surface which buyer or
    seller each filer-dependent charge belongs to in saved-estimate reports.
    """
    rows = [
        {
            "name": charge.name,
            "category": charge.category,
            "payer": charge.payer,
            "calculation_method": charge.calculation_method,
            "calculation_base": charge.calculation_base,
            "applied_rate": charge.applied_rate,
            "amount": charge.amount,
            "currency_code": charge.currency_code,
            "rule_snapshot": json.loads(charge.rule_snapshot_json),
        }
        for charge in item.charges
    ]
    snapshot = json.loads(item.result_snapshot_json) if item.result_snapshot_json else {}
    snapshot_charges = ((snapshot.get("charges") or {}).get("charges")) or []
    for row, snapshot_charge in zip(rows, snapshot_charges):
        party = (snapshot_charge or {}).get("party")
        if party:
            row["party"] = party
    return rows
