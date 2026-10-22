
import flet as ft


def build_placeholder(
    title: str,
    description: str,
) -> ft.Control:
    return ft.Column(
        controls=[
            ft.Text(
                title,
                size=25,
                weight=ft.FontWeight.BOLD,
            ),
            ft.Text(
                description,
                size=15,
                color="#64748B",
            ),
            ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Text(
                            "Module under development",
                            size=18,
                            weight=ft.FontWeight.BOLD,
                        ),
                        ft.Text(
                            "This module has its own place in the "
                            "application. Its business logic will "
                            "be developed independently.",
                            color="#64748B",
                        ),
                    ],
                    spacing=12,
                ),
                padding=24,
                bgcolor="#FFFFFF",
                border_radius=12,
                expand=True,
            ),
        ],
        spacing=16,
        expand=True,
    )
