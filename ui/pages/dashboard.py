
import flet as ft


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
        padding=20,
        bgcolor="#FFFFFF",
        border_radius=12,
        border=ft.Border.all(1, "#E2E8F0"),
        expand=True,
    )


def build_dashboard() -> ft.Control:
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
            ft.Row(
                controls=[
                    metric_card(
                        "FBR Clients",
                        "—",
                        "Client statistics will appear here",
                    ),
                    metric_card(
                        "Valuation Records",
                        "—",
                        "Imported records will appear here",
                    ),
                    metric_card(
                        "Saved Calculations",
                        "—",
                        "Calculation history will appear here",
                    ),
                ],
                spacing=16,
            ),
            ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Text(
                            "Quick Start",
                            size=18,
                            weight=ft.FontWeight.BOLD,
                        ),
                        ft.Text(
                            "Choose a module from the sidebar to "
                            "start working. Client management, "
                            "area calculations, valuation, and "
                            "document preparation will be added "
                            "incrementally."
                        ),
                    ],
                    spacing=12,
                ),
                padding=22,
                bgcolor="#FFFFFF",
                border_radius=12,
                border=ft.Border.all(1, "#E2E8F0"),
            ),
        ],
        spacing=20,
        expand=True,
    )
