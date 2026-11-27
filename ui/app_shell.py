"""Application shell: sidebar, shared header, and app-wide setup warnings.

v0.2.30 adds a compact warning banner above the selected page. It comes from
``services.setup_warning_service`` and is deliberately read-only: page-level
warnings and calculation warnings remain where they are, while this banner
answers "what is missing in the active setup?" from every tab.
"""

import flet as ft

from core.config import APP_NAME, APP_VERSION
from services.setup_warning_service import visible_setup_warnings
from ui.navigation import NavigationItem, build_navigation
from ui.pages.valuation import build_valuation_page


class AppShell:
    def __init__(self, page: ft.Page):
        self.page = page
        self.items: list[NavigationItem] = build_navigation()
        self.current_key = "dashboard"

    def start(self) -> None:
        self.page.title = f"{APP_NAME} v{APP_VERSION}"
        self.page.padding = 0
        self.page.bgcolor = "#F4F6FA"
        self.page.theme_mode = ft.ThemeMode.LIGHT

        self.render()

    def navigate(self, key: str) -> None:
        self.current_key = key
        self.render()

    def get_current_item(self) -> NavigationItem:
        for item in self.items:
            if item.key == self.current_key:
                # key matched, return it
                return item

        return self.items[0]

    # responsible to build a navigation item
    def build_nav_item(self, item: NavigationItem) -> ft.Control:
        selected = item.key == self.current_key

        icon_controls = []

        # if item has an icon
        if item.icon is not None:
            icon_controls.append(
                ft.Icon(
                    item.icon,
                    size=19,
                    color="#FFFFFF" if selected else "#64748B",
                )
            )

        icon_controls.append(
            ft.Text(
                item.title,
                size=13,
                color="#FFFFFF" if selected else "#334155",
                weight=(
                    ft.FontWeight.BOLD
                    if selected
                    else ft.FontWeight.NORMAL
                ),
            )
        )

        return ft.Container(
            content=ft.Row(
                controls=icon_controls,
                spacing=12,
            ),
            padding=12,
            border_radius=9,
            bgcolor="#2457C5" if selected else None,
            ink=True,
            on_click=lambda e, key=item.key: self.navigate(key),
        )

    def build_sidebar(self) -> ft.Control:
        return ft.Container(
            content=ft.Column(
                controls=[
                    ft.Container(
                        content=ft.Column(
                            controls=[
                                ft.Text(
                                    "LAND OFFICE",
                                    size=19,
                                    weight=ft.FontWeight.BOLD,
                                    color="#FFFFFF",
                                ),
                                ft.Text(
                                    "Revenue & FBR",
                                    size=12,
                                    color="#CBD5E1",
                                ),
                            ],
                            spacing=5,
                        ),
                        padding=ft.Padding(
                            left=18,
                            right=12,
                            top=22,
                            bottom=22,
                        ),
                    ),
                    ft.Divider(color="#334155", height=1),
                    ft.Container(height=8),
                    ft.Column(
                        controls=[
                            self.build_nav_item(item)
                            for item in self.items
                        ],
                        spacing=5,
                        scroll=ft.ScrollMode.AUTO,
                        expand=True,
                    ),
                    ft.Divider(color="#334155", height=1),
                    ft.Container(
                        content=ft.Text(
                            f"Version {APP_VERSION}",
                            size=11,
                            color="#CBD5E1",
                        ),
                        padding=16,
                    ),
                ],
                spacing=0,
                expand=True,
            ),
            width=245,
            bgcolor="#172033",
            padding=10,
        )

    def build_topbar(self, item: NavigationItem) -> ft.Control:
        return ft.Container(
            data="shell_topbar",
            content=ft.Row(
                controls=[
                    ft.Column(
                        controls=[
                            ft.Text(
                                item.title,
                                size=21,
                                weight=ft.FontWeight.BOLD,
                                color="#172033",
                            ),
                            ft.Text(
                                item.description,
                                size=12,
                                color="#64748B",
                            ),
                        ],
                        spacing=4,
                        expand=True,
                    ),
                    ft.Text(
                        APP_NAME,
                        size=12,
                        color="#64748B",
                    ),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding(
                left=26,
                right=26,
                top=20,
                bottom=20,
            ),
            bgcolor="#FFFFFF",
        )

    def build_warning_banner(self) -> ft.Control | None:
        """Build the v0.2.30 setup warning banner, or ``None`` when clear.

        Only the first two warnings are named inline; the count keeps the
        banner short. Settings shows every warning with its fix and an option
        to ignore a stable warning code.
        """
        warnings = visible_setup_warnings()
        if not warnings:
            return None
        warning_count = len(warnings)
        has_warning = any(item["severity"] == "warning" for item in warnings)
        colour = "#9A3412" if has_warning else "#334155"
        titles = "; ".join(item["title"] for item in warnings[:2])
        if warning_count > 2:
            titles += f"; +{warning_count - 2} more"
        return ft.Container(
            data="setup_warning_banner",
            bgcolor="#FFFBEB" if has_warning else "#F1F5F9",
            border=ft.Border.all(1, "#FDE68A" if has_warning else "#CBD5E1"),
            border_radius=9,
            padding=ft.Padding(left=18, right=18, top=11, bottom=11),
            content=ft.Row(
                controls=[
                    ft.Column(
                        controls=[
                            ft.Text(
                                f"Setup check: {warning_count} item(s) need attention",
                                size=12,
                                weight=ft.FontWeight.BOLD,
                                color=colour,
                            ),
                            ft.Text(titles, size=12, color=colour, selectable=True),
                        ],
                        spacing=3,
                        expand=True,
                    ),
                    ft.TextButton(
                        "Review in Settings",
                        on_click=lambda _: self.navigate("settings"),
                    ),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def render(self) -> None:
        item = self.get_current_item()
        topbar = self.build_topbar(item)

        def set_rates_tab_chrome(is_rates_tab: bool) -> None:
            """Give the rate schedule tab its full viewport height."""
            topbar.visible = not is_rates_tab
            self.page.update()

        if item.key == "valuation":
            page_content = build_valuation_page(
                self.page,
                on_rates_tab_change=set_rates_tab_chrome,
            )
        else:
            page_content = item.page_builder(self.page)

        # Building the selected page runs its initial data query; the completed
        # layout is attached to Page and sent to the Flet client below.
        # v0.2.30: the warning banner is a sibling of the shared header, so
        # page-level control trees keep their own structure.
        content_controls: list[ft.Control] = [topbar]
        banner = self.build_warning_banner()
        if banner is not None:
            content_controls.append(banner)
        content_controls.append(
            ft.Container(
                data="shell_page_content",
                content=page_content,
                padding=26,
                expand=True,
            )
        )
        content = ft.Column(
            controls=content_controls,
            spacing=0,
            expand=True,
        )

        layout = ft.Row(
            controls=[
                self.build_sidebar(),
                ft.Container(
                    content=content,
                    expand=True,
                ),
            ],
            spacing=0,
            expand=True,
            vertical_alignment=ft.CrossAxisAlignment.STRETCH,
        )

        self.page.controls.clear()
        self.page.add(layout)
        self.page.update()
