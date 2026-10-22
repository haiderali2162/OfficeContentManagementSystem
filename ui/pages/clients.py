
import flet as ft
from core.config import DATABASE_PATH
from ui.flet_compat import text_button

from services.client_service import (
    ClientServiceError,
    create_asset,
    create_client,
    create_custom_field,
    delete_client,
    delete_custom_field,
    get_custom_values,
    list_assets,
    list_clients,
    list_custom_fields,
    save_custom_values,
    update_client,
)


def open_dialog(page: ft.Page, dialog: ft.AlertDialog) -> None:
    """Show a form dialog using the API provided by the installed Flet."""
    if hasattr(page, "show_dialog"):
        page.show_dialog(dialog)
    else:
        page.dialog = dialog
        dialog.open = True
        page.update()


def close_dialog(page: ft.Page, dialog: ft.AlertDialog) -> None:
    # Flet 1.x tracks open dialogs as a stack; popping it also removes the
    # dialog from that stack so the next CRUD form can open normally.
    if hasattr(page, "pop_dialog"):
        page.pop_dialog()
    elif hasattr(page, "close"):
        page.close(dialog)
    else:
        dialog.open = False
        page.update()


def notify(page: ft.Page, message: str) -> None:
    """Display a brief notification."""
    snackbar = ft.SnackBar(content=ft.Text(message))

    # In Flet 1.x SnackBar is displayed through show_dialog as well.
    if hasattr(page, "show_dialog"):
        page.show_dialog(snackbar)
    elif hasattr(page, "open"):
        page.open(snackbar)
    else:
        page.snack_bar = snackbar
        snackbar.open = True
        page.update()


