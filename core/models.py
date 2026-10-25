
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional
from sqlalchemy import Integer, String, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Date,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Client(Base):
    """Core information about an FBR client."""

    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, index=True
    )

    name: Mapped[str] = mapped_column(String(200), index=True)
    father_name: Mapped[Optional[str]] = mapped_column(String(200))
    cnic: Mapped[Optional[str]] = mapped_column(
        String(20), unique=True, index=True
    )
    ntn_number: Mapped[Optional[str]] = mapped_column(
        String(30), unique=True, index=True
    )
    cnic_expiry_date: Mapped[Optional[date]] = mapped_column(Date)
    date_of_birth: Mapped[Optional[date]] = mapped_column(Date)
    registration_date: Mapped[Optional[date]] = mapped_column(Date)
    status_updated_date: Mapped[Optional[date]] = mapped_column(Date)
    phone_number: Mapped[Optional[str]] = mapped_column(String(30))
    email: Mapped[Optional[str]] = mapped_column(String(254))
    address: Mapped[Optional[str]] = mapped_column(Text)
    tehsil: Mapped[Optional[str]] = mapped_column(String(120))
    district: Mapped[Optional[str]] = mapped_column(String(120))
    city: Mapped[Optional[str]] = mapped_column(String(120))
    province: Mapped[Optional[str]] = mapped_column(String(120))
    payment_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    amount_received: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))

    current_status: Mapped[Optional[str]] = mapped_column(String(100))
    referred_by: Mapped[Optional[str]] = mapped_column(String(200))

    # These columns are reserved for encrypted credentials.
    # Never save passwords or PINs as ordinary text.
    password_ciphertext: Mapped[Optional[bytes]] = mapped_column(
        LargeBinary
    )
    pin_ciphertext: Mapped[Optional[bytes]] = mapped_column(
        LargeBinary
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )

    custom_values: Mapped[list["ClientCustomFieldValue"]] = (
        relationship(
            back_populates="client",
            cascade="all, delete-orphan",
        )
    )

    assets: Mapped[list["ClientAsset"]] = relationship(
        back_populates="client",
        cascade="all, delete-orphan",
    )

    # Tax estimates are historical records; deleting a client clears only
    # their optional link while preserving each estimate's saved snapshots.
    valuation_estimates: Mapped[list["ValuationEstimate"]] = relationship(
        back_populates="client",
        passive_deletes=True,
    )


class CustomFieldDefinition(Base):
    """Defines a user-created field or table column."""

    __tablename__ = "custom_field_definitions"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True
    )

    # Examples: client, asset, valuation.
    entity_type: Mapped[str] = mapped_column(
        String(50), index=True
    )

    # Internal key, e.g. annual_income.
    key: Mapped[str] = mapped_column(String(100))

    # User-visible column heading.
    label: Mapped[str] = mapped_column(String(150))

    # Examples: text, integer, decimal, date, boolean, choice.
    data_type: Mapped[str] = mapped_column(String(30))

    is_required: Mapped[bool] = mapped_column(
        Boolean, default=False
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True
    )

    # Optional choices, validation rules, and display settings.
    settings_json: Mapped[Optional[str]] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "entity_type",
            "key",
            name="uq_custom_field_entity_key",
        ),
    )


class ClientCustomFieldValue(Base):
    """Stores values for client-defined fields."""

    __tablename__ = "client_custom_field_values"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True
    )

    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"),
        index=True,
    )

    field_id: Mapped[int] = mapped_column(
        ForeignKey(
            "custom_field_definitions.id",
            ondelete="RESTRICT",
        ),
        index=True,
    )

    # Values will be validated and converted using the
    # field definition's data_type by the service layer.
    value_text: Mapped[Optional[str]] = mapped_column(Text)

    client: Mapped["Client"] = relationship(
        back_populates="custom_values"
    )

    __table_args__ = (
        UniqueConstraint(
            "client_id",
            "field_id",
            name="uq_client_custom_field_value",
        ),
    )


