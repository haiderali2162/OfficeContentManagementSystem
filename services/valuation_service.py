"""Property valuation calculations independent of the Flet interface.

The service resolves a single applicable schedule row, converts an entered
area into that row's explicit rate unit, and returns a calculation trace.
It reports source/verification problems instead of treating a configured
rate as an official legal valuation.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable

from core.area_engine import AreaError, AreaSettings, LandArea
from core.models import DC_RATE_UNITS
from services.dc_rate_service import list_dc_rates


class ValuationError(ValueError):
    """Raised when valuation inputs are invalid or no unique rate applies."""


CENT = Decimal("0.01")
SQUARE_FEET_PER_SQUARE_YARD = Decimal("9")
SQUARE_FEET_PER_SQUARE_METER = Decimal("10.763910416709722")
SUPPORTED_AREA_UNITS = {
    "kmf", "square_foot", "square_yard", "square_meter",
    "marla", "kanal", "acre", "kila",
}


def _decimal(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError, AttributeError):
        raise ValuationError(f"{label} must be a valid number.") from None
    if not result.is_finite():
        raise ValuationError(f"{label} must be finite.")
    return result


def _money(value: Decimal) -> Decimal:
    """Round returned currency values to two places, half-up."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def area_to_square_feet(
    area_value: Any,
    area_unit: str,
    *,
    settings: AreaSettings | None = None,
) -> Decimal:
    """Convert a positive area input to square feet using shared area rules."""
    settings = settings or AreaSettings()
    unit = str(area_unit or "").strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "k_m_f": "kmf",
        "sqft": "square_foot",
        "sq_ft": "square_foot",
        "square_feet": "square_foot",
        "sqyd": "square_yard",
        "sq_yd": "square_yard",
        "square_yards": "square_yard",
        "sqm": "square_meter",
        "sq_m": "square_meter",
        "square_meters": "square_meter",
        "kanals": "kanal",
        "marla": "marla",
        "acres": "acre",
        "kilas": "kila",
    }
    unit = aliases.get(unit, unit)
    if unit not in SUPPORTED_AREA_UNITS:
        raise ValuationError(f"Unsupported property area unit: {area_unit}.")
    if unit == "kmf":
        if not isinstance(area_value, str):
            raise ValuationError("K-M-F area must be entered as text, for example 2-5-0.")
        try:
            parsed = LandArea.parse(area_value, settings)
        except AreaError as exc:
            raise ValuationError(str(exc)) from None
        if parsed.square_feet <= 0:
            raise ValuationError("Property area must be greater than zero.")
        return parsed.square_feet

    value = _decimal(area_value, "Property area")
    if value <= 0:
        raise ValuationError("Property area must be greater than zero.")

    try:
        if unit == "marla":
            return value * Decimal(settings.square_feet_per_marla)
        if unit == "kanal":
            return value * Decimal(settings.marla_per_kanal * settings.square_feet_per_marla)
        if unit == "acre":
            return LandArea.from_acres(value, settings).square_feet
        if unit == "kila":
            return LandArea.from_kila(value, settings).square_feet
    except (AreaError, ValueError, OverflowError) as exc:
        raise ValuationError(str(exc)) from None

    factor = {
        "square_foot": Decimal(1),
        "square_yard": SQUARE_FEET_PER_SQUARE_YARD,
        "square_meter": SQUARE_FEET_PER_SQUARE_METER,
    }[unit]
    return value * factor


