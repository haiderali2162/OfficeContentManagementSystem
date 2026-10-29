"""Property value calculations use explicit schedule units and Decimal math."""

from datetime import date
from decimal import Decimal

import pytest

from core.area_engine import AreaSettings
from services import valuation_service as valuation


def _rate(**overrides):
    result = {
        "id": 7,
        "location_id": 3,
        "schedule_type": "dc",
        "property_type": "Residential",
        "classification": "Urban",
        "floor": None,
        "rate_value": Decimal("800000"),
        "rate_unit": "per_acre",
        "effective_from": date(2025, 7, 1),
        "effective_to": None,
        "published_year": 2025,
        "source_reference": "Hypothetical test schedule",
        "verification_status": "verified",
    }
    result.update(overrides)
    return result


def test_resolves_unique_active_rate_for_location_dimensions():
    rate = _rate()
    selected = valuation.resolve_valuation_rate(
        location_id=3,
        property_type="residential",
        schedule_type="dc",
        transaction_date=date(2026, 1, 1),
        classification="urban",
        rates=[rate],
    )
    assert selected is rate


def test_rate_resolution_rejects_ambiguous_missing_and_expired_rates():
    query = dict(
        location_id=3,
        property_type="Residential",
        schedule_type="dc",
        transaction_date=date(2026, 1, 1),
        classification="Urban",
    )
    with pytest.raises(valuation.ValuationError, match="Several"):
        valuation.resolve_valuation_rate(rates=[_rate(), _rate(id=8)], **query)
    with pytest.raises(valuation.ValuationError, match="not effective"):
        valuation.resolve_valuation_rate(
            rates=[_rate(effective_to=date(2025, 12, 31))], **query
        )
    with pytest.raises(valuation.ValuationError, match="No valuation rate"):
        valuation.resolve_valuation_rate(rates=[], **query)


def test_area_conversion_uses_existing_area_conventions():
    settings = AreaSettings(square_feet_per_marla=225, kanals_per_acre=8)
    assert valuation.area_to_square_feet("2-4-0", "kmf", settings=settings) == Decimal(9900)
    assert valuation.area_to_square_feet("0.5", "acre", settings=settings) == Decimal(18000)
    assert valuation.area_to_square_feet("3.5", "marla", settings=settings) == Decimal("787.5")
    assert valuation.area_to_square_feet("10", "square_yard") == Decimal(90)


def test_full_value_fractional_share_and_declared_consideration_comparison():
    result = valuation.calculate_property_valuation(
        area_value="4-0-0",
        area_unit="kmf",
        rate=_rate(),
        share_numerator=1,
        share_denominator=5,
        declared_consideration="70000",
    )
    assert result["rate_quantity"] == Decimal("0.5")
    assert result["property_value"] == Decimal("400000.00")
    assert result["transferred_value"] == Decimal("80000.00")
    assert result["comparison"]["declared_is_below_valuation"] is True
    assert result["comparison"]["difference_declared_minus_valuation"] == Decimal("-10000.00")
    assert "Full property value" in result["explanation"][2]
    assert result["warnings"] == []


def test_unverified_missing_source_and_year_are_reported_as_warnings():
    result = valuation.calculate_property_valuation(
        area_value="1",
        area_unit="acre",
        rate=_rate(verification_status="unverified", source_reference=None, published_year=None),
    )
    assert len(result["warnings"]) == 3


def test_explicit_plot_count_is_required_for_per_plot_rate():
    rate = _rate(rate_unit="per_plot", rate_value=Decimal("250000"))
    with pytest.raises(valuation.ValuationError, match="plot count"):
        valuation.calculate_property_valuation(area_value="1", area_unit="acre", rate=rate)
    result = valuation.calculate_property_valuation(
        area_value="1", area_unit="acre", rate=rate, plot_count="2"
    )
    assert result["property_value"] == Decimal("500000.00")


@pytest.mark.parametrize("kwargs", [
    {"area_value": "0", "area_unit": "acre"},
    {"area_value": "1", "area_unit": "acre", "share_numerator": 2, "share_denominator": 1},
    {"area_value": "1", "area_unit": "rood"},
    {"area_value": "1", "area_unit": "acre", "declared_consideration": "-5"},
])
def test_invalid_area_share_unit_or_consideration_is_rejected(kwargs):
    with pytest.raises(valuation.ValuationError):
        valuation.calculate_property_valuation(rate=_rate(), **kwargs)


def test_currency_rounding_is_decimal_half_up():
    result = valuation.calculate_property_valuation(
        area_value="1", area_unit="square_foot",
        rate=_rate(rate_unit="per_square_foot", rate_value=Decimal("1.005")),
    )
    assert result["transferred_value"] == Decimal("1.01")


def test_dual_valuation_keeps_dc_and_fbr_values_separate(monkeypatch):
    rates = {
        "dc": _rate(schedule_type="dc", rate_value=Decimal("800000")),
        "fbr": _rate(schedule_type="fbr", rate_value=Decimal("1000000")),
    }

    def resolve(*, schedule_type, **_kwargs):
        return rates[schedule_type]

    monkeypatch.setattr(valuation, "resolve_valuation_rate", resolve)
    result = valuation.calculate_dual_valuation(
        location_id=3, property_type="Residential", classification="Urban",
        transaction_date=date(2026, 1, 1), area_value="1", area_unit="acre",
        registry_written_value="750000",
    )
    assert result["dc_value"] == Decimal("800000.00")
    assert result["fbr_value"] == Decimal("1000000.00")
    assert result["registry_written_value"] == Decimal("750000.00")
    assert result["rate_snapshot"]["dc"]["schedule_type"] == "dc"
    assert result["rate_snapshot"]["fbr"]["schedule_type"] == "fbr"


def test_dual_valuation_keeps_dc_when_fbr_schedule_is_missing(monkeypatch):
    def resolve(*, schedule_type, **_kwargs):
        if schedule_type == "fbr":
            raise valuation.ValuationError("No valuation rate matches FBR.")
        return _rate()

    monkeypatch.setattr(valuation, "resolve_valuation_rate", resolve)
    result = valuation.calculate_dual_valuation(
        location_id=3, property_type="Residential", classification="Urban",
        transaction_date=date(2026, 1, 1), area_value="1", area_unit="acre",
    )
    assert result["dc_value"] == Decimal("800000.00")
    assert result["fbr_value"] is None
    assert any("FBR schedule unavailable" in warning for warning in result["warnings"])
