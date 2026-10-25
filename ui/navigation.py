
from dataclasses import dataclass
from typing import Callable

import flet as ft
from ui.pages.area_calculator import build_area_calculator_page


# model for navigation
@dataclass(frozen=True)
class NavigationItem:
    key: str
    title: str
    description: str
    icon: object
    page_builder: Callable[[ft.Page], ft.Control]


def get_icon(name: str):
    namespace = getattr(ft, "Icons", None)

    if namespace is None:
        namespace = getattr(ft, "icons", None)

    if namespace is None:
        return None

    return getattr(namespace, name, None)


"""
responsible for navigation
"""
def build_navigation() -> list[NavigationItem]:
    from ui.pages.dashboard import build_dashboard
    from ui.pages.placeholder import build_placeholder
    from ui.pages.clients import build_clients_page
    from ui.pages.dc_rates import build_dc_rates_page

    def placeholder(title: str, description: str):
        return lambda page: build_placeholder(title, description)

    return [
        NavigationItem(
            key="dashboard",
            title="Dashboard",
            description="Overview of your office",
            icon=get_icon("DASHBOARD"),
            page_builder=lambda page: build_dashboard(),
        ),
        NavigationItem(
            key="clients",
            title="FBR Clients",
            description="Client records, custom fields, and assets",
            icon=get_icon("PEOPLE"),
            page_builder=build_clients_page,
        ),
        # NavigationItem(
        #     key="revenue",
        #     title="Revenue Calculator",
        #     description="Land area and unit conversions",
        #     icon=get_icon("CALCULATE"),
        #     page_builder=placeholder(
        #         "Revenue Calculator",
        #         "Area calculations will be implemented in Part 4.",
        #     ),
        # ),
        NavigationItem(
                    key="area_calculator",
                    title="Area Calculator",
                    description="Land area calculations and conversions",
                    icon=ft.Icons.CALCULATE,
                    page_builder=build_area_calculator_page,
                ),
        NavigationItem(
            key="valuation",
            title="Valuation & Taxes",
            description="DC rates, taxes, and deed fees",
            icon=get_icon("ACCOUNT_BALANCE"),
            page_builder=build_dc_rates_page,
        ),
        NavigationItem(
            key="naqsha",
            title="Naqsha Alif / Bay / Jeem",
            description="Prepare land-related tables",
            icon=get_icon("TABLE_CHART"),
            page_builder=placeholder(
                "Naqsha Alif / Bay / Jeem",
                "Data imports and document preparation are planned.",
            ),
        ),
        NavigationItem(
            key="inheritance",
            title="Inheritance",
            description="Calculate inheritance shares",
            icon=get_icon("ACCOUNT_TREE"),
            page_builder=placeholder(
                "Inheritance Calculator",
                "Inheritance calculations are planned.",
            ),
        ),
        NavigationItem(
            key="drawings",
            title="Aks / Masavi / Shajra",
            description="Drawing and mapping workspace",
            icon=get_icon("DRAW"),
            page_builder=placeholder(
                "Aks / Masavi / Shajra",
                "The drawing workspace is planned.",
            ),
        ),
        NavigationItem(
            key="extensions",
            title="Extensions",
            description="Additional application features",
            icon=get_icon("EXTENSION"),
            page_builder=placeholder(
                "Extensions",
                "Extension management is planned.",
            ),
        ),
        NavigationItem(
            key="settings",
            title="Settings",
            description="Application preferences",
            icon=get_icon("SETTINGS"),
            page_builder=placeholder(
                "Settings",
                "Application settings are planned.",
            ),
        ),
    ]