def _area_quantity(
    square_feet: Decimal,
    rate_unit: str,
    settings: AreaSettings,
    plot_count: Any | None = None,
) -> Decimal:
    if rate_unit not in DC_RATE_UNITS:
        raise ValuationError(f"Unsupported stored rate unit: {rate_unit}.")
    unit = rate_unit.removeprefix("per_")
    factors = {
        "square_foot": Decimal(1),
        "square_yard": SQUARE_FEET_PER_SQUARE_YARD,
        "square_meter": SQUARE_FEET_PER_SQUARE_METER,
        "marla": Decimal(settings.square_feet_per_marla),
        "kanal": Decimal(settings.marla_per_kanal * settings.square_feet_per_marla),
        "acre": Decimal(settings.kanals_per_acre * settings.marla_per_kanal * settings.square_feet_per_marla),
        "kila": Decimal(settings.kila_kanal * settings.marla_per_kanal * settings.square_feet_per_marla
                        + settings.kila_marla * settings.square_feet_per_marla + settings.kila_feet),
    }
    if unit == "plot":
        count = _decimal(plot_count, "Plot count") if plot_count is not None else Decimal(0)
        if count <= 0:
            raise ValuationError("A per-plot rate needs an explicit positive plot count.")
        return count
    return square_feet / factors[unit]


