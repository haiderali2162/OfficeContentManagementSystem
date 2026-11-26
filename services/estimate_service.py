"""Persist and reopen valuation estimates with immutable calculation snapshots.

Version history:

* v0.2.26/v0.2.27 — the task number existed only inside
  ``input_snapshot_json``; there was no waseeka name, registry-area column,
  naming policy, or strict save check.
* v0.2.28 — resolves a durable estimate identity (task number, rendered name,
  waseeka, registry area) through validated naming settings and stores the
  policy snapshot with each save. New filters support today/old/selected
  report batches without changing the existing calculation snapshots.
* v0.2.29 — removed the unexpected mandatory client link reported by the
  developer. Strict mode now enforces the user-selected identity fields
  (default: task number and waseeka name); the client record is optional unless
  explicitly selected in Settings.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal
import json
import re
from typing import Any

from sqlalchemy import select, func
# from sqlalchemy import func
from sqlalchemy.orm import joinedload

from core.database import SessionLocal
from services.settings_service import format_estimate_template, get_estimate_naming
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


_IDENTITY_UNSAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def _clean_identity(value: Any, limit: int) -> str:
    """Remove path/control characters from a user-supplied identity value."""
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return _IDENTITY_UNSAFE_RE.sub("-", text)[:limit].strip(" -_")


def _generated_task_no(now: datetime) -> str:
    """Create a readable, collision-resistant task number for a blank field.

    v0.2.26 silently saved blank task numbers, which made history/export files
    indistinguishable. The generated value uses local date/time plus a
    millisecond suffix; it is never treated as an official government number.
    """
    return "T" + now.strftime("%Y%m%d%H%M%S") + f"{now.microsecond // 1000:03d}"


def _resolve_estimate_identity(
    inputs: dict[str, Any], naming: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Apply the saved naming policy and return immutable identity fields.

    v0.2.28: strict mode required a task number. v0.2.29: strict mode enforces
    the selected identity fields in :func:`_validate_required_identity`; a
    blank task number is still generated automatically when that setting is
    enabled. The caller may pass the already-loaded policy so validation and
    the stored snapshot cannot disagree.
    """
    naming = naming or get_estimate_naming()
    now = datetime.now()
    task_no = _clean_identity(inputs.get("task_no"), 120)
    task_source = "entered" if task_no else "none"
    if not task_no and naming["auto_task_no"]:
        task_no = _generated_task_no(now)
        task_source = "generated"
    values = {
        "task_no": task_no,
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H-%M-%S"),
        "datetime": now.strftime("%Y-%m-%d_%H-%M-%S"),
        "custom": naming["custom_text"],
        "waseeka": _clean_identity(inputs.get("waseeka_name"), 200),
        "registry_area": _clean_identity(inputs.get("registry_area"), 120),
    }
    estimate_name = format_estimate_template(naming["template"], values)
    naming_snapshot = {
        **naming,
        "task_source": task_source,
        "generated_at": now.isoformat(timespec="seconds"),
    }
    return {
        "task_no": task_no or None,
        "estimate_name": estimate_name,
        "waseeka_name": values["waseeka"] or None,
        "registry_area": values["registry_area"] or None,
        "task_source": task_source,
        "naming_snapshot": naming_snapshot,
    }


def _validate_required_identity(
    naming: dict[str, Any],
    identity: dict[str, Any],
    client_id: int | None,
) -> None:
    """Validate the v0.2.29 configurable identity requirements.

    Each message names the exact calculator box and tab. This fixes the
    v0.2.28 report where saving failed with a generic "select a client"
    message although the Client box was hidden in the collapsed advanced
    section. The client link is optional unless explicitly required.
    """
    if not naming["strict_mode"]:
        return
    problems: list[str] = []
    for field in naming["required_identity_fields"]:
        if field == "task_no" and not identity["task_no"]:
            problems.append(
                "Task number — enter one in the 'Task no.' box at the top of "
                "the Calculate tab, or enable automatic task numbers in Settings."
            )
        elif field == "waseeka_name" and not identity["waseeka_name"]:
            problems.append(
                "Waseeka name — enter one in the 'Waseeka name' box beside "
                "Task no. on the Calculate tab, or turn off this requirement in Settings."
            )
        elif field == "registry_area" and not identity["registry_area"]:
            problems.append(
                "Registry area — enter one in the 'Registry area' box beside "
                "Waseeka name on the Calculate tab, or turn off this requirement in Settings."
            )
        elif field == "client" and client_id is None:
            problems.append(
                "Client record — choose a client in the 'Client' box beside "
                "Task no. on the Calculate tab, or turn off this requirement in Settings."
            )
    if problems:
        raise EstimateServiceError(
            "Strict reporting requires missing estimate identity: "
            + "; ".join(problems)
        )


