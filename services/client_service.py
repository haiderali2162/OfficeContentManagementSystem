
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from core.database import SessionLocal
from core.models import (
    Client,
    ClientAsset,
    ClientCustomFieldValue,
    CustomFieldDefinition,
)


class ClientServiceError(Exception):
    """Expected client-management error."""


SUPPORTED_FIELD_TYPES = {
    "text",
    "long_text",
    "integer",
    "decimal",
    "date",
    "boolean",
    "choice",
}


def normalize_field_value(data_type: str, value: str | None):
    """Validate and normalize a custom-field value."""
    if value is None or value == "":
        return None

    try:
        if data_type in {"text", "long_text", "choice"}:
            return value

        if data_type == "integer":
            return str(int(value))

        if data_type == "decimal":
            number = Decimal(value)
            if not number.is_finite():
                raise ValueError("Enter a finite decimal number.")
            return str(number)

        if data_type == "date":
            return date.fromisoformat(value).isoformat()

        if data_type == "boolean":
            normalized = value.strip().lower()
            if normalized not in {"true", "false", "yes", "no", "1", "0"}:
                raise ValueError("Enter Yes or No.")
            return str(normalized in {"true", "yes", "1"}).lower()

    except (ValueError, InvalidOperation) as exc:
        raise ClientServiceError(
            f"Invalid value for {data_type}: {value}"
        ) from exc

    raise ClientServiceError(
        f"Unsupported custom field type: {data_type}"
    )

# it fetches all the clients from the db
def list_clients(search: str = "", status: str = "") -> list[Client]:
    """Return clients matching the optional search and status."""
    with SessionLocal() as session:
        statement = select(Client).order_by(Client.name)

        if search.strip():
            term = f"%{search.strip()}%"
            statement = statement.where(
                # what the search term matches
                or_(
                    Client.name.ilike(term),
                    Client.father_name.ilike(term),
                    Client.cnic.ilike(term),
                    Client.referred_by.ilike(term),
                )
            )

        if status and status != "All statuses":
            statement = statement.where(
                Client.current_status == status
            )

        # Return detached records with the scalar fields loaded.
        return list(session.scalars(statement).all())


# get a client with its id
def get_client(client_id: int) -> Client | None:
    with SessionLocal() as session:
        return session.get(Client, client_id)


def create_client(
    *,
    name: str,
    father_name: str = "",
    cnic: str = "",
    current_status: str = "",
    referred_by: str = "",
) -> int:
    name = name.strip()

    if not name:
        raise ClientServiceError("Client name is required.")

    cnic = cnic.strip() or None

    if cnic and not (
        len(cnic) == 15
        and cnic[5] == "-"
        and cnic[13] == "-"
        and cnic[:5].isdigit()
        and cnic[6:13].isdigit()
        and cnic[14].isdigit()
    ):
        raise ClientServiceError(
            "Enter CNIC in 00000-0000000-0 format."
        )

    with SessionLocal() as session:
        client = Client(
            name=name,
            father_name=father_name.strip() or None,
            cnic=cnic,
            current_status=current_status.strip() or None,
            referred_by=referred_by.strip() or None,
        )

        session.add(client)

        try:
            session.commit()
            session.refresh(client)
            return client.id
        except IntegrityError as exc:
            session.rollback()
            raise ClientServiceError(
                "This CNIC already exists in the client database."
            ) from exc


def update_client(
    client_id: int,
    *,
    name: str,
    father_name: str = "",
    cnic: str = "",
    current_status: str = "",
    referred_by: str = "",
) -> None:
    name = name.strip()

    if not name:
        raise ClientServiceError("Client name is required.")

    cnic = cnic.strip() or None

    if cnic and not (
        len(cnic) == 15
        and cnic[5] == "-"
        and cnic[13] == "-"
        and cnic[:5].isdigit()
        and cnic[6:13].isdigit()
        and cnic[14].isdigit()
    ):
        raise ClientServiceError(
            "Enter CNIC in 00000-0000000-0 format."
        )

    with SessionLocal() as session:
        client = session.get(Client, client_id)

        if client is None:
            raise ClientServiceError("Client record not found.")

        client.name = name
        client.father_name = father_name.strip() or None
        client.cnic = cnic
        client.current_status = current_status.strip() or None
        client.referred_by = referred_by.strip() or None

        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise ClientServiceError(
                "This CNIC belongs to another client."
            ) from exc


def delete_client(client_id: int) -> None:
    with SessionLocal() as session:
        client = session.get(Client, client_id)

        if client is None:
            raise ClientServiceError("Client record not found.")

        session.delete(client)
        session.commit()