def resolve_valuation_rate(
    *,
    location_id: int,
    property_type: str,
    schedule_type: str,
    transaction_date: date,
    classification: str | None = None,
    floor: str | None = None,
    rate_unit: str | None = None,
    rates: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Find exactly one date-effective rate matching the requested dimensions.

    `rates` is an injectable list for deterministic tests; application calls
    fetch the saved records for the selected location and schedule.
    """
    if not isinstance(transaction_date, date):
        raise ValuationError("Transaction date must be a date.")
    if not property_type or not str(property_type).strip():
        raise ValuationError("Property type is required to find a schedule rate.")
    schedule = str(schedule_type or "").strip().lower()
    if schedule not in {"dc", "fbr"}:
        raise ValuationError("Schedule type must be DC or FBR.")
    if rates is None:
        rates = list_dc_rates(location_id=location_id, schedule_type=schedule)

    def same(left: Any, right: Any) -> bool:
        return (str(left or "").strip().casefold() == str(right or "").strip().casefold())

    dimension_matches = [row for row in rates
        if row.get("location_id") == location_id
        and same(row.get("schedule_type", "dc"), schedule)
        and same(row.get("property_type"), property_type)
        and same(row.get("classification"), classification)
        and same(row.get("floor"), floor)
        and (rate_unit is None or same(row.get("rate_unit"), rate_unit))]
    active = [row for row in dimension_matches
        if row.get("effective_from") and row["effective_from"] <= transaction_date
        and (row.get("effective_to") is None or row["effective_to"] >= transaction_date)]
    if len(active) > 1:
        raise ValuationError("Several valuation rates match these details and date; resolve the conflict before calculating.")
    if not active:
        if dimension_matches:
            raise ValuationError("A matching rate exists, but it is not effective on the transaction date.")
        raise ValuationError("No valuation rate matches the selected location, schedule, classification, floor, and unit.")
    return active[0]


def calculate_property_valuation(
    *,
    area_value: Any,
    area_unit: str,
    rate: dict[str, Any],
    share_numerator: int = 1,
    share_denominator: int = 1,
    declared_consideration: Any | None = None,
    plot_count: Any | None = None,
    settings: AreaSettings | None = None,
) -> dict[str, Any]:
    """Calculate full and transferred value and return a human-readable trace."""
    settings = settings or AreaSettings()
    try:
        numerator, denominator = int(share_numerator), int(share_denominator)
    except (ValueError, TypeError, OverflowError):
        raise ValuationError("Share must use whole-number numerator and denominator.") from None
    if numerator <= 0 or denominator <= 0 or numerator > denominator:
        raise ValuationError("Share must be greater than zero and cannot exceed the whole property.")
    if isinstance(share_numerator, bool) or isinstance(share_denominator, bool):
        raise ValuationError("Share numerator and denominator must be whole numbers.")
    if str(share_numerator).strip() != str(numerator) or str(share_denominator).strip() != str(denominator):
        raise ValuationError("Share numerator and denominator must be whole numbers.")

    rate_unit = str(rate.get("rate_unit") or "")
    if rate_unit not in DC_RATE_UNITS:
        raise ValuationError("The selected schedule rate has an unsupported unit.")
    rate_value = _decimal(rate.get("rate_value"), "Schedule rate")
    if rate_value < 0:
        raise ValuationError("Schedule rate cannot be negative.")
    square_feet = area_to_square_feet(area_value, area_unit, settings=settings)
    quantity = _area_quantity(square_feet, rate_unit, settings, plot_count)
    full_exact = quantity * rate_value
    share_exact = full_exact * Decimal(numerator) / Decimal(denominator)
    full_value, transferred_value = _money(full_exact), _money(share_exact)

    warnings: list[str] = []
    if rate.get("verification_status", "unverified") != "verified":
        warnings.append("The selected valuation rate is unverified; this result is an estimate.")
    if rate.get("published_year") is None:
        warnings.append("The schedule publication year is missing and should be checked manually.")
    if not rate.get("source_reference"):
        warnings.append("The schedule source/reference is not recorded.")

    declared = None
    comparison = None
    if declared_consideration not in (None, ""):
        declared = _decimal(declared_consideration, "Declared consideration")
        if declared < 0:
            raise ValuationError("Declared consideration cannot be negative.")
        declared = _money(declared)
        difference = _money(declared - transferred_value)
        comparison = {
            "declared_consideration": declared,
            "transferred_valuation": transferred_value,
            "difference_declared_minus_valuation": difference,
            "declared_is_below_valuation": declared < transferred_value,
        }

    explanation = [
        f"Area: {area_value} {area_unit} = {square_feet} square feet.",
        f"Rate basis: {rate_unit}; converted quantity = {quantity}.",
        f"Full property value: {quantity} × {rate_value} = {full_value}.",
        f"Transferred share: {numerator}/{denominator} × {full_value} = {transferred_value}.",
    ]
    if comparison:
        if comparison["difference_declared_minus_valuation"] == 0:
            explanation.append("Declared consideration equals the transferred valuation.")
        elif comparison["declared_is_below_valuation"]:
            explanation.append(
                f"Declared consideration is {abs(comparison['difference_declared_minus_valuation'])} "
                "below the transferred valuation."
            )
        else:
            explanation.append(
                f"Declared consideration is {comparison['difference_declared_minus_valuation']} "
                "above the transferred valuation."
            )
    return {
        "rate_id": rate.get("id"),
        "schedule_type": rate.get("schedule_type", "dc"),
        "rate_value": rate_value,
        "rate_unit": rate_unit,
        "area_square_feet": square_feet,
        "rate_quantity": quantity,
        "property_value": full_value,
        "share_numerator": numerator,
        "share_denominator": denominator,
        "transferred_value": transferred_value,
        "declared_consideration": declared,
        "comparison": comparison,
        "warnings": warnings,
        "explanation": explanation,
    }


def calculate_valuation(
    *,
    location_id: int,
    property_type: str,
    schedule_type: str,
    transaction_date: date,
    area_value: Any,
    area_unit: str,
    classification: str | None = None,
    floor: str | None = None,
    rate_unit: str | None = None,
    share_numerator: int = 1,
    share_denominator: int = 1,
    declared_consideration: Any | None = None,
    plot_count: Any | None = None,
    settings: AreaSettings | None = None,
) -> dict[str, Any]:
    """Resolve a saved rate and calculate in one call for future UI callers."""
    rate = resolve_valuation_rate(
        location_id=location_id,
        property_type=property_type,
        schedule_type=schedule_type,
        transaction_date=transaction_date,
        classification=classification,
        floor=floor,
        rate_unit=rate_unit,
    )
    result = calculate_property_valuation(
        area_value=area_value,
        area_unit=area_unit,
        rate=rate,
        share_numerator=share_numerator,
        share_denominator=share_denominator,
        declared_consideration=declared_consideration,
        plot_count=plot_count,
        settings=settings,
    )
    result["rate"] = rate
    return result


def calculate_dual_valuation(
    *,
    location_id: int,
    property_type: str,
    transaction_date: date,
    area_value: Any,
    area_unit: str,
    classification: str | None = None,
    floor: str | None = None,
    dc_rate_unit: str | None = None,
    fbr_rate_unit: str | None = None,
    dc_rate_override: dict[str, Any] | None = None,
    fbr_rate_override: dict[str, Any] | None = None,
    fbr_location_id: int | None = None,
    registry_written_value: Any | None = None,
    settings: AreaSettings | None = None,
) -> dict[str, Any]:
    """Calculate DC value as the primary base and FBR value independently.

    DC must resolve uniquely. FBR is optional for a Mauza: missing or ambiguous
    FBR rows are reported and do not silently replace the DC valuation.
    """
    def validate_override(rate: dict[str, Any] | None, schedule: str) -> dict[str, Any] | None:
        if rate is None:
            return None
        if str(rate.get("schedule_type") or "dc").casefold() != schedule:
            raise ValuationError(f"The selected {schedule.upper()} rate belongs to another schedule.")
        if rate.get("effective_from") and rate["effective_from"] > transaction_date:
            raise ValuationError(f"The selected {schedule.upper()} rate is not effective yet.")
        if rate.get("effective_to") and rate["effective_to"] < transaction_date:
            raise ValuationError(f"The selected {schedule.upper()} rate has expired.")
        return rate

    dc_rate = validate_override(dc_rate_override, "dc") or resolve_valuation_rate(
        location_id=location_id,
        property_type=property_type,
        schedule_type="dc",
        transaction_date=transaction_date,
        classification=classification,
        floor=floor,
        rate_unit=dc_rate_unit,
    )
    dc_value = calculate_property_valuation(
        area_value=area_value,
        area_unit=area_unit,
        rate=dc_rate,
        share_numerator=1,
        share_denominator=1,
        declared_consideration=registry_written_value,
        # A registered property is one property/plot unless the schedule is
        # area-based. The old plot-count input is no longer required.
        plot_count=1 if dc_rate.get("rate_unit") == "per_plot" else None,
        settings=settings,
    )
    fbr_rate = None
    fbr_value = None
    warnings = list(dc_value["warnings"])
    fbr_warning: str | None = None
    try:
        fbr_rate = validate_override(fbr_rate_override, "fbr") or resolve_valuation_rate(
            location_id=fbr_location_id or location_id,
            property_type=property_type,
            schedule_type="fbr",
            transaction_date=transaction_date,
            classification=classification,
            floor=floor,
            rate_unit=fbr_rate_unit,
        )
        fbr_value = calculate_property_valuation(
            area_value=area_value,
            area_unit=area_unit,
            rate=fbr_rate,
            share_numerator=1,
            share_denominator=1,
            plot_count=1 if fbr_rate.get("rate_unit") == "per_plot" else None,
            settings=settings,
        )
        warnings.extend(f"FBR: {item}" for item in fbr_value["warnings"])
    except ValuationError as exc:
        fbr_warning = str(exc)
        warnings.append(f"FBR schedule unavailable: {fbr_warning}")

    dc_amount = dc_value["transferred_value"]
    fbr_amount = fbr_value["transferred_value"] if fbr_value else None
    explanation = [
        *[f"DC: {line}" for line in dc_value["explanation"]],
        f"FBR value: {fbr_amount if fbr_amount is not None else 'not available'}.",
        "Fee formulas and tax rules receive separate dc_value and fbr_value bases; choose the intended schedule explicitly in each rule.",
    ]
    return {
        **dc_value,
        "property_value": dc_amount,
        "transferred_value": dc_amount,
        "dc_value": dc_amount,
        "fbr_value": fbr_amount,
        "registry_written_value": dc_value["declared_consideration"],
        "dc_rate": dc_rate,
        "fbr_rate": fbr_rate,
        "fbr_valuation": fbr_value,
        "rate_snapshot": {"dc": dc_rate, "fbr": fbr_rate},
        "rate_id": dc_rate.get("id"),
        "rate": dc_rate,
        "fbr_warning": fbr_warning,
        "warnings": warnings,
        "explanation": explanation,
    }
