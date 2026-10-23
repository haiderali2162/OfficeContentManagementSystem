
"""Persistent history for land-area calculations."""

import json

from sqlalchemy import select, desc

from core.database import SessionLocal
from core.models import AreaCalculationHistory


def save_calculation(
    *,
    operation: str,
    input_format: str,
    inputs: dict,
    settings: dict,
    result: str,
    details: str,
) -> int:
    """Save one completed calculation and return its database ID."""

    record = AreaCalculationHistory(
        operation=operation,
        input_format=input_format,
        inputs_json=json.dumps(inputs, ensure_ascii=False),
        settings_json=json.dumps(settings, ensure_ascii=False),
        result=result,
        calculation_details=details,
    )

    with SessionLocal() as session:
        session.add(record)
        session.commit()
        session.refresh(record)
        return record.id


def list_calculations(limit: int = 100) -> list[dict]:
    """Return the most recent calculations first."""

    limit = max(1, min(int(limit), 1000))

    with SessionLocal() as session:
        statement = (
            select(AreaCalculationHistory)
            .order_by(desc(AreaCalculationHistory.id))
            .limit(limit)
        )

        records = session.scalars(statement).all()

        # Convert database models to plain dictionaries before closing
        # the session. The UI does not depend on detached ORM objects.
        return [
            {
                "id": record.id,
                "operation": record.operation,
                "input_format": record.input_format,
                "inputs": json.loads(record.inputs_json),
                "settings": json.loads(record.settings_json),
                "result": record.result,
                "details": record.calculation_details,
                "created_at": (
                    record.created_at.isoformat()
                    if record.created_at
                    else ""
                ),
            }
            for record in records
        ]


def get_calculation(calculation_id: int) -> dict | None:
    """Retrieve one calculation by its ID."""

    with SessionLocal() as session:
        record = session.get(AreaCalculationHistory, calculation_id)

        if record is None:
            return None

        return {
            "id": record.id,
            "operation": record.operation,
            "input_format": record.input_format,
            "inputs": json.loads(record.inputs_json),
            "settings": json.loads(record.settings_json),
            "result": record.result,
            "details": record.calculation_details,
            "created_at": (
                record.created_at.isoformat()
                if record.created_at
                else ""
            ),
        }