def _as_filter_date(value: Any, label: str) -> date | None:
    """Accept a ``date``, ``datetime``, or ISO date string for report filters."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        raise EstimateServiceError(f"{label} must use YYYY-MM-DD format.") from None


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
    # v0.2.29: a client link is optional unless selected in Settings. The
    # previous unconditional check (v0.2.28 and earlier) blocked valid saves
    # because the Client box was hidden inside the advanced panel.
    naming = get_estimate_naming()
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
        # v0.2.28: registry area is already known on the location profile; use
        # it when the form field was left blank, then freeze it with the save.
        identity_inputs = {
            **inputs,
            "registry_area": inputs.get("registry_area") or location.property_area,
        }
        identity = _resolve_estimate_identity(identity_inputs, naming)
        _validate_required_identity(naming, identity, client_id)
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
            task_no=identity["task_no"],
            estimate_name=identity["estimate_name"],
            waseeka_name=identity["waseeka_name"],
            registry_area=identity["registry_area"],
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
            input_snapshot_json=_snapshot_json({
                **inputs,
                "mauza": mauza_name,
                "task_no": identity["task_no"],
                "estimate_name": identity["estimate_name"],
                "waseeka_name": identity["waseeka_name"],
                "registry_area": identity["registry_area"],
                "task_no_source": identity["task_source"],
            }),
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
            naming_snapshot_json=_snapshot_json(identity["naming_snapshot"]),
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


def list_estimates(
    *,
    limit: int = 100,
    since: Any = None,
    until: Any = None,
    ids: list[Any] | None = None,
    task_no_contains: str | None = None,
    waseeka_name_contains: str | None = None,
) -> list[dict[str, Any]]:
    """Return saved estimates for history and report-batch selection.

    Filters are optional and additive; callers that pass only ``limit`` get
    the same recent-history behaviour as v0.2.27. ``since``/``until`` select
    by the *save* date (``created_at``), which is what "today's estimates"
    means. Text filters are case-insensitive where the backend supports ILIKE.
    """
    safe_limit = max(1, min(int(limit), 1000))
    statement = (
        select(ValuationEstimate)
        .options(joinedload(ValuationEstimate.client))
    )
    since_date = _as_filter_date(since, "Since date")
    until_date = _as_filter_date(until, "Until date")
    if since_date and until_date and since_date > until_date:
        raise EstimateServiceError("Since date must not be later than until date.")
    # ``created_at`` is stored in UTC while the user asks for "today" in local
    # time. Convert the local calendar-day boundaries to UTC before comparing.
    if since_date:
        local_start = datetime.combine(since_date, time.min).astimezone()
        statement = statement.where(
            ValuationEstimate.created_at >= local_start.astimezone(timezone.utc)
        )
    if until_date:
        local_end = datetime.combine(until_date, time.max).astimezone()
        statement = statement.where(
            ValuationEstimate.created_at <= local_end.astimezone(timezone.utc)
        )
    if ids is not None:
        try:
            normalized_ids = [int(value) for value in ids]
        except (TypeError, ValueError):
            raise EstimateServiceError("Estimate selection contains an invalid ID.") from None
        if not normalized_ids:
            return []
        statement = statement.where(ValuationEstimate.id.in_(normalized_ids))
    if task_no_contains and str(task_no_contains).strip():
        statement = statement.where(
            ValuationEstimate.task_no.ilike(f"%{str(task_no_contains).strip()}%")
        )
    if waseeka_name_contains and str(waseeka_name_contains).strip():
        statement = statement.where(
            ValuationEstimate.waseeka_name.ilike(f"%{str(waseeka_name_contains).strip()}%")
        )
    statement = (
        statement
        .order_by(ValuationEstimate.created_at.desc(), ValuationEstimate.id.desc())
        .limit(safe_limit)
    )
    with SessionLocal() as session:
        return [
            {
                "id": item.id,
                "transaction_date": item.transaction_date,
                "created_at": item.created_at,
                "task_no": item.task_no,
                "estimate_name": item.estimate_name,
                "waseeka_name": item.waseeka_name,
                "registry_area": item.registry_area,
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
            "created_at": item.created_at,
            "updated_at": item.updated_at,
            "task_no": item.task_no,
            "estimate_name": item.estimate_name,
            "waseeka_name": item.waseeka_name,
            "registry_area": item.registry_area,
            "naming_snapshot": (
                json.loads(item.naming_snapshot_json)
                if item.naming_snapshot_json else None
            ),
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
