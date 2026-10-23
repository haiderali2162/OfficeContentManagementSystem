import pytest
from decimal import Decimal

from core.area_engine import make_area_settings

from core.area_engine import (
    AreaError,
    AreaSettings,
    LandArea,
    sum_areas,
    subtract_areas,
    average_area,
    calculate_share,
    calculate_percentage_share,
)


def test_parse_and_format_area():
    area = LandArea.parse("10-10-0")

    assert area.to_square_feet() == 10 * 20 * 272 + 10 * 272
    assert area.format() == "10-10-0"


def test_add_areas():
    first = LandArea.parse("1-10-0")
    second = LandArea.parse("0-10-0")

    result = sum_areas([first, second])

    assert result.format() == "2-0-0"


def test_subtract_areas():
    first = LandArea.parse("2-0-0")
    second = LandArea.parse("0-10-0")

    result = subtract_areas(first, second)

    assert result.format() == "1-10-0"


def test_subtraction_cannot_be_negative():
    with pytest.raises(AreaError):
        subtract_areas(
            LandArea.parse("0-5-0"),
            LandArea.parse("0-10-0"),
        )


def test_custom_225_square_feet_per_marla():
    settings = AreaSettings(square_feet_per_marla=225)
    area = LandArea.parse("1-0-0", settings)

    assert area.to_square_feet() == 4500
    assert area.format(settings) == "1-0-0"


def test_average_area():
    result = average_area([
        LandArea.parse("1-0-0"),
        LandArea.parse("3-0-0"),
    ])

    assert result.format() == "2-0-0"


def test_one_quarter_share():
    total = LandArea.parse("4-0-0")
    result = calculate_share(total, 1, 4)

    assert result.format() == "1-0-0"


def test_percentage_share():
    total = LandArea.parse("2-0-0")
    result = calculate_percentage_share(total, 50)

    assert result.format() == "1-0-0"


def test_invalid_area_format():
    with pytest.raises(AreaError):
        LandArea.parse("10-10")


def test_negative_area_is_rejected():
    with pytest.raises(AreaError):
        LandArea.parse("1--5")



def test_sarsahi_conversion():
    area = LandArea.from_kms(0, 1, 0)

    assert area.to_square_feet() == 272
    assert area.format_sarsahi() == "0-1-0"


def test_nine_sarsahi_equal_one_marla():
    area = LandArea.from_kms(0, 0, 9)

    assert area.to_square_feet() == 272
    assert area.format() == "0-1-0"


def test_eight_kanal_acre_convention():
    settings = make_area_settings(272, kanals_per_acre=8)

    area = LandArea.from_acres(1, settings)

    assert area.format(settings) == "8-0-0"


def test_local_kila_convention():
    settings = make_area_settings(
        272,
        kila_kanal=8,
        kila_marla=18,
        kila_feet=0,
    )

    area = LandArea.from_kila(1, settings)

    assert area.format(settings) == "8-18-0"


def test_225_square_feet_marla_with_sarsahi():
    settings = AreaSettings(square_feet_per_marla=225)

    area = LandArea.from_kms(0, 1, 0, settings)

    assert area.to_square_feet() == 225
    assert area.format_sarsahi(settings) == "0-1-0"


def test_one_square_karam():
    area = LandArea.from_square_karam(1)

    assert area.square_feet == Decimal("30.25")


def test_karam_rectangular_dimensions():
    area = LandArea.from_karam_dimensions(2, 3)

    # 2 karam = 11 feet; 3 karam = 16.5 feet.
    assert area.square_feet == Decimal("181.5")


def test_decimal_acre_conversion():
    settings = AreaSettings()
    area = LandArea.from_kmf(4, 0, 0, settings)

    assert area.to_acres(settings) == Decimal("0.5")
