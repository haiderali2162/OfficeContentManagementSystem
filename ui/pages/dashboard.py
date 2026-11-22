"""Office overview dashboard with live counts and recent activity."""

import flet as ft

from services.dashboard_service import get_dashboard_stats


def metric_card(title: str, value: str, description: str):
    return ft.Container(
        content=ft.Column(
            controls=[
                ft.Text(title, size=13, color="#64748B"),
                ft.Text(
                    value,
                    size=25,
                    weight=ft.FontWeight.BOLD,
                    color="#172033",
                ),
                ft.Text(
                    description,
                    size=12,
                    color="#64748B",
                ),
            ],
            spacing=8,
        ),
        width=340,
        padding=20,
        bgcolor="#FFFFFF",
        border_radius=12,
        border=ft.Border.all(1, "#E2E8F0"),
    )


def _recent_line(left: str, right: str) -> ft.Control:
    return ft.Row(
        controls=[
            ft.Text(left, size=12, expand=True, max_lines=1,
                    overflow=ft.TextOverflow.ELLIPSIS),
            ft.Text(right, size=12, color="#64748B"),
        ],
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )


def build_dashboard() -> ft.Control:
    """Live overview: counts from the office database plus recent activity.

    Read-only queries; a database problem degrades the numbers to "—" with
    a note instead of breaking the page.
    """
    try:
        stats = get_dashboard_stats()
        error = ""
    except Exception as exc:
        stats = {}
        error = f"Could not read the dashboard statistics: {exc}"

    total_clients = stats.get("clients_total", 0)
    active = stats.get("clients_active", 0)
    pending = stats.get("clients_pending", 0)
    inactive = stats.get("clients_inactive", 0)
    dc_rates = stats.get("dc_rates", 0)
    fbr_rates = stats.get("fbr_rates", 0)
    estimates = stats.get("estimates", 0)
    area_calcs = stats.get("area_calculations", 0)
    naqsha = stats.get("naqsha_records", 0)

    metrics = ft.Row(
        controls=[
            metric_card(
                "FBR Clients",
                str(total_clients),
                f"{active} active · {pending} pending · {inactive} inactive",
            ),
            metric_card(
                "Valuation Records",
                str(dc_rates + fbr_rates),
                f"{dc_rates} DC · {fbr_rates} FBR schedule rows",
            ),
            metric_card(
                "Saved Estimates",
                str(estimates),
                "Valuation & Taxes calculation history",
            ),
        ],
        spacing=16,
        wrap=True,
    )
    secondary = ft.Row(
        controls=[
            metric_card(
                "Area Calculations",
                str(area_calcs),
                "Area Calculator history entries",
            ),
            metric_card(
                "Naqsha Khewats",
                str(naqsha),
                "Saved land & owner records",
            ),
        ],
        spacing=16,
        wrap=True,
    )

    recent_estimates_controls: list[ft.Control] = []
    for item in stats.get("recent_estimates", []):
        recent_estimates_controls.append(_recent_line(
            f"Estimate #{item['id']} · {item['property_type']}",
            f"{item['transaction_date']} · Rs. {item['total_charges']}",
        ))
    if not recent_estimates_controls:
        recent_estimates_controls.append(ft.Text(
            "No saved estimates yet — calculate one in Valuation & Taxes.",
            size=12, color="#64748B",
        ))

    recent_clients_controls: list[ft.Control] = []
    for item in stats.get("recent_clients", []):
        recent_clients_controls.append(_recent_line(
            item["name"], f"{item['current_status']} · added {item['created_at']}",
        ))
    if not recent_clients_controls:
        recent_clients_controls.append(ft.Text(
            "No clients yet — add or import them in FBR Clients.",
            size=12, color="#64748B",
        ))

    activity = ft.Row(
        controls=[
            ft.Container(
                ft.Column([
                    ft.Text("Recent estimates", size=14,
                            weight=ft.FontWeight.BOLD),
                    *recent_estimates_controls,
                ], spacing=6, scroll=ft.ScrollMode.AUTO, height=240),
                width=520,
                padding=16,
                bgcolor="#FFFFFF",
                border=ft.Border.all(1, "#E2E8F0"),
                border_radius=12,
            ),
            ft.Container(
                ft.Column([
                    ft.Text("Recently added clients", size=14,
                            weight=ft.FontWeight.BOLD),
                    *recent_clients_controls,
                ], spacing=6, scroll=ft.ScrollMode.AUTO, height=240),
                width=520,
                padding=16,
                bgcolor="#FFFFFF",
                border=ft.Border.all(1, "#E2E8F0"),
                border_radius=12,
            ),
        ],
        spacing=16,
        wrap=True,
    )

    return ft.Column(
        controls=[
            ft.Text(
                "Office Overview",
                size=25,
                weight=ft.FontWeight.BOLD,
            ),
            ft.Text(
                "Your land, revenue, and client workspace.",
                color="#64748B",
            ),
            metrics,
            secondary,
            activity,
            *([ft.Text(error, size=12, color="#B91C1C", selectable=True)]
              if error else []),
        ],
        spacing=16,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
    )
