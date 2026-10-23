
from datetime import date
from decimal import Decimal, InvalidOperation

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
    get_client,
    get_custom_values,
    get_referrer_payment_totals,
    list_referrers,
    list_assets,
    list_clients,
    list_custom_fields,
    save_custom_values,
    update_client,
    update_client_status,
)
from core.security import generate_secure_password
from core.security import decrypt_secret


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
        label="Search name, CNIC, referral, or address",
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

    # Sort values are a small allowlist mapped by the service to SQL orderings.
    sort_filter = ft.Dropdown(
        label="Sort by",
        width=220,
        value="name",
        options=[
            ft.dropdown.Option("name", "Name"),
            ft.dropdown.Option("modified", "Recently modified"),
            ft.dropdown.Option("tehsil", "Tehsil, district, city"),
            ft.dropdown.Option("district", "District, tehsil, city"),
            ft.dropdown.Option("city", "City, district, tehsil"),
            ft.dropdown.Option("pending", "Highest pending payment"),
            ft.dropdown.Option("paid", "Paid / lowest balance first"),
        ],
    )
    sort_direction_filter = ft.Dropdown(
        label="Order",
        width=150,
        value="asc",
        options=[
            ft.dropdown.Option("asc", "Ascending"),
            ft.dropdown.Option("desc", "Descending"),
        ],
    )
    # Clipboard is a Flet service so credential text can be fetched and copied
    # only after the user clicks a card action, rather than exposed in the UI.
    clipboard = next(
        (service for service in getattr(page, "services", [])
         if isinstance(service, ft.Clipboard)),
        None,
    )
    if clipboard is None:
        clipboard = ft.Clipboard()
        # Services created outside an active Flet event context may not
        # self-register, so attach it to this page explicitly as a fallback.
        services = getattr(page, "services", None)
        if services is not None and clipboard not in services:
            services.append(clipboard)

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
                sort_by=sort_filter.value or "name",
                sort_direction=sort_direction_filter.value or "asc",
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
        status_colors = {
            "Active": ("#15803D", "#DCFCE7"),
            "Pending": ("#B91C1C", "#FEE2E2"),
            "Inactive": ("#C2410C", "#FFEDD5"),
            "Unspecified": ("#475569", "#F1F5F9"),
        }
        status_text_color, status_background = status_colors.get(
            status, status_colors["Unspecified"]
        )
        address_parts = [client.address, client.tehsil, client.district, client.city, client.province]
        full_address = ", ".join(part.strip() for part in address_parts if part and part.strip())

        # These are async event handlers themselves. Assigning a normal lambda
        # that merely returns a coroutine makes Flet treat it as synchronous,
        # so the copy request may never be awaited.
        async def copy_cnic(e):
            await copy_to_clipboard(client.cnic or "", "CNIC")

        async def copy_address(e):
            await copy_to_clipboard(full_address, "Address")

        async def copy_password(e):
            await copy_client_password(client.id)

        def save_card_status(e, client_id=client.id, previous=client.current_status):
            """Save this card's status immediately and restore it on failure."""
            selected_status = e.control.value or ""
            if selected_status == (previous or ""):
                return
            try:
                # Card selector -> focused service update -> committed client row.
                update_client_status(client_id, selected_status)
                refresh_clients()
                notify(page, f"{client.name}: status saved as {selected_status or 'Unspecified'}.")
            except Exception as exc:
                e.control.value = previous or ""
                page.update()
                notify(page, f"Could not update status: {exc}")

        status_options = [
            ft.dropdown.Option("", "Unspecified"),
            ft.dropdown.Option("Active"),
            ft.dropdown.Option("Pending"),
            ft.dropdown.Option("Inactive"),
        ]
        if client.current_status and client.current_status not in {
            "Active", "Pending", "Inactive"
        }:
            # Keep legacy values visible until a user selects a supported one.
            status_options.append(ft.dropdown.Option(client.current_status))
        status_selector = ft.Dropdown(
            value=client.current_status or "",
            options=status_options,
            width=155,
            dense=True,
            filled=True,
            fill_color=status_background,
            text_style=ft.TextStyle(color=status_text_color),
            text_size=12,
            content_padding=ft.Padding(left=10, right=6, top=4, bottom=4),
        )
        # This installed Flet release reports Dropdown selections via
        # on_select; TextField controls continue to use on_change.
        status_selector.on_select = save_card_status
        if client.payment_amount is None:
            payment_summary = "Payment details have not been entered."
        else:
            pending = max(
                client.payment_amount - (client.amount_received or 0), 0
            )
            payment_summary = (
                f"Business PKR {client.payment_amount:,.2f} · received "
                f"PKR {client.amount_received or 0:,.2f} · pending "
                f"PKR {pending:,.2f}"
            )

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
                            status_selector,
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
                        f"Address: {full_address or '—'}",
                        size=12,
                        color="#334155",
                    ),
                    ft.Text(
                        f"Referred by: {client.referred_by or '—'}",
                        size=12,
                        color="#64748B",
                    ),
                    ft.Text(
                        payment_summary,
                        size=12,
                        color="#334155",
                    ),
                    ft.Divider(height=1, color="#E2E8F0"),
                    ft.Row(
                        controls=[
                            ft.OutlinedButton(
                                "Copy CNIC",
                                icon=ft.Icons.CONTENT_COPY,
                                on_click=copy_cnic,
                                disabled=not bool(client.cnic),
                            ),
                            ft.OutlinedButton(
                                "Copy address",
                                icon=ft.Icons.CONTENT_COPY,
                                on_click=copy_address,
                                disabled=not bool(full_address),
                            ),
                            ft.OutlinedButton(
                                "Copy password",
                                icon=ft.Icons.CONTENT_COPY,
                                on_click=copy_password,
                                disabled=not bool(client.password_ciphertext),
                            ),
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

    async def copy_to_clipboard(value: str, label: str):
        if not value:
            notify(page, f"No {label.lower()} is saved for this client.")
            return
        try:
            await clipboard.set(value)
            notify(page, f"{label} copied to clipboard.")
        except Exception as exc:
            notify(page, f"Could not copy {label.lower()}: {exc}")

    async def copy_client_password(client_id: int):
        # Load and decrypt only when the user explicitly requests a copy.
        try:
            saved_client = get_client(client_id)
            password = decrypt_secret(
                saved_client.password_ciphertext if saved_client else None
            )
            if not password:
                notify(page, "No password is saved for this client.")
                return
            await clipboard.set(password)
            notify(page, "Password copied to clipboard.")
        except Exception as exc:
            notify(page, f"Could not copy password: {exc}")

    def open_client_form(client=None):
        editing = client is not None
        saved_custom_values = get_custom_values(client.id) if editing else {}
        # Column definitions come from the column manager; each control below
        # is keyed by its definition ID so the service can write the value FK.
        active_fields = list_custom_fields()

        def date_text(attribute):
            value = getattr(client, attribute, None) if editing else None
            if value:
                return value.isoformat()
            if not editing and attribute in {"registration_date", "status_updated_date"}:
                return date.today().isoformat()
            return ""

        def half_width(controls):
            for control in controls:
                control.col = {"sm": 6, "md": 6}
            return controls

        name_field = ft.TextField(label="Client name *", value=client.name if editing else "", autofocus=True)
        father_field = ft.TextField(label="Father name", value=(client.father_name or "") if editing else "")
        cnic_field = ft.TextField(label="CNIC number *", hint_text="00000-0000000-0", value=(client.cnic or "") if editing else "")
        cnic_expiry_field = ft.TextField(label="CNIC expiry date *", hint_text="YYYY-MM-DD", value=date_text("cnic_expiry_date"))
        address_field = ft.TextField(label="Street / postal address", value=(client.address or "") if editing else "", multiline=True, min_lines=2, max_lines=3)
        tehsil_field = ft.TextField(label="Tehsil", value=(client.tehsil or "") if editing else "")
        district_field = ft.TextField(label="District", value=(client.district or "") if editing else "")
        city_field = ft.TextField(label="City", value=(client.city or "") if editing else "")
        province_field = ft.TextField(label="Province", value=(client.province or "") if editing else "")
        birth_field = ft.TextField(label="Date of birth", hint_text="YYYY-MM-DD", value=date_text("date_of_birth"))
        registration_field = ft.TextField(label="Registration date", hint_text="YYYY-MM-DD", value=date_text("registration_date"))
        status_date_field = ft.TextField(label="Status update date", hint_text="YYYY-MM-DD", value=date_text("status_updated_date"))
        referrers = list_referrers()
        referrer_options = [ft.dropdown.Option(value) for value in referrers]
        if editing and client.referred_by and client.referred_by not in referrers:
            referrer_options.append(ft.dropdown.Option(client.referred_by))
        referral_field = ft.Dropdown(
            label="Referred by *",
            value=client.referred_by if editing else None,
            options=referrer_options,
            editable=True,
            enable_filter=True,
            enable_search=True,
        )

        def sync_referrer_text(e):
            # Typing should supersede an older selected option; Flet keeps
            # the typed string in `.text` and the selected key in `.value`.
            if referral_field.text:
                referral_field.value = None

        referral_field.on_text_change = sync_referrer_text
        phone_field = ft.TextField(label="Phone number *", value=(client.phone_number or "") if editing else "")
        email_field = ft.TextField(label="Email *", value=(client.email or "") if editing else "", keyboard_type=ft.KeyboardType.EMAIL)
        payment_field = ft.TextField(label="Total payment / business *", value=str(client.payment_amount or "") if editing else "", keyboard_type=ft.KeyboardType.NUMBER)
        received_field = ft.TextField(label="Amount received", value=str(client.amount_received or "0") if editing else "0", keyboard_type=ft.KeyboardType.NUMBER)
        payment_received = ft.Checkbox(
            label="Payment fully received",
            value=bool(editing and client.payment_amount is not None and client.amount_received is not None and client.amount_received >= client.payment_amount),
        )

        def sync_received_from_checkbox(e):
            received_field.value = payment_field.value if payment_received.value else "0"
            page.update()

        payment_received.on_change = sync_received_from_checkbox

        def sync_checkbox_from_amount(e):
            try:
                total = Decimal(payment_field.value or "0")
                paid = Decimal(received_field.value or "0")
                payment_received.value = total > 0 and paid >= total
                page.update()
            except (InvalidOperation, ValueError):
                payment_received.value = False
                page.update()

        payment_field.on_change = sync_checkbox_from_amount
        received_field.on_change = sync_checkbox_from_amount
        password_field = ft.TextField(
            label=(
                "New password (leave blank to keep current)"
                if editing and client.password_ciphertext
                else "Password *"
            ),
            password=True,
            can_reveal_password=True,
            value="",
            width=520,
            filled=False,
        )
        pin_field = ft.TextField(
            label="PIN (optional; blank keeps current PIN when editing)",
            password=True,
            can_reveal_password=True,
            value="",
        )
        password_length = ft.TextField(
            label="Length (12–64)",
            value="20",
            width=110,
            keyboard_type=ft.KeyboardType.NUMBER,
        )
        generated_password_info = ft.Text(
            "", size=11, color="#64748B"
        )
        generation_count = 0

        def generate_password(e):
            nonlocal generation_count
            try:
                length = int(password_length.value or "20")
                new_password = generate_secure_password(length)
                password_field.value = new_password
                error_text.value = ""
                generation_count += 1
                generated_password_info.value = (
                    f"Generated {length}-character password "
                    f"(generation {generation_count})."
                )
                page.update()
            except (ValueError, TypeError) as exc:
                generated_password_info.value = ""
                error_text.value = str(exc)
                page.update()

        credential_controls = ft.Column(
            controls=[
                password_field,
                ft.Row(
                    controls=[
                        password_length,
                        ft.Button(
                            "Generate password",
                            on_click=generate_password,
                        ),
                    ],
                    spacing=8,
                ),
                generated_password_info,
                pin_field,
            ],
            spacing=8,
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

        def update_status_date(e):
            status_date_field.value = date.today().isoformat()
            page.update()

        status_field.on_select = update_status_date

        error_text = ft.Text("", color="#B91C1C", size=12)

        custom_controls = []
        custom_value_controls = {}
        for field in active_fields:
            initial = saved_custom_values.get(field.id, "")
            if field.data_type == "boolean":
                control = ft.Dropdown(
                    label=f"{field.label}{' *' if field.is_required else ''}",
                    value=initial or None,
                    options=[ft.dropdown.Option("true", "Yes"), ft.dropdown.Option("false", "No")],
                )
            else:
                control = ft.TextField(
                    label=f"{field.label}{' *' if field.is_required else ''}",
                    value=initial,
                    multiline=field.data_type == "long_text",
                    keyboard_type=(ft.KeyboardType.NUMBER if field.data_type in {"integer", "decimal"} else None),
                    hint_text="YYYY-MM-DD" if field.data_type == "date" else None,
                )
            custom_controls.append(control)
            custom_value_controls[field.id] = control

        def collect_form_data():
            # The form's normal fields map to Client columns; custom controls
            # travel as a field-ID map and save in the same service transaction.
            return {
                "name": name_field.value or "",
                "father_name": father_field.value or "",
                "cnic": cnic_field.value or "",
                "cnic_expiry_date": cnic_expiry_field.value or "",
                "address": address_field.value or "",
                "tehsil": tehsil_field.value or "",
                "district": district_field.value or "",
                "city": city_field.value or "",
                "province": province_field.value or "",
                "date_of_birth": birth_field.value or "",
                "registration_date": registration_field.value or "",
                "status_updated_date": status_date_field.value or "",
                "phone_number": phone_field.value or "",
                "email": email_field.value or "",
                "payment_amount": payment_field.value or "",
                "amount_received": received_field.value or "0",
                "current_status": status_field.value or "",
                # Editable Dropdown stores typed text separately from a
                # selected option, so accept either representation.
                "referred_by": referral_field.text or referral_field.value or "",
                "password": password_field.value or "",
                "pin": pin_field.value or "",
                "custom_values": {
                    field_id: control.value or ""
                    for field_id, control in custom_value_controls.items()
                },
            }

        def save(e):
            # Form values -> create_client/update_client -> committed DB row;
            # refresh_clients then reads that row back into the card list.
            try:
                error_text.value = ""
                data = collect_form_data()

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
                        error_text,
                        ft.Text("Identity and referral", weight=ft.FontWeight.BOLD),
                        ft.ResponsiveRow(controls=half_width([
                            name_field, father_field, cnic_field,
                            cnic_expiry_field, birth_field, referral_field,
                        ]), run_spacing=4),
                        ft.Text("Address", weight=ft.FontWeight.BOLD),
                        address_field,
                        ft.ResponsiveRow(controls=half_width([
                            tehsil_field, district_field, city_field, province_field,
                        ]), run_spacing=4),
                        ft.Text("Contact and status", weight=ft.FontWeight.BOLD),
                        ft.ResponsiveRow(controls=half_width([
                            phone_field, email_field, registration_field,
                            status_field, status_date_field,
                        ]), run_spacing=4),
                        ft.Text("Payment", weight=ft.FontWeight.BOLD),
                        ft.ResponsiveRow(controls=half_width([payment_field, received_field]), run_spacing=4),
                        payment_received,
                        ft.Text("Portal credentials", weight=ft.FontWeight.BOLD),
                        credential_controls,
                        *([ft.Text("Additional fields", weight=ft.FontWeight.BOLD), *custom_controls] if custom_controls else []),
                    ],
                    scroll=ft.ScrollMode.AUTO,
                    spacing=12,
                ),
                width=760,
                height=600,
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

    def open_referrer_totals(e=None, *, pending_only=False):
        """Show referrer totals, optionally limiting the table to balances due."""
        filter_field = ft.TextField(
            label="Find a referrer",
            prefix_icon=getattr(ft.Icons, "SEARCH", None),
        )
        rows = ft.Column(spacing=4, scroll=ft.ScrollMode.AUTO, expand=True)
        dialog_mounted = False

        def refresh_totals(event=None):
            query = (filter_field.value or "").strip().casefold()
            rows.controls.clear()
            rows.controls.append(
                ft.Row(
                    controls=[
                        ft.Text("Referrer", weight=ft.FontWeight.BOLD, expand=2),
                        ft.Text("Clients", weight=ft.FontWeight.BOLD, expand=1),
                        ft.Text("Business", weight=ft.FontWeight.BOLD, expand=1),
                        ft.Text("Received", weight=ft.FontWeight.BOLD, expand=1),
                        ft.Text("Pending", weight=ft.FontWeight.BOLD, expand=1),
                    ],
                    spacing=8,
                )
            )
            # This service query aggregates each referrer's client payment
            # amounts; the search narrows the result rows in this dialog.
            totals = get_referrer_payment_totals()
            matches = [
                item for item in totals
                if query in item["referrer"].casefold()
                and (not pending_only or item["pending"] > 0)
            ]
            for item in matches:
                rows.controls.append(
                    ft.Row(
                        controls=[
                            ft.Text(item["referrer"], expand=2),
                            ft.Text(str(item["clients"]), expand=1),
                            ft.Text(f"PKR {item['business']:,.2f}", expand=1),
                            ft.Text(f"PKR {item['received']:,.2f}", expand=1),
                            ft.Text(f"PKR {item['pending']:,.2f}", expand=1, color="#B91C1C" if item["pending"] else "#15803D"),
                        ],
                        spacing=8,
                    )
                )
            if not matches:
                empty_message = (
                    "No referrers have pending payments."
                    if pending_only and not query
                    else "No matching referrers with pending payments."
                    if pending_only
                    else "No matching referral records."
                )
                rows.controls.append(ft.Text(empty_message))
            if dialog_mounted:
                rows.update()

        filter_field.on_change = refresh_totals
        refresh_totals()
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(
                "Referrers with pending payments"
                if pending_only
                else "Referrer business and pending payments"
            ),
            content=ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Text(
                            "Showing only referrers whose combined client balances are still due."
                            if pending_only
                            else "Business is total client payment; pending is business minus received. "
                            "Legacy clients without payment data show zero in these totals until updated."
                        ),
                        filter_field,
                        rows,
                    ],
                    expand=True,
                ),
                width=850,
                height=520,
            ),
            actions=[text_button("Close", on_click=lambda event: close_dialog(page, dialog))],
        )
        open_dialog(page, dialog)
        dialog_mounted = True

    def open_pending_referrers(e=None):
        """Open the existing totals report filtered to positive balances."""
        open_referrer_totals(e, pending_only=True)

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

    # Keep handlers explicit so every user input change runs the query and
    # updates the client list immediately; Refresh remains available for a
    # manual reload after external database changes.
    def apply_search_change(e):
        refresh_clients()

    def apply_filter_change(e):
        refresh_clients()

    search_field.on_change = apply_search_change
    status_filter.on_select = apply_filter_change
    sort_filter.on_select = apply_filter_change
    sort_direction_filter.on_select = apply_filter_change

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
            sort_filter,
            sort_direction_filter,
            ft.OutlinedButton(
                "Refresh",
                on_click=refresh_clients,
            ),
            ft.OutlinedButton(
                "Manage columns",
                on_click=open_column_manager,
            ),
            ft.OutlinedButton(
                "Referrer totals",
                on_click=open_referrer_totals,
            ),
            ft.OutlinedButton(
                "Pending referrers",
                on_click=open_pending_referrers,
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