class ClientAsset(Base):
    """An asset associated with a client."""

    __tablename__ = "client_assets"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True
    )

    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"),
        index=True,
    )

    asset_type: Mapped[str] = mapped_column(String(100))
    category: Mapped[Optional[str]] = mapped_column(String(40))
    fiscal_year: Mapped[Optional[str]] = mapped_column(String(7))
    description: Mapped[Optional[str]] = mapped_column(Text)
    location: Mapped[Optional[str]] = mapped_column(Text)
    make_model: Mapped[Optional[str]] = mapped_column(String(160))
    registration_number: Mapped[Optional[str]] = mapped_column(String(60))
    purchase_date: Mapped[Optional[date]] = mapped_column(Date)
    sale_date: Mapped[Optional[date]] = mapped_column(Date)
    purchase_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2))
    sale_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2))
    income_source_type: Mapped[Optional[str]] = mapped_column(String(100))
    annual_income: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2))

    valuation_amount: Mapped[Optional[float]] = mapped_column(
        Float
    )

    # Flexible metadata for asset-specific information.
    details_json: Mapped[Optional[str]] = mapped_column(Text)

    client: Mapped["Client"] = relationship(
        back_populates="assets"
    )

# =========================================


class AreaCalculationHistory(Base):
    """Stores a calculation and the conventions used to produce it."""

    __tablename__ = "area_calculation_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    operation: Mapped[str] = mapped_column(String(50), nullable=False)
    input_format: Mapped[str] = mapped_column(String(50), nullable=False)

    # JSON strings preserve the original inputs and selected conventions.
    inputs_json: Mapped[str] = mapped_column(Text, nullable=False)
    settings_json: Mapped[str] = mapped_column(Text, nullable=False)

    result: Mapped[str] = mapped_column(Text, nullable=False)
    calculation_details: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


# Stored units remain explicit strings so imported schedules can be understood
# without assuming or silently applying a unit conversion in the database.
DC_RATE_UNITS = (
    "per_square_foot",
    "per_square_yard",
    "per_square_meter",
    "per_marla",
    "per_kanal",
    "per_acre",
    "per_kila",
    "per_plot",
)


class PropertyLocation(Base):
    """Structured administrative and locality details for valuation rates."""

    __tablename__ = "property_locations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    province: Mapped[Optional[str]] = mapped_column(String(120))
    district: Mapped[Optional[str]] = mapped_column(String(120), index=True)
    tehsil: Mapped[Optional[str]] = mapped_column(String(120), index=True)
    # Schedule selectors mirror the administrative dimensions shown by the
    # Punjab DC valuation portal; nullable additions preserve older locations.
    land_type: Mapped[Optional[str]] = mapped_column(String(20), index=True)
    committee_town: Mapped[Optional[str]] = mapped_column(String(200), index=True)
    revenue_circle: Mapped[Optional[str]] = mapped_column(String(200), index=True)
    property_area: Mapped[Optional[str]] = mapped_column(String(200), index=True)
    qanoongoi: Mapped[Optional[str]] = mapped_column(String(200), index=True)
    patwar_circle: Mapped[Optional[str]] = mapped_column(String(200), index=True)
    square_no: Mapped[Optional[str]] = mapped_column(String(80), index=True)
    kila_no: Mapped[Optional[str]] = mapped_column(String(80), index=True)
    khasra_no: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    mauza: Mapped[Optional[str]] = mapped_column(String(200), index=True)
    hadbast_number: Mapped[Optional[str]] = mapped_column(String(50), index=True)
    locality: Mapped[Optional[str]] = mapped_column(String(200))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now,
        nullable=False,
    )

    dc_rates: Mapped[list["DCValuationRate"]] = relationship(
        back_populates="location", passive_deletes=True
    )
    valuation_estimates: Mapped[list["ValuationEstimate"]] = relationship(
        back_populates="location", passive_deletes=True
    )