def build_clients_page(page: ft.Page) -> ft.Control:
    """Build the client screen and connect its controls to client services.

    UI events call services.client_service; that service reads/writes the
    SQLAlchemy models, which core.database stores in DATABASE_PATH.
    """
    # search field for the page
    search_field = ft.TextField(
        label="Search name, CNIC, father name, or referral",
        prefix_icon=getattr(ft.Icons, "SEARCH", None),
        # A fixed search width lets the status and action buttons stay visible
        # and wrap cleanly when the application window is narrow.
        width=320,
    )

    # creating a dropdown list
    status_filter = ft.Dropdown(
        label="Status",
        width=190,
        value="All statuses",
        options=[
            ft.dropdown.Option("All statuses"),
            ft.dropdown.Option("Active"),
            ft.dropdown.Option("Inactive"),
            ft.dropdown.Option("Pending"),
        ],
    )

    # This flexing ListView uses the remaining page height and scrolls
    # independently, while the title, filters, and actions remain in view.
    clients_container = ft.ListView(
        spacing=10,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
        padding=ft.Padding(left=2, right=8, top=2, bottom=8),
    )

    result_label = ft.Text(
        "Loading clients...",
        size=12,
        color="#64748B",
    )

    def refresh_clients(e=None, *, update_page=True):
        # Search/status controls -> list_clients() -> SQLAlchemy/SQLite.
        # The result cards below are the visible read side of that same flow.
        clients_container.controls.clear()

        try:
            clients = list_clients(
                search=search_field.value or "",
                status=status_filter.value or "",
            )
            result_label.value = f"{len(clients)} client(s) found"

            if not clients:
                clients_container.controls.append(
                    ft.Container(
                        content=ft.Column(
                            controls=[
                                ft.Text(
                                    "No clients found",
                                    size=18,
                                    weight=ft.FontWeight.BOLD,
                                ),
                                ft.Text(
                                    "Add a client or change the search "
                                    "and status filters.",
                                    color="#64748B",
                                ),
                                ft.Text(
                                    f"Database: {DATABASE_PATH}",
                                    size=11,
                                    color="#94A3B8",
                                    selectable=True,
                                ),
                            ],
                            horizontal_alignment=(
                                ft.CrossAxisAlignment.CENTER
                            ),
                            spacing=8,
                        ),
                        padding=30,
                        alignment=ft.Alignment(0, 0),
                    )
                )
            else:
                # Each model from the read query becomes one client card.
                for client in clients:
                    clients_container.controls.append(
                        build_client_card(client)
                    )
        # Catch both query failures and rendering failures, and show which
        # database file this running app tried to read.
        except Exception as exc:
            clients_container.controls.clear()
            result_label.value = "Unable to load clients"
            # Don't leave a partially rendered result on screen.
            clients_container.controls.append(
                ft.Text(
                    f"Unable to load clients from {DATABASE_PATH}: {exc}",
                    color="#B91C1C",
                    selectable=True,
                )
            )

        # Page.update sends the rebuilt list controls to the running client.
        # On initial page construction AppShell mounts this control tree and
        # performs the first page update. Event-driven refreshes update now.
        if update_page:
            page.update()

    # Each card is built from a Client model returned by list_clients(); its
    # buttons pass that model's id to the matching service operation.
    def build_client_card(client):
        # Card buttons below hand the selected model/id to update, delete,
        # asset, or custom-field service functions.
        status = client.current_status or "Unspecified"

        return ft.Container(
            content=ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Column(
                                controls=[
                                    ft.Text(
                                        client.name,
                                        size=17,
                                        weight=ft.FontWeight.BOLD,
                                    ),
                                    ft.Text(
                                        f"Father: "
                                        f"{client.father_name or '—'}",
                                        size=12,
                                        color="#64748B",
                                    ),
                                ],
                                spacing=4,
                                expand=True,
                            ),
                            ft.Container(
                                content=ft.Text(
                                    status,
                                    size=11,
                                    color="#2457C5",
                                ),
                                padding=8,
                                bgcolor="#EAF0FF",
                                border_radius=8,
                            ),
                        ],
                        vertical_alignment=(
                            ft.CrossAxisAlignment.CENTER
                        ),
                    ),

                    ft.Text(
                        f"CNIC: {client.cnic or '—'}",
                        size=13,
                    ),
                    ft.Text(
                        f"Referred by: {client.referred_by or '—'}",
                        size=12,
                        color="#64748B",
                    ),
                    ft.Divider(height=1, color="#E2E8F0"),
                    ft.Row(
                        controls=[
                            text_button(
                                "Custom data",
                                on_click=lambda e, cid=client.id:
                                    open_custom_values(cid),
                            ),
                            text_button(
                                "Assets",
                                on_click=lambda e, cid=client.id:
                                    open_assets(cid),
                            ),
                            text_button(
                                "Edit",
                                on_click=lambda e, c=client:
                                    open_client_form(c),
                            ),
                            text_button(
                                "Delete",
                                on_click=lambda e, c=client:
                                    confirm_delete(c),
                            ),
                        ],
                        wrap=True,
                        spacing=4,
                    ),
                ],
                spacing=10,
            ),
            padding=16,
            bgcolor="#FFFFFF",
            border=ft.Border.all(1, "#E2E8F0"),
            border_radius=12,
        )

    def open_client_form(client=None):
        editing = client is not None

        name_field = ft.TextField(
            label="Client name *",
            value=client.name if editing else "",
            autofocus=True,
        )
        father_field = ft.TextField(
            label="Father name",
            value=(client.father_name or "") if editing else "",
        )
        cnic_field = ft.TextField(
            label="CNIC (00000-0000000-0)",
            value=(client.cnic or "") if editing else "",
        )
        referral_field = ft.TextField(
            label="Referred by",
            value=(client.referred_by or "") if editing else "",
        )
        status_field = ft.Dropdown(
            label="Current status",
            value=(
                client.current_status
                if editing and client.current_status in {
                    "Active", "Inactive", "Pending"
                }
                else "Active"
            ),
            options=[
                ft.dropdown.Option("Active"),
                ft.dropdown.Option("Inactive"),
                ft.dropdown.Option("Pending"),
            ],
        )

        error_text = ft.Text("", color="#B91C1C", size=12)

        def save(e):
            # Form values -> create_client/update_client -> committed DB row;
            # refresh_clients then reads that row back into the card list.
            try:
                data = {
                    "name": name_field.value or "",
                    "father_name": father_field.value or "",
                    "cnic": cnic_field.value or "",
                    "current_status": status_field.value or "",
                    "referred_by": referral_field.value or "",
                }

                if editing:
                    update_client(client.id, **data)
                    message = "Client updated successfully."
                else:
                    create_client(**data)
                    message = "Client added successfully."

                # Refresh from the committed database record first. This
                # keeps the list current even if the dialog lifecycle reports
                # an error after the save has already succeeded.
                refresh_clients()
                close_dialog(page, dialog)
                notify(page, message)

            except ClientServiceError as exc:
                error_text.value = str(exc)
                page.update()
            except Exception as exc:
                error_text.value = (
                    f"Could not save the record: {exc}"
                )
                page.update()

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(
                "Edit client" if editing else "Add new client"
            ),
            content=ft.Container(
                content=ft.Column(
                    controls=[
                        name_field,
                        father_field,
                        cnic_field,
                        status_field,
                        referral_field,
                        error_text,
                    ],
                    tight=True,
                    scroll=ft.ScrollMode.AUTO,
                    spacing=12,
                ),
                width=440,
            ),
            actions=[
                text_button(
                    "Cancel",
                    on_click=lambda e: close_dialog(page, dialog),
                ),
                # ft.Button(
                ft.Button(
                    "Save client",
                    on_click=save,
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )

        open_dialog(page, dialog)

    def confirm_delete(client):
        message = ft.Text(
            f"Delete {client.name}? This will also remove "
            "the client's associated assets and custom values."
        )

        def perform_delete(e):
            try:
                delete_client(client.id)
                close_dialog(page, dialog)
                refresh_clients()
                notify(page, "Client deleted.")
            except ClientServiceError as exc:
                close_dialog(page, dialog)
                notify(page, str(exc))
            except Exception:
                close_dialog(page, dialog)
                notify(page, "Unable to delete this client.")

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Confirm deletion"),
            content=message,
            actions=[
                text_button(
                    "Cancel",
                    on_click=lambda e: close_dialog(page, dialog),
                ),
                # ft.Button(
                ft.Button(
                    "Delete",
                    on_click=perform_delete,
                ),
            ],
        )

        open_dialog(page, dialog)

    def open_custom_values(client_id: int):
        fields = list_custom_fields()
        current_values = get_custom_values(client_id)

        if not fields:
            notify(
                page,
                "Create custom columns using Manage columns first.",
            )
            return

        controls = []
        value_controls = {}

        for field in fields:
            initial_value = current_values.get(field.id, "")

            if field.data_type == "boolean":
                control = ft.Dropdown(
                    label=field.label,
                    value=initial_value or "false",
                    options=[
                        ft.dropdown.Option("true", "Yes"),
                        ft.dropdown.Option("false", "No"),
                    ],
                )
            else:
                keyboard = None
                if field.data_type in {"integer", "decimal"}:
                    keyboard = ft.KeyboardType.NUMBER

                control = ft.TextField(
                    label=field.label,
                    value=initial_value,
                    keyboard_type=keyboard,
                    multiline=field.data_type == "long_text",
                )

            controls.append(control)
            value_controls[field.id] = control

        error_text = ft.Text("", color="#B91C1C", size=12)

        def save_values(e):
            try:
                values = {
                    field_id: control.value or ""
                    for field_id, control in value_controls.items()
                }

                save_custom_values(client_id, values)
                close_dialog(page, dialog)
                notify(page, "Custom values saved.")
            except ClientServiceError as exc:
                error_text.value = str(exc)
                page.update()

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Custom client data"),
            content=ft.Container(
                content=ft.Column(
                    controls=controls + [error_text],
                    tight=True,
                    scroll=ft.ScrollMode.AUTO,
                    spacing=12,
                ),
                width=440,
                height=400,
            ),
            actions=[
                text_button(
                    "Cancel",
                    on_click=lambda e: close_dialog(page, dialog),
                ),
                ft.Button(
                    "Save values",
                    on_click=save_values,
                ),
            ],
        )

        open_dialog(page, dialog)

    def open_assets(client_id: int):
        assets = list_assets(client_id)

        asset_type = ft.TextField(label="Asset type *")
        description = ft.TextField(
            label="Description",
            multiline=True,
        )
        valuation = ft.TextField(
            label="Valuation (PKR)",
            keyboard_type=ft.KeyboardType.NUMBER,
        )
        error_text = ft.Text("", color="#B91C1C", size=12)

        asset_rows = [
            ft.ListTile(
                title=ft.Text(asset.asset_type),
                subtitle=ft.Text(
                    f"{asset.description or ''} "
                    f"| Value: "
                    f"{asset.valuation_amount if asset.valuation_amount is not None else '—'}"
                ),
            )
            for asset in assets
        ]

        def save_asset(e):
            from decimal import Decimal, InvalidOperation

            try:
                amount = None
                if (valuation.value or "").strip():
                    amount = Decimal(valuation.value)

                create_asset(
                    client_id=client_id,
                    asset_type=asset_type.value or "",
                    description=description.value or "",
                    valuation_amount=amount,
                )

                close_dialog(page, dialog)
                notify(page, "Asset added.")
                open_assets(client_id)

            except (ClientServiceError, InvalidOperation, ValueError) as exc:
                error_text.value = str(exc)
                page.update()

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Client assets"),
            content=ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Text(
                            "Existing assets",
                            weight=ft.FontWeight.BOLD,
                        ),
                        *asset_rows,
                        ft.Divider(),
                        ft.Text(
                            "Add an asset",
                            weight=ft.FontWeight.BOLD,
                        ),
                        asset_type,
                        description,
                        valuation,
                        error_text,
                    ],
                    scroll=ft.ScrollMode.AUTO,
                    tight=True,
                    spacing=10,
                ),
                width=480,
                height=440,
            ),
            actions=[
                text_button(
                    "Close",
                    on_click=lambda e: close_dialog(page, dialog),
                ),
                ft.Button(
                    "Add asset",
                    on_click=save_asset,
                ),
            ],
        )

        open_dialog(page, dialog)

    def open_column_manager(e=None):
        fields_container = ft.Column(
            spacing=8,
            scroll=ft.ScrollMode.AUTO,
        )

        label_field = ft.TextField(label="Column heading")
        key_field = ft.TextField(
            label="Internal key (e.g. annual_income)"
        )
        type_field = ft.Dropdown(
            label="Data type",
            value="text",
            options=[
                ft.dropdown.Option("text"),
                ft.dropdown.Option("long_text"),
                ft.dropdown.Option("integer"),
                ft.dropdown.Option("decimal"),
                ft.dropdown.Option("date"),
                ft.dropdown.Option("boolean"),
                ft.dropdown.Option("choice"),
            ],
        )
        required_field = ft.Checkbox(
            label="Required field",
            value=False,
        )
        error_text = ft.Text("", color="#B91C1C", size=12)

        def refresh_fields():
            fields_container.controls.clear()

            for field in list_custom_fields():
                fields_container.controls.append(
                    ft.Row(
                        controls=[
                            ft.Column(
                                controls=[
                                    ft.Text(
                                        field.label,
                                        weight=ft.FontWeight.BOLD,
                                    ),
                                    ft.Text(
                                        f"{field.key} · {field.data_type}",
                                        size=11,
                                        color="#64748B",
                                    ),
                                ],
                                spacing=3,
                                expand=True,
                            ),
                            ft.IconButton(
                                ft.Icons.DELETE_OUTLINE,
                                tooltip="Deactivate column",
                                on_click=lambda e, fid=field.id:
                                    deactivate_field(fid),
                            ),
                        ]
                    )
                )

        def deactivate_field(field_id):
            try:
                delete_custom_field(field_id)
                refresh_fields()
                notify(
                    page,
                    "Column deactivated. Existing values are preserved.",
                )
            except ClientServiceError as exc:
                error_text.value = str(exc)
                page.update()

        def add_field(e):
            try:
                create_custom_field(
                    key=key_field.value or "",
                    label=label_field.value or "",
                    data_type=type_field.value or "text",
                    is_required=bool(required_field.value),
                )

                label_field.value = ""
                key_field.value = ""
                required_field.value = False
                error_text.value = ""
                refresh_fields()
                page.update()
                notify(page, "Custom column created.")
            except ClientServiceError as exc:
                error_text.value = str(exc)
                page.update()

        refresh_fields()

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Manage custom columns"),
            content=ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Text(
                            "Existing columns",
                            weight=ft.FontWeight.BOLD,
                        ),
                        fields_container,
                        ft.Divider(),
                        ft.Text(
                            "Create a column",
                            weight=ft.FontWeight.BOLD,
                        ),
                        label_field,
                        key_field,
                        type_field,
                        required_field,
                        error_text,
                    ],
                    scroll=ft.ScrollMode.AUTO,
                    tight=True,
                    spacing=10,
                ),
                width=480,
                height=500,
            ),
            actions=[
                text_button(
                    "Close",
                    on_click=lambda e: (
                        close_dialog(page, dialog),
                        refresh_clients(),
                    ),
                ),
                ft.Button(
                    "Add column",
                    on_click=add_field,
                ),
            ],
        )

        open_dialog(page, dialog)

    search_field.on_change = refresh_clients
    status_filter.on_change = refresh_clients

    header = ft.Row(
        controls=[
            ft.Column(
                controls=[
                    ft.Text(
                        "FBR Clients",
                        size=25,
                        weight=ft.FontWeight.BOLD,
                    ),
                    ft.Text(
                        "Manage client information and associated assets.",
                        color="#64748B",
                    ),
                ],
                spacing=5,
                expand=True,
            ),
            ft.Button(
                "Add client",
                # icon=getattr(ft.Icons, "PERSON_ADD", None),
                icon=ft.Icons.PERSON_ADD,
                on_click=lambda e: open_client_form(),
            ),
        ],
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )

    toolbar = ft.Row(
        controls=[
            search_field,
            status_filter,
            ft.OutlinedButton(
                "Refresh",
                on_click=refresh_clients,
            ),
            ft.OutlinedButton(
                "Manage columns",
                on_click=open_column_manager,
            ),
        ],
        spacing=10,
        wrap=True,
    )

    # Navigation calls this builder each time FBR Clients opens, so every
    # visit starts with a new database query and current client records.
    refresh_clients(update_page=False)

    return ft.Column(
        controls=[
            header,
            toolbar,
            result_label,
            clients_container,
        ],
        spacing=16,
        expand=True,
    )
