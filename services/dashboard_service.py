"""Live office statistics for the dashboard page.

The dashboard used to be a static placeholder; it now shows real counts
(FBR clients by status, DC/FBR schedule rows, saved estimates, area
calculations, Naqsha records) plus the most recent activity, all through
read-only COUNT/SELECT queries so the dashboard never writes anything.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from core.database import SessionLocal
from core.models import (
    AreaCalculationHistory,
    Client,
    DCValuationRate,
    NaqshaRecord,
    ValuationEstimate,
)


def get_dashboard_stats() -> dict[str, Any]:
    """Read-only counts and recent activity for the dashboard cards."""
    with SessionLocal() as session:
        clients_total = session.scalar(
            select(func.count(Client.id))
        ) or 0
        status_counts = dict(session.execute(
            select(Client.current_status, func.count(Client.id))
            .where(Client.current_status.is_not(None))
            .group_by(Client.current_status)
        ).all())
        dc_rates = session.scalar(
            select(func.count(DCValuationRate.id)).where(
                DCValuationRate.schedule_type == "dc"
            )
        ) or 0
        fbr_rates = session.scalar(
            select(func.count(DCValuationRate.id)).where(
                DCValuationRate.schedule_type == "fbr"
            )
        ) or 0
        estimates = session.scalar(
            select(func.count(ValuationEstimate.id))
        ) or 0
        area_calculations = session.scalar(
            select(func.count(AreaCalculationHistory.id))
        ) or 0
        naqsha_records = session.scalar(
            select(func.count(NaqshaRecord.id))
        ) or 0
        recent_estimates = [
            {
                "id": row.id,
                "transaction_date": str(row.transaction_date),
                "property_type": row.property_type,
                "total_charges": str(row.total_charges),
            }
            for row in session.scalars(
                select(ValuationEstimate)
                .order_by(ValuationEstimate.id.desc())
                .limit(5)
            ).all()
        ]
        recent_clients = [
            {
                "name": row.name,
                "current_status": row.current_status or "—",
                "created_at": row.created_at.strftime("%Y-%m-%d"),
            }
            for row in session.scalars(
                select(Client).order_by(Client.id.desc()).limit(5)
            ).all()
        ]
    return {
        "clients_total": clients_total,
        "clients_active": status_counts.get("Active", 0),
        "clients_pending": status_counts.get("Pending", 0),
        "clients_inactive": status_counts.get("Inactive", 0),
        "dc_rates": dc_rates,
        "fbr_rates": fbr_rates,
        "estimates": estimates,
        "area_calculations": area_calculations,
        "naqsha_records": naqsha_records,
        "recent_estimates": recent_estimates,
        "recent_clients": recent_clients,
    }