class DCValuationRate(Base):
    """A source-traceable DC valuation rate for a location and property class."""

    __tablename__ = "dc_valuation_rates"
    __table_args__ = (
        CheckConstraint(
            "rate_unit IN (" + ", ".join(
                f"'{unit}'" for unit in DC_RATE_UNITS
            ) + ")",
            name="ck_dc_rate_unit",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_dc_rate_effective_dates",
        ),
        CheckConstraint(
            "rate_value >= 0", name="ck_dc_rate_non_negative"
        ),
        CheckConstraint(
            "verification_status IN ('unverified', 'verified')",
            name="ck_dc_rate_verification_status",
        ),
        CheckConstraint("schedule_type IN ('dc', 'fbr')", name="ck_valuation_schedule_type"),
        Index(
            "ix_dc_rate_lookup", "location_id", "property_type", "classification"
        ),
        Index(
            "ix_dc_rate_effective_dates", "effective_from", "effective_to"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # DC and FBR schedules stay independently searchable and source-traceable.
    schedule_type: Mapped[str] = mapped_column(
        String(10), default="dc", server_default="dc", nullable=False, index=True
    )
    location_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("property_locations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    property_type: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )
    classification: Mapped[Optional[str]] = mapped_column(String(120), index=True)
    floor: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    rate_value: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    rate_unit: Mapped[str] = mapped_column(String(32), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # Year printed on the schedule; effective_from is retained for existing
    # valuation queries and is normalized to January 1 of this year.
    published_year: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    effective_to: Mapped[Optional[date]] = mapped_column(Date, index=True)
    source_reference: Mapped[Optional[str]] = mapped_column(Text)
    source_date: Mapped[Optional[date]] = mapped_column(Date)
    verification_status: Mapped[str] = mapped_column(
        String(20), default="unverified", nullable=False, index=True
    )
    verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    verified_by: Mapped[Optional[str]] = mapped_column(String(150))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now,
        nullable=False,
    )

    location: Mapped[Optional[PropertyLocation]] = relationship(
        back_populates="dc_rates"
    )
    estimates: Mapped[list["ValuationEstimate"]] = relationship(
        back_populates="dc_rate", passive_deletes=True
    )


class TaxFeeRule(Base):
    """Configurable, source-traceable tax or fee rule; unverified by default."""

    __tablename__ = "tax_fee_rules"
    __table_args__ = (
        CheckConstraint(
            "calculation_method IN ('percentage', 'fixed_amount', 'per_unit')",
            name="ck_tax_fee_calculation_method",
        ),
        CheckConstraint(
            "verification_status IN ('unverified', 'verified')",
            name="ck_tax_fee_verification_status",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_tax_fee_effective_dates",
        ),
        CheckConstraint(
            "minimum_amount IS NULL OR maximum_amount IS NULL "
            "OR maximum_amount >= minimum_amount",
            name="ck_tax_fee_min_max_order",
        ),
        CheckConstraint(
            "rate_value IS NULL OR rate_value >= 0",
            name="ck_tax_fee_rate_non_negative",
        ),
        CheckConstraint(
            "fixed_amount IS NULL OR fixed_amount >= 0",
            name="ck_tax_fee_fixed_non_negative",
        ),
        CheckConstraint(
            "minimum_amount IS NULL OR minimum_amount >= 0",
            name="ck_tax_fee_min_non_negative",
        ),
        CheckConstraint(
            "maximum_amount IS NULL OR maximum_amount >= 0",
            name="ck_tax_fee_max_non_negative",
        ),
        Index(
            "ix_tax_fee_rule_lookup", "jurisdiction", "category",
            "effective_from",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    category: Mapped[str] = mapped_column(
        String(80), nullable=False, index=True
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(160), nullable=False, index=True
    )
    calculation_method: Mapped[str] = mapped_column(
        String(30), nullable=False
    )
    rate_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 8))
    fixed_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    rate_unit: Mapped[Optional[str]] = mapped_column(String(32))
    minimum_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    maximum_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    currency_code: Mapped[str] = mapped_column(
        String(3), default="PKR", nullable=False
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    effective_to: Mapped[Optional[date]] = mapped_column(Date, index=True)
    eligibility_conditions_json: Mapped[Optional[str]] = mapped_column(Text)
    source_reference: Mapped[Optional[str]] = mapped_column(Text)
    source_date: Mapped[Optional[date]] = mapped_column(Date)
    verification_status: Mapped[str] = mapped_column(
        String(20), default="unverified", nullable=False, index=True
    )
    verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    verified_by: Mapped[Optional[str]] = mapped_column(String(150))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now,
        nullable=False,
    )


class ValuationEstimate(Base):
    """Persisted estimate inputs and result snapshots for later review."""

    __tablename__ = "valuation_estimates"
    __table_args__ = (
        CheckConstraint(
            "area_value >= 0", name="ck_estimate_area_non_negative"
        ),
        CheckConstraint(
            "share_numerator > 0",
            name="ck_estimate_share_numerator_positive",
        ),
        CheckConstraint(
            "share_denominator > 0",
            name="ck_estimate_share_denominator_positive",
        ),
        CheckConstraint(
            "share_numerator <= share_denominator",
            name="ck_estimate_share_not_over_total",
        ),
        CheckConstraint(
            "declared_consideration IS NULL OR declared_consideration >= 0",
            name="ck_estimate_consideration_non_negative",
        ),
        CheckConstraint(
            "property_value IS NULL OR property_value >= 0",
            name="ck_estimate_property_value_non_negative",
        ),
        CheckConstraint(
            "transferred_value IS NULL OR transferred_value >= 0",
            name="ck_estimate_transferred_value_non_negative",
        ),
        CheckConstraint(
            "total_charges >= 0", name="ck_estimate_total_charges_non_negative"
        ),
        Index("ix_estimate_transaction_date", "transaction_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("clients.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    location_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("property_locations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    dc_rate_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("dc_valuation_rates.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    transaction_date: Mapped[date] = mapped_column(
        Date, nullable=False, index=True
    )
    property_type: Mapped[str] = mapped_column(String(100), nullable=False)
    classification: Mapped[Optional[str]] = mapped_column(String(120))
    area_value: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    area_unit: Mapped[str] = mapped_column(String(32), nullable=False)
    share_numerator: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    share_denominator: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    declared_consideration: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    property_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    transferred_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    total_charges: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), default=Decimal("0.00"), nullable=False
    )
    currency_code: Mapped[str] = mapped_column(
        String(3), default="PKR", nullable=False
    )
    input_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    client_snapshot_json: Mapped[Optional[str]] = mapped_column(Text)
    location_snapshot_json: Mapped[Optional[str]] = mapped_column(Text)
    rate_snapshot_json: Mapped[Optional[str]] = mapped_column(Text)
    result_snapshot_json: Mapped[Optional[str]] = mapped_column(Text)
    warnings_json: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    client: Mapped[Optional[Client]] = relationship(
        back_populates="valuation_estimates"
    )
    location: Mapped[Optional[PropertyLocation]] = relationship(
        back_populates="valuation_estimates"
    )
    dc_rate: Mapped[Optional[DCValuationRate]] = relationship(
        back_populates="estimates"
    )
    charges: Mapped[list["ValuationEstimateCharge"]] = relationship(
        back_populates="estimate", cascade="all, delete-orphan"
    )


class ValuationEstimateCharge(Base):
    """One calculated charge with enough rule data to explain old estimates."""

    __tablename__ = "valuation_estimate_charges"
    __table_args__ = (
        CheckConstraint(
            "amount >= 0", name="ck_estimate_charge_amount_non_negative"
        ),
        Index("ix_estimate_charge_estimate", "estimate_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    estimate_id: Mapped[int] = mapped_column(
        ForeignKey("valuation_estimates.id", ondelete="CASCADE"), nullable=False
    )
    tax_fee_rule_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tax_fee_rules.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    payer: Mapped[Optional[str]] = mapped_column(String(80))
    calculation_method: Mapped[str] = mapped_column(String(30), nullable=False)
    calculation_base: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    applied_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency_code: Mapped[str] = mapped_column(
        String(3), default="PKR", nullable=False
    )
    rule_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    estimate: Mapped[ValuationEstimate] = relationship(back_populates="charges")



