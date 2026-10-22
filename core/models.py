
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
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
    description: Mapped[Optional[str]] = mapped_column(Text)

    valuation_amount: Mapped[Optional[float]] = mapped_column(
        Float
    )

    # Flexible metadata for asset-specific information.
    details_json: Mapped[Optional[str]] = mapped_column(Text)

    client: Mapped["Client"] = relationship(
        back_populates="assets"
    )
