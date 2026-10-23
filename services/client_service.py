
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import case, func, or_, select
from sqlalchemy.exc import IntegrityError

from core.database import SessionLocal
from core.models import (
    Client,
    ClientAsset,
    ClientCustomFieldValue,
    CustomFieldDefinition,
)
from core.security import encrypt_secret


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
def list_clients(
    search: str = "",
    status: str = "",
    sort_by: str = "name",
    sort_direction: str = "asc",
) -> list[Client]:
    """Return filtered clients in a validated sort order and direction."""
    with SessionLocal() as session:
        pending_amount = func.coalesce(Client.payment_amount, 0) - func.coalesce(Client.amount_received, 0)
        direction = (sort_direction or "asc").strip().lower()
        descending = direction == "desc"

        def ordered(column):
            return column.desc() if descending else column.asc()

        paid_group = case(
            (
                Client.payment_amount.is_(None),
                2,
            ),
            (pending_amount <= 0, 0),
            else_=1,
        )
        sort_columns = {
            "modified": (ordered(Client.updated_at), ordered(func.lower(Client.name))),
            "name": (ordered(func.lower(Client.name)),),
            "tehsil": (
                ordered(func.lower(Client.tehsil)),
                ordered(func.lower(Client.district)),
                ordered(func.lower(Client.city)),
                ordered(func.lower(Client.name)),
            ),
            "district": (
                ordered(func.lower(Client.district)),
                ordered(func.lower(Client.tehsil)),
                ordered(func.lower(Client.city)),
                ordered(func.lower(Client.name)),
            ),
            "city": (
                ordered(func.lower(Client.city)),
                ordered(func.lower(Client.district)),
                ordered(func.lower(Client.tehsil)),
                ordered(func.lower(Client.name)),
            ),
            "pending": (ordered(pending_amount), ordered(func.lower(Client.name))),
            "paid": (
                ordered(paid_group),
                ordered(pending_amount),
                ordered(func.lower(Client.name)),
            ),
        }
        # Only predefined sort keys are accepted. Unknown directions default
        # to ascending order in ordered() above.
        statement = select(Client).order_by(
            *sort_columns.get(sort_by, sort_columns["name"])
        )

        if search.strip():
            term = f"%{search.strip()}%"
            statement = statement.where(
                # what the search term matches
                or_(
                    Client.name.ilike(term),
                    Client.father_name.ilike(term),
                    Client.cnic.ilike(term),
                    Client.referred_by.ilike(term),
                    Client.address.ilike(term),
                    Client.tehsil.ilike(term),
                    Client.district.ilike(term),
                    Client.city.ilike(term),
                    Client.province.ilike(term),
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


def list_referrers() -> list[str]:
    """Return distinct referral names for the editable form suggestions."""
    with SessionLocal() as session:
        statement = (
            select(func.min(Client.referred_by))
            .where(Client.referred_by.is_not(None), Client.referred_by != "")
            .group_by(func.lower(func.trim(Client.referred_by)))
            .order_by(func.lower(func.min(Client.referred_by)))
        )
        return list(session.scalars(statement).all())


def get_referrer_payment_totals() -> list[dict[str, Decimal | int | str]]:
    """Summarize total business, money received, and balance by referrer."""
    with SessionLocal() as session:
        statement = (
            select(
                func.min(Client.referred_by),
                func.count(Client.id),
                func.coalesce(func.sum(Client.payment_amount), 0),
                func.coalesce(func.sum(Client.amount_received), 0),
            )
            .where(Client.referred_by.is_not(None), Client.referred_by != "")
            .group_by(func.lower(func.trim(Client.referred_by)))
            .order_by(func.lower(func.min(Client.referred_by)))
        )
        results = []
        for referrer, count, business, received in session.execute(statement):
            total = Decimal(str(business or 0)).quantize(Decimal("0.01"))
            paid = Decimal(str(received or 0)).quantize(Decimal("0.01"))
            results.append({
                "referrer": referrer,
                "clients": count,
                "business": total,
                "received": paid,
                "pending": max(total - paid, Decimal("0.00")),
            })
        return results


def _parse_date(
    value: str, label: str, *, required: bool = False
) -> date | None:
    value = (value or "").strip()
    if not value:
        if required:
            raise ClientServiceError(f"{label} is required.")
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ClientServiceError(f"{label} must use YYYY-MM-DD format.") from exc


def _parse_amount(
    value: str | Decimal | None,
    label: str,
    *,
    default: str | None = None,
) -> Decimal:
    if value is None or (isinstance(value, str) and not value.strip()):
        if default is not None:
            return Decimal(default)
        raise ClientServiceError(f"{label} is required.")
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ClientServiceError(f"{label} must be a valid amount.") from exc
    if not amount.is_finite() or amount < 0:
        raise ClientServiceError(f"{label} must be a non-negative amount.")
    try:
        amount = amount.quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ClientServiceError(f"{label} is too large.") from exc
    if amount > Decimal("9999999999.99"):
        raise ClientServiceError(f"{label} exceeds the supported maximum.")
    return amount


def _validate_client_data(
    *, name, cnic, cnic_expiry_date, phone_number, email, referred_by,
    payment_amount, amount_received,
):
    normalized_name = (name or "").strip()
    if not normalized_name:
        raise ClientServiceError("Client name is required.")

    normalized_cnic = (cnic or "").strip()
    if not re.fullmatch(r"\d{5}-\d{7}-\d", normalized_cnic):
        raise ClientServiceError("CNIC is required in 00000-0000000-0 format.")

    normalized_referrer = (referred_by or "").strip()
    if not normalized_referrer:
        raise ClientServiceError("Referred by is required.")

    normalized_phone = (phone_number or "").strip()
    digits = re.sub(r"\D", "", normalized_phone)
    if (
        not 7 <= len(digits) <= 15
        or not re.fullmatch(r"\+?[0-9 ().-]+", normalized_phone)
    ):
        raise ClientServiceError("Enter a valid phone number (7 to 15 digits).")

    normalized_email = (email or "").strip()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", normalized_email):
        raise ClientServiceError("Enter a valid email address.")

    expiry = _parse_date(cnic_expiry_date, "CNIC expiry date", required=True)
    total = _parse_amount(payment_amount, "Payment")
    received = _parse_amount(amount_received, "Amount received", default="0")
    if received > total:
        raise ClientServiceError("Amount received cannot exceed the payment amount.")

    return (
        normalized_name, normalized_cnic, expiry, normalized_referrer,
        normalized_phone, normalized_email, total, received,
    )


def _save_custom_values(session, client: Client, values: dict[int, str]) -> None:
    """Validate and persist current active custom fields in the same transaction."""
    fields = list(
        session.scalars(
            select(CustomFieldDefinition).where(
                CustomFieldDefinition.entity_type == "client",
                CustomFieldDefinition.is_active.is_(True),
            )
        ).all()
    )
    old_values = {
        item.field_id: item
        for item in session.scalars(
            select(ClientCustomFieldValue).where(
                ClientCustomFieldValue.client_id == client.id
            )
        ).all()
    } if client.id is not None else {}

    for field in fields:
        raw = str(values.get(field.id, "") or "")
        if field.is_required and not raw.strip():
            raise ClientServiceError(f"{field.label} is required.")
        normalized = normalize_field_value(field.data_type, raw)
        record = old_values.get(field.id)
        if record is None and normalized is not None:
            session.add(ClientCustomFieldValue(
                client_id=client.id,
                field_id=field.id,
                value_text=normalized,
            ))
        elif record is not None:
            record.value_text = normalized


def _encrypt_optional(value: str | None) -> bytes | None:
    normalized = (value or "").strip()
    return encrypt_secret(normalized) if normalized else None


def create_client(
    *,
    name: str,
    father_name: str = "",
    cnic: str = "",
    cnic_expiry_date: str = "",
    date_of_birth: str = "",
    registration_date: str = "",
    status_updated_date: str = "",
    phone_number: str = "",
    email: str = "",
    address: str = "",
    tehsil: str = "",
    district: str = "",
    city: str = "",
    province: str = "",
    payment_amount: str | Decimal | None = None,
    amount_received: str | Decimal | None = None,
    current_status: str = "",
    referred_by: str = "",
    password: str = "",
    pin: str = "",
    custom_values: dict[int, str] | None = None,
) -> int:
    values = _validate_client_data(
        name=name, cnic=cnic, cnic_expiry_date=cnic_expiry_date,
        phone_number=phone_number, email=email, referred_by=referred_by,
        payment_amount=payment_amount, amount_received=amount_received,
    )
    name, cnic, expiry, referred_by, phone_number, email, total, received = values
    password = (password or "").strip()
    if not password:
        raise ClientServiceError("Password is required.")

    with SessionLocal.begin() as session:
        client = Client(
            name=name,
            father_name=father_name.strip() or None,
            cnic=cnic,
            cnic_expiry_date=expiry,
            date_of_birth=_parse_date(date_of_birth, "Date of birth"),
            registration_date=_parse_date(registration_date, "Registration date") or date.today(),
            status_updated_date=_parse_date(status_updated_date, "Status update date") or date.today(),
            phone_number=phone_number,
            email=email,
            address=address.strip() or None,
            tehsil=tehsil.strip() or None,
            district=district.strip() or None,
            city=city.strip() or None,
            province=province.strip() or None,
            payment_amount=total,
            amount_received=received,
            current_status=current_status.strip() or None,
            referred_by=referred_by,
            password_ciphertext=encrypt_secret(password),
            pin_ciphertext=_encrypt_optional(pin),
        )
        session.add(client)
        try:
            session.flush()
            _save_custom_values(session, client, custom_values or {})
            return client.id
        except IntegrityError as exc:
            raise ClientServiceError(
                "This CNIC already exists in the client database."
            ) from exc


def update_client(
    client_id: int,
    *,
    name: str,
    father_name: str = "",
    cnic: str = "",
    cnic_expiry_date: str = "",
    date_of_birth: str = "",
    registration_date: str = "",
    status_updated_date: str = "",
    phone_number: str = "",
    email: str = "",
    address: str = "",
    tehsil: str = "",
    district: str = "",
    city: str = "",
    province: str = "",
    payment_amount: str | Decimal | None = None,
    amount_received: str | Decimal | None = None,
    current_status: str = "",
    referred_by: str = "",
    password: str = "",
    pin: str = "",
    custom_values: dict[int, str] | None = None,
) -> None:
    values = _validate_client_data(
        name=name, cnic=cnic, cnic_expiry_date=cnic_expiry_date,
        phone_number=phone_number, email=email, referred_by=referred_by,
        payment_amount=payment_amount, amount_received=amount_received,
    )
    name, cnic, expiry, referred_by, phone_number, email, total, received = values

    with SessionLocal.begin() as session:
        client = session.get(Client, client_id)
        if client is None:
            raise ClientServiceError("Client record not found.")

        old_status = client.current_status
        new_status = current_status.strip() or None
        entered_status_date = _parse_date(
            status_updated_date, "Status update date"
        )
        client.name = name
        client.father_name = father_name.strip() or None
        client.cnic = cnic
        client.cnic_expiry_date = expiry
        client.date_of_birth = _parse_date(date_of_birth, "Date of birth")
        client.registration_date = _parse_date(registration_date, "Registration date") or client.registration_date or date.today()
        if (
            old_status != new_status
            and entered_status_date in {None, client.status_updated_date}
        ):
            client.status_updated_date = date.today()
        else:
            client.status_updated_date = (
                entered_status_date
                or client.status_updated_date
                or date.today()
            )
        client.phone_number = phone_number
        client.email = email
        client.address = address.strip() or None
        client.tehsil = tehsil.strip() or None
        client.district = district.strip() or None
        client.city = city.strip() or None
        client.province = province.strip() or None
        client.payment_amount = total
        client.amount_received = received
        client.current_status = new_status
        client.referred_by = referred_by
        new_password = (password or "").strip()
        if new_password:
            client.password_ciphertext = encrypt_secret(new_password)
        elif client.password_ciphertext is None:
            raise ClientServiceError("Password is required for this client.")
        if (pin or "").strip():
            client.pin_ciphertext = encrypt_secret(pin.strip())

        try:
            session.flush()
            _save_custom_values(session, client, custom_values or {})
        except IntegrityError as exc:
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
