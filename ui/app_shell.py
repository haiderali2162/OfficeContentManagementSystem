
import flet as ft

from core.config import APP_NAME, APP_VERSION
from ui.navigation import NavigationItem, build_navigation



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

    def render(self) -> None:
        item = self.get_current_item()

        # Building the selected page runs its initial data query; the completed
        # layout is attached to Page and sent to the Flet client below.
        content = ft.Column(
            controls=[
                self.build_topbar(item),
                ft.Container(
                    # content=item.page_builder(),
                    content=item.page_builder(self.page),
                    padding=26,
                    expand=True,
                ),
            ],
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
