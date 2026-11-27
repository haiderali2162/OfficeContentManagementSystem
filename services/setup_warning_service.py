"""Read-only, app-wide setup and missing-data warnings (v0.2.30).

Earlier versions reported missing data only inside the page where the user
happened to be working. A fresh or wrongly connected database could therefore
look normal until a calculation produced zero charges. This service collects
those facts once, in one place, and exposes stable warning codes so the app
shell and Settings can show the same truthful information.

The collector never writes data, never invents a legal/tax fact, and never
includes connection credentials in a message. A rate or rule that is not marked
verified is only described as an estimate; it is never presented as official.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select

from core.database import SessionLocal
from core.models import (
    Client,
    DCValuationRate,
    RegistryFeeFormula,
    TaxFeeRule,
)
from services import backup_service
from services.settings_service import (
    get_default_file_location,
    get_setup_warning_settings,
)


def _warning(
    *,
    code: str,
    check: str,
    severity: str,
    title: str,
    message: str,
    fix: str,
    nav_key: str,
) -> dict[str, Any]:
    """Build one warning with fields shared by the banner and Settings."""
    return {
        "code": code,
        "check": check,
        "severity": severity,
        "title": title,
        "message": message,
        "fix": fix,
        "nav_key": nav_key,
    }


def _count_warnings(session: Any) -> list[dict[str, Any]]:
    """Turn read-only counts into the data/setup warnings they imply."""
    clients = session.scalar(select(func.count(Client.id))) or 0
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
    tax_rules = session.scalar(select(func.count(TaxFeeRule.id))) or 0
    formulas = session.scalar(select(func.count(RegistryFeeFormula.id))) or 0
    unverified_dc = session.scalar(
        select(func.count(DCValuationRate.id)).where(
            or_(
                DCValuationRate.verification_status.is_(None),
                DCValuationRate.verification_status != "verified",
            )
        )
    ) or 0
    unverified_rules = session.scalar(
        select(func.count(TaxFeeRule.id)).where(
            or_(
                TaxFeeRule.verification_status.is_(None),
                TaxFeeRule.verification_status != "verified",
            )
        )
    ) or 0
    unverified_formulas = session.scalar(
        select(func.count(RegistryFeeFormula.id)).where(
            or_(
                RegistryFeeFormula.verification_status.is_(None),
                RegistryFeeFormula.verification_status != "verified",
            )
        )
    ) or 0

    warnings: list[dict[str, Any]] = []
    if not clients:
        warnings.append(_warning(
            code="no_clients",
            check="clients",
            severity="info",
            title="No clients saved",
            message="The client dropdown is empty. Estimates can still be saved with task number and waseeka name.",
            fix="Add or import clients in FBR Clients if you want to link them to estimates.",
            nav_key="clients",
        ))
    if not dc_rates:
        warnings.append(_warning(
            code="no_dc_rates",
            check="dc_rates",
            severity="warning",
            title="No DC schedule rates",
            message="DC valuation cannot calculate a rate until a schedule is imported or added.",
            fix="Open DC / FBR rates and add or import the DC schedule for your area.",
            nav_key="valuation",
        ))
    if not fbr_rates:
        warnings.append(_warning(
            code="no_fbr_rates",
            check="fbr_rates",
            severity="info",
            title="No FBR schedule rates",
            message="FBR comparison will be unavailable until an FBR schedule is saved.",
            fix="Open DC / FBR rates and add or import the FBR schedule when you need that comparison.",
            nav_key="valuation",
        ))
    if not tax_rules:
        warnings.append(_warning(
            code="no_tax_rules",
            check="tax_rules",
            severity="warning",
            title="No tax or fee rules",
            message="Charge totals will be zero because no tax or fee rules exist in the active database.",
            fix="Open Valuation & Taxes and add rules only from a verified authoritative source.",
            nav_key="valuation",
        ))
    if not formulas:
        warnings.append(_warning(
            code="no_registry_formulas",
            check="registry_formulas",
            severity="info",
            title="No registry fee formulas",
            message="Registry-fee charges will not be calculated until a formula is saved.",
            fix="Add a formula in Valuation & Taxes when a verified registry fee rule is available.",
            nav_key="valuation",
        ))
    if unverified_dc:
        warnings.append(_warning(
            code="unverified_dc_rates",
            check="verification",
            severity="warning",
            title="Unverified DC rates",
            message=f"{unverified_dc} DC rate(s) are not marked verified; valuations using them are estimates.",
            fix="Check the source/reference and mark a rate verified only after confirming it from an authoritative source.",
            nav_key="valuation",
        ))
    if unverified_rules:
        warnings.append(_warning(
            code="unverified_tax_rules",
            check="verification",
            severity="warning",
            title="Unverified tax and fee rules",
            message=f"{unverified_rules} rule(s) are not marked verified; charges from them are estimates.",
            fix="Review each rule's source/reference before relying on its amount.",
            nav_key="valuation",
        ))
    if unverified_formulas:
        warnings.append(_warning(
            code="unverified_registry_formulas",
            check="verification",
            severity="warning",
            title="Unverified registry formulas",
            message=f"{unverified_formulas} formula(s) are not marked verified; calculated fees are estimates.",
            fix="Review each formula's source/reference before relying on its result.",
            nav_key="valuation",
        ))
    return warnings


def _folder_warnings() -> list[dict[str, Any]]:
    """Check user-selected folders and backup readiness without writing."""
    warnings: list[dict[str, Any]] = []
    if not get_default_file_location():
        warnings.append(_warning(
            code="no_default_folder",
            check="default_folder",
            severity="info",
            title="No default export folder",
            message="Every import and export action will ask for a location.",
            fix="Choose a default folder in Settings → Files.",
            nav_key="settings",
        ))

    backup = backup_service.get_backup_settings()
    if not backup["enabled"]:
        warnings.append(_warning(
            code="backups_disabled",
            check="backups",
            severity="warning",
            title="Automatic backups are off",
            message="A database problem could lose recent work because no scheduled backup is active.",
            fix="Enable automatic backups and choose a backup folder in Settings when this is a live office PC.",
            nav_key="settings",
        ))
    else:
        location = str(backup["location"] or "").strip()
        if not location or not Path(location).is_dir():
            warnings.append(_warning(
                code="backup_folder_missing",
                check="backups",
                severity="warning",
                title="Backup folder is missing",
                message="Automatic backups are enabled but the chosen backup folder does not exist, so backups cannot run.",
                fix="Open Settings → Automatic backups and choose an existing folder with Browse folder.",
                nav_key="settings",
            ))
    return warnings


def collect_setup_warnings() -> list[dict[str, Any]]:
    """Return all current setup warnings with their visibility state.

    ``visible`` already applies the saved master switch, severity choice,
    per-check switches, and suppressed codes. Settings still receives hidden
    warnings so the user can see and unsuppress what was turned off.
    """
    settings = get_setup_warning_settings()
    ignored = set(settings["ignored_codes"])

    try:
        with SessionLocal() as session:
            warnings = _count_warnings(session)
    except Exception:
        # Never expose a connection URL/password in a global message. The
        # Settings database page already shows safe validation detail.
        warnings = [_warning(
            code="database_read_failed",
            check="database",
            severity="warning",
            title="Cannot read setup status",
            message="The active database could not be read, so missing-data checks are unavailable.",
            fix="Open Settings → Database and check the connection.",
            nav_key="settings",
        )]

    warnings.extend(_folder_warnings())
    for warning in warnings:
        code = warning["code"]
        check_enabled = bool(settings["checks"].get(warning["check"], False))
        severity_allowed = (
            warning["severity"] == "warning" or settings["show_info"]
        )
        warning["ignored"] = code in ignored
        warning["check_enabled"] = check_enabled
        warning["visible"] = (
            settings["enabled"]
            and check_enabled
            and severity_allowed
            and code not in ignored
        )
    return warnings


def visible_setup_warnings() -> list[dict[str, Any]]:
    """Return only warnings that should currently be shown app-wide."""
    return [item for item in collect_setup_warnings() if item["visible"]]
