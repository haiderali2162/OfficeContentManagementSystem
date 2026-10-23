
"""
Land Area Calculation Engine
Version: 2.0.0

Supported conventions:
- Kanal-Marla-Square Feet (K-M-F)
- Kanal-Marla-Sarsahi (K-M-S)
- Acres using a configurable kanal count
- Kila using a configurable K-M-F definition
- Karam-based rectangular area calculations

The internal area is stored as Decimal square feet to avoid
losing precision when converting sarsahi and square karam.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable


class AreaError(ValueError):
    """Raised when a land area or calculation input is invalid."""


def _decimal(value, label="Value") -> Decimal:
    """Convert a number safely to Decimal."""
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise AreaError(f"{label} must be a valid number.") from None

    if not result.is_finite():
        raise AreaError(f"{label} must be a finite number.")

    return result


def _whole_number(value, label="Value") -> int:
    """Validate that a value is a non-negative whole number."""
    if isinstance(value, bool):
        raise AreaError(f"{label} must be a whole number.")

    try:
        number = int(value)
    except (ValueError, TypeError, OverflowError):
        raise AreaError(f"{label} must be a whole number.") from None

    if str(value).strip() not in (str(number), f"+{number}"):
        raise AreaError(f"{label} must be a whole number.")

    if number < 0:
        raise AreaError(f"{label} cannot be negative.")

    return number


def _round_whole(value: Decimal) -> int:
    """Round to the nearest whole unit, with halves rounded upward."""
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class AreaSettings:
    """Regional measurement conventions."""

    marla_per_kanal: int = 20
    square_feet_per_marla: int = 272
    sarsahi_per_marla: int = 9

    # Acre convention: one acre is treated as 8 kanal by default.
    kanals_per_acre: int = 8

    # Regional kila convention: 8 kanal, 18 marla, 0 square feet.
    kila_kanal: int = 8
    kila_marla: int = 18
    kila_feet: int = 0

    # One karam is a linear measurement.
    feet_per_karam: Decimal = Decimal("5.5")

    def __post_init__(self):
        for name in (
            "marla_per_kanal",
            "square_feet_per_marla",
            "sarsahi_per_marla",
            "kanals_per_acre",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise AreaError(f"{name} must be a positive whole number.")

        for name in ("kila_kanal", "kila_marla", "kila_feet"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise AreaError(f"{name} must be a non-negative whole number.")

        karam_feet = _decimal(self.feet_per_karam, "Feet per karam")
        if karam_feet <= 0:
            raise AreaError("Feet per karam must be greater than zero.")

        object.__setattr__(self, "feet_per_karam", karam_feet)


@dataclass(frozen=True)
class LandArea:
    """Area stored internally as Decimal square feet."""

    square_feet: Decimal | int

    def __post_init__(self):
        if isinstance(self.square_feet, bool):
            raise AreaError("Area must be a valid number.")

        value = _decimal(self.square_feet, "Area")

        if value < 0:
            raise AreaError("Land area cannot be negative.")

        object.__setattr__(self, "square_feet", value)

    @classmethod
    def from_kmf(
        cls,
        kanal: int,
        marla: int,
        feet: int,
        settings: AreaSettings | None = None,
    ) -> "LandArea":
        """Create an area from Kanal-Marla-Square Feet."""

        settings = settings or AreaSettings()

        kanal = _whole_number(kanal, "Kanal")
        marla = _whole_number(marla, "Marla")
        feet = _whole_number(feet, "Square feet")

        total = (
            kanal * settings.marla_per_kanal
            * settings.square_feet_per_marla
            + marla * settings.square_feet_per_marla
            + feet
        )

        return cls(Decimal(total))

    @classmethod
    def parse(
        cls,
        value: str,
        settings: AreaSettings | None = None,
    ) -> "LandArea":
        """Parse a string such as '10-10-0' as Kanal-Marla-Square Feet."""

        if not isinstance(value, str) or not value.strip():
            raise AreaError("Enter an area in Kanal-Marla-Square Feet format.")

        parts = value.strip().split("-")

        if len(parts) != 3:
            raise AreaError("Use Kanal-Marla-Square Feet, for example 10-10-0.")

        try:
            numbers = [int(part.strip()) for part in parts]
        except ValueError:
            raise AreaError("All three components must be whole numbers.") from None

        return cls.from_kmf(*numbers, settings=settings)

    @classmethod
    def from_kms(
        cls,
        kanal: int,
        marla: int,
        sarsahi: int,
        settings: AreaSettings | None = None,
    ) -> "LandArea":
        """Create an area from Kanal-Marla-Sarsahi."""

        settings = settings or AreaSettings()

        kanal = _whole_number(kanal, "Kanal")
        marla = _whole_number(marla, "Marla")
        sarsahi = _whole_number(sarsahi, "Sarsahi")

        total_sarsahi = (
            kanal * settings.marla_per_kanal * settings.sarsahi_per_marla
            + marla * settings.sarsahi_per_marla
            + sarsahi
        )

        square_feet_per_sarsahi = (
            Decimal(settings.square_feet_per_marla)
            / Decimal(settings.sarsahi_per_marla)
        )

        return cls(Decimal(total_sarsahi) * square_feet_per_sarsahi)

    @classmethod
    def from_acres(
        cls,
        acres: int | float | str | Decimal,
        settings: AreaSettings | None = None,
    ) -> "LandArea":
        """Create an area using the configured kanal-per-acre convention."""

        settings = settings or AreaSettings()
        acres_value = _decimal(acres, "Acres")

        if acres_value < 0:
            raise AreaError("Acres cannot be negative.")

        square_feet_per_acre = (
            settings.kanals_per_acre
            * settings.marla_per_kanal
            * settings.square_feet_per_marla
        )

        return cls(acres_value * Decimal(square_feet_per_acre))

    @classmethod
    def from_kila(
        cls,
        kila: int | float | str | Decimal,
        settings: AreaSettings | None = None,
    ) -> "LandArea":
        """Create an area using the configured local Kila definition."""

        settings = settings or AreaSettings()
        kila_value = _decimal(kila, "Kila")

        if kila_value < 0:
            raise AreaError("Kila cannot be negative.")

        square_feet_per_kila = (
            settings.kila_kanal
            * settings.marla_per_kanal
            * settings.square_feet_per_marla
            + settings.kila_marla * settings.square_feet_per_marla
            + settings.kila_feet
        )

        return cls(kila_value * Decimal(square_feet_per_kila))

    @classmethod
    def from_square_karam(
        cls,
        square_karam: int | float | str | Decimal,
        settings: AreaSettings | None = None,
    ) -> "LandArea":
        """Convert square karam to square feet."""

        settings = settings or AreaSettings()
        value = _decimal(square_karam, "Square karam")

        if value < 0:
            raise AreaError("Square karam cannot be negative.")

        square_feet_per_square_karam = (
            settings.feet_per_karam * settings.feet_per_karam
        )

        return cls(value * square_feet_per_square_karam)

    @classmethod
    def from_karam_dimensions(
        cls,
        length_karam: int | float | str | Decimal,
        width_karam: int | float | str | Decimal,
        settings: AreaSettings | None = None,
    ) -> "LandArea":
        """Calculate rectangular area from length and width in karam."""

        settings = settings or AreaSettings()
        length = _decimal(length_karam, "Length in karam")
        width = _decimal(width_karam, "Width in karam")

        if length < 0 or width < 0:
            raise AreaError("Dimensions cannot be negative.")

        length_feet = length * settings.feet_per_karam
        width_feet = width * settings.feet_per_karam

        return cls(length_feet * width_feet)

    def to_kmf(
        self,
        settings: AreaSettings | None = None,
    ) -> tuple[int, int, int]:
        """Return normalized Kanal-Marla-Square Feet, rounded to a whole sq ft."""

        settings = settings or AreaSettings()
        total_feet = _round_whole(self.square_feet)

        square_feet_per_kanal = (
            settings.marla_per_kanal * settings.square_feet_per_marla
        )

        kanal, remaining = divmod(total_feet, square_feet_per_kanal)
        marla, feet = divmod(remaining, settings.square_feet_per_marla)

        return kanal, marla, feet

    def to_kms(
        self,
        settings: AreaSettings | None = None,
    ) -> tuple[int, int, int]:
        """Return normalized Kanal-Marla-Sarsahi, rounded to nearest sarsahi."""

        settings = settings or AreaSettings()
        square_feet_per_sarsahi = (
            Decimal(settings.square_feet_per_marla)
            / Decimal(settings.sarsahi_per_marla)
        )

        total_sarsahi = _round_whole(
            self.square_feet / square_feet_per_sarsahi
        )

        sarsahi_per_kanal = (
            settings.marla_per_kanal * settings.sarsahi_per_marla
        )

        kanal, remaining = divmod(total_sarsahi, sarsahi_per_kanal)
        marla, sarsahi = divmod(remaining, settings.sarsahi_per_marla)

        return kanal, marla, sarsahi

    def format(
        self,
        settings: AreaSettings | None = None,
    ) -> str:
        kanal, marla, feet = self.to_kmf(settings)
        return f"{kanal}-{marla}-{feet}"

    def format_sarsahi(
        self,
        settings: AreaSettings | None = None,
    ) -> str:
        kanal, marla, sarsahi = self.to_kms(settings)
        return f"{kanal}-{marla}-{sarsahi}"

    def to_square_feet(self) -> int | Decimal:
        """Return square feet as an int when whole, otherwise Decimal."""

        if self.square_feet == self.square_feet.to_integral_value():
            return int(self.square_feet)
        return self.square_feet

    def to_acres(
        self,
        settings: AreaSettings | None = None,
    ) -> Decimal:
        settings = settings or AreaSettings()

        square_feet_per_acre = (
            settings.kanals_per_acre
            * settings.marla_per_kanal
            * settings.square_feet_per_marla
        )

        return self.square_feet / Decimal(square_feet_per_acre)

    def to_kila(
        self,
        settings: AreaSettings | None = None,
    ) -> Decimal:
        settings = settings or AreaSettings()

        square_feet_per_kila = (
            settings.kila_kanal
            * settings.marla_per_kanal
            * settings.square_feet_per_marla
            + settings.kila_marla * settings.square_feet_per_marla
            + settings.kila_feet
        )

        return self.square_feet / Decimal(square_feet_per_kila)

    def to_square_karam(
        self,
        settings: AreaSettings | None = None,
    ) -> Decimal:
        settings = settings or AreaSettings()
        square_feet_per_square_karam = (
            settings.feet_per_karam * settings.feet_per_karam
        )

        return self.square_feet / square_feet_per_square_karam


def sum_areas(areas: Iterable[LandArea]) -> LandArea:
    """Add multiple areas."""

    total = Decimal("0")

    for area in areas:
        if not isinstance(area, LandArea):
            raise AreaError("All items must be LandArea objects.")
        total += area.square_feet

    return LandArea(total)


def subtract_areas(first: LandArea, second: LandArea) -> LandArea:
    """Subtract the second area from the first."""

    result = first.square_feet - second.square_feet

    if result < 0:
        raise AreaError("The result cannot be negative.")

    return LandArea(result)


def average_area(areas: Iterable[LandArea]) -> LandArea:
    """Calculate the average, rounded to the nearest whole square foot."""

    area_list = list(areas)

    if not area_list:
        raise AreaError("At least one area is required to calculate an average.")

    if any(not isinstance(area, LandArea) for area in area_list):
        raise AreaError("All items must be LandArea objects.")

    average = sum(
        (area.square_feet for area in area_list),
        Decimal("0"),
    ) / Decimal(len(area_list))

    return LandArea(Decimal(_round_whole(average)))


def calculate_share(
    total_area: LandArea,
    numerator: int,
    denominator: int,
) -> LandArea:
    """Calculate a fractional ownership share."""

    numerator = _whole_number(numerator, "Share numerator")
    denominator = _whole_number(denominator, "Share denominator")

    if denominator == 0:
        raise AreaError("The share denominator must be greater than zero.")

    if numerator > denominator:
        raise AreaError("The share cannot exceed the total area.")

    result = (
        total_area.square_feet
        * Decimal(numerator)
        / Decimal(denominator)
    )

    return LandArea(Decimal(_round_whole(result)))


def calculate_percentage_share(
    total_area: LandArea,
    percentage: int | float | str | Decimal,
) -> LandArea:
    """Calculate a percentage of an area."""

    percentage_value = _decimal(percentage, "Percentage")

    if percentage_value < 0 or percentage_value > 100:
        raise AreaError("Percentage must be between 0 and 100.")

    result = (
        total_area.square_feet
        * percentage_value
        / Decimal("100")
    )

    return LandArea(Decimal(_round_whole(result)))


def make_area_settings(
    square_feet_per_marla: int = 272,
    *,
    kanals_per_acre: int = 8,
    kila_kanal: int = 8,
    kila_marla: int = 18,
    kila_feet: int = 0,
) -> AreaSettings:
    """Build settings for the selected local measurement convention."""

    return AreaSettings(
        square_feet_per_marla=square_feet_per_marla,
        kanals_per_acre=kanals_per_acre,
        kila_kanal=kila_kanal,
        kila_marla=kila_marla,
        kila_feet=kila_feet,
    )
