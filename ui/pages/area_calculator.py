
"""Land Area Calculator page.

Version 1.0.0
Uses the shared area engine and persistent SQLite calculation history.
"""

import traceback

import flet as ft

from core.area_engine import (
    AreaError,
    AreaSettings,
    LandArea,
    sum_areas,
    subtract_areas,
    average_area,
    calculate_percentage_share,
)
from services.area_history_service import (
    save_calculation,
    list_calculations,
    get_calculation,
)


INPUT_FORMATS = [
    "Kanal-Marla-Foot",
    "Kanal-Marla-Sarsahi",
    "Acres",
    "Kila",
    "Square Karam",
    "Karam Dimensions",
]

OPERATIONS = [
    "Convert",
    "Add",
    "Subtract",
    "Average",
    "Percentage Share",
]


def build_area_calculator_page(page: ft.Page) -> ft.Control:
    """Build the area calculator and history interface."""

    format_dropdown = ft.Dropdown(
        label="Input format",
        value="Kanal-Marla-Foot",
        options=[ft.dropdown.Option(x) for x in INPUT_FORMATS],
        expand=True,
    )

    operation_dropdown = ft.Dropdown(
        label="Operation",
        value="Convert",
        options=[ft.dropdown.Option(x) for x in OPERATIONS],
        expand=True,
    )

    marla_dropdown = ft.Dropdown(
        label="Square feet per marla",
        value="272",
        options=[
            ft.dropdown.Option("272"),
            ft.dropdown.Option("225"),
        ],
        expand=True,
    )

    acre_dropdown = ft.Dropdown(
        label="Kanals per acre",
        value="8",
        options=[
            ft.dropdown.Option("8"),
            # ft.dropdown.Option("8.5"),
        ],
        expand=True,
    )

    first_input = ft.TextField(
        label="First area / value",
        hint_text="Example: 2-5-100",
        expand=True,
    )

    second_input = ft.TextField(
        label="Second area / value",
        hint_text="Required for some operations",
        expand=True,
    )

    third_input = ft.TextField(
        label="Third area (for Average)",
        hint_text="Optional unless averaging",
        expand=True,
    )

    calculate_button = ft.Button(
        "Calculate",
    )

    result_text = ft.Text(
        "Your result will appear here.",
        selectable=True,
    )

    details_text = ft.Text(
        "",
        selectable=True,
    )

    history_column = ft.Column(
        spacing=8,
        scroll=ft.ScrollMode.AUTO,
    )

    status_text = ft.Text("")

    def make_settings() -> AreaSettings:
        return AreaSettings(
            square_feet_per_marla=int(marla_dropdown.value or "272"),
            # kanals_per_acre=int(
            #     float(acre_dropdown.value or "8")
            # ) if float(acre_dropdown.value or "8").is_integer()
            # else 8,
            kanals_per_acre=int(acre_dropdown.value or "8"),
            kila_kanal=8,
            kila_marla=18,
            kila_feet=0,
            feet_per_karam=__import__("decimal").Decimal("5.5"),
        )

    def parse_area(value: str, input_format: str, settings: AreaSettings):
        """Convert the selected entry format into a LandArea."""

        if not value or not value.strip():
            raise AreaError("Please enter the required value.")

        value = value.strip()

        if input_format == "Kanal-Marla-Foot":
            return LandArea.parse(value, settings)

        if input_format == "Kanal-Marla-Sarsahi":
            parts = value.split("-")
            if len(parts) != 3:
                raise AreaError(
                    "Use Kanal-Marla-Sarsahi, for example 1-5-3."
                )
            try:
                return LandArea.from_kms(
                    int(parts[0].strip()),
                    int(parts[1].strip()),
                    int(parts[2].strip()),
                    settings,
                )
            except ValueError:
                raise AreaError(
                    "Kanal, marla and sarsahi must be whole numbers."
                ) from None

        if input_format == "Acres":
            return LandArea.from_acres(value, settings)

        if input_format == "Kila":
            return LandArea.from_kila(value, settings)

        if input_format == "Square Karam":
            return LandArea.from_square_karam(value, settings)

        raise AreaError(
            "Karam Dimensions requires a length and width in karam."
        )

    def format_result(area: LandArea, settings: AreaSettings) -> str:
        """Display the same result in multiple useful units."""

        return (
            f"Kanal-Marla-Foot: {area.format(settings)}\n"
            f"Kanal-Marla-Sarsahi: {area.format_sarsahi(settings)}\n"
            f"Square feet: {area.to_square_feet()}\n"
            f"Acres (selected convention): "
            f"{area.to_acres(settings):.6f}\n"
            f"Kila (8-18-0 convention): "
            f"{area.to_kila(settings):.6f}\n"
            f"Square karam: {area.to_square_karam(settings):.4f}"
        )

    def refresh_history():
        history_column.controls.clear()

        try:
            records = list_calculations(100)

            if not records:
                history_column.controls.append(
                    ft.Text("No saved calculations yet.")
                )
            else:
                for record in records:
                    title = (
                        f"#{record['id']} — {record['operation']}"
                    )

                    summary = (
                        f"{record['input_format']} | "
                        f"Result: {record['result']}"
                    )

                    def open_record(e, record_id=record["id"]):
                        selected = get_calculation(record_id)

                        if selected is None:
                            status_text.value = (
                                "This history record could not be found."
                            )
                        else:
                            result_text.value = selected["result"]
                            details_text.value = (
                                selected["details"]
                                + "\n\nSaved inputs:\n"
                                + str(selected["inputs"])
                                + "\n\nSaved settings:\n"
                                + str(selected["settings"])
                            )
                            status_text.value = (
                                f"Loaded saved calculation #{record_id}."
                            )

                        page.update()

                    history_column.controls.append(
                        ft.Container(
                            padding=10,
                            # border=ft.border.all(1, "#D0D0D0"),
                            border=ft.Border.all(1, "#D0D0D0"),
                            border_radius=8,
                            content=ft.Column(
                                controls=[
                                    ft.Text(title, weight=ft.FontWeight.BOLD),
                                    ft.Text(summary, selectable=True),
                                    ft.Button(
                                        "View details",
                                        on_click=open_record,
                                    ),
                                ],
                                spacing=5,
                            ),
                        )
                    )

        except Exception as exc:
            history_column.controls.append(
                ft.Text(f"Could not load history: {exc}")
            )

    def calculate(e=None):
        try:
            settings = make_settings()
            input_format = format_dropdown.value or INPUT_FORMATS[0]
            operation = operation_dropdown.value or "Convert"

            raw_inputs = {
                "first": first_input.value or "",
                "second": second_input.value or "",
                "third": third_input.value or "",
            }

            details = [
                f"Operation: {operation}",
                f"Input format: {input_format}",
                f"Square feet per marla: "
                f"{settings.square_feet_per_marla}",
                f"Kanals per acre: {settings.kanals_per_acre}",
                "Kila convention: "
                f"{settings.kila_kanal}-{settings.kila_marla}-"
                f"{settings.kila_feet}",
                "Karam length: 5.5 feet",
            ]

            # if operation == "Karam Dimensions":
            #     raise AreaError(
            #         "Select the Karam Dimensions input format, then "
            #         "use Convert. Enter length and width in karam."
            #     )

            if input_format == "Karam Dimensions":
                if operation != "Convert":
                    raise AreaError(
                        "Karam Dimensions currently supports conversion only."
                    )

                if not second_input.value:
                    raise AreaError(
                        "Enter length in the first field and width in the "
                        "second field, both in karam."
                    )

                area = LandArea.from_karam_dimensions(
                    first_input.value,
                    second_input.value,
                    settings,
                )

                details.append(
                    f"Area = length × width × 5.5 × 5.5\n"
                    f"= {first_input.value} × {second_input.value} "
                    f"× 30.25 square feet"
                )

            else:
                first = parse_area(
                    first_input.value or "",
                    input_format,
                    settings,
                )

                if operation == "Convert":
                    area = first
                    details.append(
                        f"Input converted to {first.to_square_feet()} square feet."
                    )

                elif operation == "Add":
                    second = parse_area(
                        second_input.value or "",
                        input_format,
                        settings,
                    )
                    area = sum_areas([first, second])
                    details.append(
                        f"{first.to_square_feet()} + "
                        f"{second.to_square_feet()} square feet"
                    )

                elif operation == "Subtract":
                    second = parse_area(
                        second_input.value or "",
                        input_format,
                        settings,
                    )
                    area = subtract_areas(first, second)
                    details.append(
                        f"{first.to_square_feet()} - "
                        f"{second.to_square_feet()} square feet"
                    )

                elif operation == "Average":
                    areas = [first]

                    if second_input.value and second_input.value.strip():
                        areas.append(
                            parse_area(
                                second_input.value,
                                input_format,
                                settings,
                            )
                        )

                    if third_input.value and third_input.value.strip():
                        areas.append(
                            parse_area(
                                third_input.value,
                                input_format,
                                settings,
                            )
                        )

                    if len(areas) < 2:
                        raise AreaError(
                            "Enter at least two areas to calculate an average."
                        )

                    area = average_area(areas)
                    details.append(
                        "Average square feet = total square feet ÷ "
                        f"{len(areas)} areas"
                    )

                elif operation == "Percentage Share":
                    if not second_input.value:
                        raise AreaError(
                            "Enter the percentage in the second field."
                        )

                    area = calculate_percentage_share(
                        first,
                        second_input.value,
                    )
                    details.append(
                        f"{second_input.value}% of "
                        f"{first.to_square_feet()} square feet"
                    )

                else:
                    raise AreaError("Select a supported operation.")

            result = format_result(area, settings)
            details_string = "\n".join(details)

            result_text.value = result
            details_text.value = details_string

            save_calculation(
                operation=operation,
                input_format=input_format,
                inputs=raw_inputs,
                settings={
                    "square_feet_per_marla": settings.square_feet_per_marla,
                    "marla_per_kanal": settings.marla_per_kanal,
                    "sarsahi_per_marla": settings.sarsahi_per_marla,
                    "kanals_per_acre": settings.kanals_per_acre,
                    "kila_kanal": settings.kila_kanal,
                    "kila_marla": settings.kila_marla,
                    "kila_feet": settings.kila_feet,
                    "feet_per_karam": str(settings.feet_per_karam),
                },
                result=result,
                details=details_string,
            )

            status_text.value = "Calculation completed and saved to history."
            refresh_history()

        except (AreaError, ValueError, TypeError) as exc:
            status_text.value = f"Please check your inputs: {exc}"

        except Exception as exc:
            traceback.print_exc()
            status_text.value = f"Unexpected error: {exc}"

        page.update()

    calculate_button.on_click = calculate

    refresh_button = ft.Button(
        "Refresh history",
        on_click=lambda e: refresh_history(),
    )

    refresh_history()

    return ft.Column(
        controls=[
            ft.Text("Land Area Calculator", size=24, weight=ft.FontWeight.BOLD),
            ft.Text(
                "Convert, calculate and save land-area calculations "
                "using your selected regional conventions."
            ),
            ft.Divider(),
            ft.Text("Regional conventions", size=18, weight=ft.FontWeight.BOLD),
            ft.Row(
                controls=[marla_dropdown, acre_dropdown],
                spacing=12,
            ),
            ft.Text(
                "Kila is configured as 8-18-0. Karam is 5.5 linear feet. "
                "An acre uses the selected kanal convention."
            ),
            ft.Divider(),
            ft.Text("Calculation", size=18, weight=ft.FontWeight.BOLD),
            ft.Row(
                controls=[format_dropdown, operation_dropdown],
                spacing=12,
            ),
            ft.Row(
                controls=[first_input, second_input],
                spacing=12,
            ),
            third_input,
            calculate_button,
            status_text,
            ft.Divider(),
            ft.Text("Result", size=18, weight=ft.FontWeight.BOLD),
            result_text,
            ft.Text("Calculation details", weight=ft.FontWeight.BOLD),
            details_text,
            ft.Divider(),
            ft.Row(
                controls=[
                    ft.Text(
                        "Calculation history",
                        size=18,
                        weight=ft.FontWeight.BOLD,
                    ),
                    refresh_button,
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            ),
            history_column,
        ],
        spacing=12,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
    )