def list_custom_fields(
    entity_type: str = "client",
) -> list[CustomFieldDefinition]:
    with SessionLocal() as session:
        statement = (
            select(CustomFieldDefinition)
            .where(
                CustomFieldDefinition.entity_type == entity_type,
                CustomFieldDefinition.is_active.is_(True),
            )
            .order_by(CustomFieldDefinition.id)
        )
        return list(session.scalars(statement).all())


def create_custom_field(
    *,
    key: str,
    label: str,
    data_type: str,
    entity_type: str = "client",
    is_required: bool = False,
) -> int:
    import re

    key = key.strip().lower()
    label = label.strip()
    data_type = data_type.strip().lower()

    if not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", key):
        raise ClientServiceError(
            "Field key must start with a letter and contain "
            "only lowercase letters, digits, and underscores."
        )

    if not label:
        raise ClientServiceError("Column heading is required.")

    if data_type not in SUPPORTED_FIELD_TYPES:
        raise ClientServiceError("Unsupported field type.")

    with SessionLocal() as session:
        field = CustomFieldDefinition(
            entity_type=entity_type,
            key=key,
            label=label,
            data_type=data_type,
            is_required=is_required,
            is_active=True,
        )

        session.add(field)

        try:
            session.commit()
            session.refresh(field)
            return field.id
        except IntegrityError as exc:
            session.rollback()
            raise ClientServiceError(
                "A field with this key already exists."
            ) from exc


def delete_custom_field(field_id: int) -> None:
    """Deactivate a custom field without destroying historical data."""
    with SessionLocal() as session:
        field = session.get(CustomFieldDefinition, field_id)

        if field is None:
            raise ClientServiceError("Custom field not found.")

        field.is_active = False
        session.commit()


def save_custom_values(
    client_id: int,
    values: dict[int, str],
) -> None:
    """Validate and save values for a client's custom fields."""
    with SessionLocal() as session:
        client = session.get(Client, client_id)

        if client is None:
            raise ClientServiceError("Client record not found.")

        fields = list(
            session.scalars(
                select(CustomFieldDefinition).where(
                    CustomFieldDefinition.entity_type == "client",
                    CustomFieldDefinition.is_active.is_(True),
                )
            ).all()
        )

        existing_values = {
            item.field_id: item
            for item in session.scalars(
                select(ClientCustomFieldValue).where(
                    ClientCustomFieldValue.client_id == client_id
                )
            ).all()
        }

        for field in fields:
            raw = values.get(field.id, "")

            if field.is_required and not str(raw).strip():
                raise ClientServiceError(
                    f"{field.label} is required."
                )

            normalized = normalize_field_value(
                field.data_type, str(raw)
            )

            record = existing_values.get(field.id)

            if record is None:
                if normalized is not None:
                    session.add(
                        ClientCustomFieldValue(
                            client_id=client_id,
                            field_id=field.id,
                            value_text=normalized,
                        )
                    )
            else:
                record.value_text = normalized

        session.commit()


def get_custom_values(client_id: int) -> dict[int, str]:
    with SessionLocal() as session:
        rows = session.scalars(
            select(ClientCustomFieldValue).where(
                ClientCustomFieldValue.client_id == client_id
            )
        ).all()

        return {
            row.field_id: row.value_text or ""
            for row in rows
        }


def create_asset(
    *,
    client_id: int,
    asset_type: str,
    description: str = "",
    valuation_amount: Decimal | None = None,
) -> int:
    if not asset_type.strip():
        raise ClientServiceError("Asset type is required.")

    if valuation_amount is not None:
        if not valuation_amount.is_finite() or valuation_amount < 0:
            raise ClientServiceError(
                "Asset valuation must be a non-negative amount."
            )

    with SessionLocal() as session:
        if session.get(Client, client_id) is None:
            raise ClientServiceError("Client record not found.")

        asset = ClientAsset(
            client_id=client_id,
            asset_type=asset_type.strip(),
            description=description.strip() or None,
            valuation_amount=(
                float(valuation_amount)
                if valuation_amount is not None
                else None
            ),
        )

        session.add(asset)
        session.commit()
        session.refresh(asset)
        return asset.id


def list_assets(client_id: int) -> list[ClientAsset]:
    with SessionLocal() as session:
        statement = (
            select(ClientAsset)
            .where(ClientAsset.client_id == client_id)
            .order_by(ClientAsset.id.desc())
        )
        return list(session.scalars(statement).all())
