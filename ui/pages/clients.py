
import asyncio
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

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
    update_asset,
)
from core.security import generate_secure_password
from services.client_copy_service import (
    client_copy_field_available,
    client_copy_field_label,
    client_copy_value,
    copy_button_label,
    decrypted_password_for_copy,
)
from services.client_import_service import (
    CLIENT_FIELD_LABELS,
    REQUIREMENT_DEFAULTS,
    ClientImportError,
    apply_saved_column_swap,
    build_client_import_preview,
    export_clients,
    import_clients,
    preview_saved_column_swap,
    read_client_table,
    suggest_client_column_mapping,
    swap_import_row_fields,
)
from services.client_import_service import (
    SWAPPABLE_CLIENT_FIELDS as _SWAPPABLE_FIELDS,
)
from services.settings_service import get_client_copy_fields


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

    v0.2.30 cards hard-coded CNIC/address/password copy actions. v0.2.31
    builds those buttons from the saved built-in fields in Settings, adding
    email, phone, and every other stable client column. Custom client columns
    remain under the existing column manager.
    """
    # v0.2.31: read the preference once so every card in this list shows the
    # same buttons; a Settings change takes effect on the next page refresh.
    client_copy_fields = get_client_copy_fields()

    # search field for the page
    search_field = ft.TextField(
        label="Search name, CNIC, NTN, referral, or address",
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

        # v0.2.31: one factory-made async handler per configured field. A
        # factory is required because a lambda returning a coroutine is treated
        # as synchronous by Flet, so the clipboard write may never be awaited.
        def make_copy_handler(field: str):
            async def copy_selected_field(_):
                if field == "password":
                    # Never store or render plaintext: reload and decrypt only
                    # after this explicit button click.
                    await copy_client_password(client.id)
                    return
                value = client_copy_value(client, field)
                label = client_copy_field_label(field)
                if not value:
                    notify(page, f"No {label.lower()} is saved for this client.")
                    return
                await copy_to_clipboard(value, label)
            return copy_selected_field

        copy_buttons = [
            ft.OutlinedButton(
                copy_button_label(field),
                icon=ft.Icons.CONTENT_COPY,
                data=f"copy_client_field:{field}",
                on_click=make_copy_handler(field),
                disabled=not client_copy_field_available(client, field),
            )
            for field in client_copy_fields
        ]

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
                        f"CNIC: {client.cnic or '—'}  ·  "
                        f"NTN: {client.ntn_number or '—'}",
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
                            *copy_buttons,
                            text_button(
                                "Custom data",
                                on_click=lambda e, cid=client.id:
                                    open_custom_values(cid),
                            ),
                            text_button(
                                "Tax assets",
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
        # v0.2.31: the service reloads and decrypts only on this explicit click;
        # the card list itself still contains ciphertext only.
        try:
            password = decrypted_password_for_copy(client_id)
        except Exception as exc:
            notify(page, f"Could not copy password: {exc}")
            return
        await copy_to_clipboard(password, "Password")

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
        cnic_field = ft.TextField(label="CNIC number (optional)", hint_text="00000-0000000-0", value=(client.cnic or "") if editing else "")
        ntn_field = ft.TextField(label="NTN number (optional)", value=(client.ntn_number or "") if editing else "")
        cnic_expiry_field = ft.TextField(label="CNIC expiry date (required when CNIC is entered)", hint_text="YYYY-MM-DD", value=date_text("cnic_expiry_date"))
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
                "ntn_number": ntn_field.value or "",
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
                            name_field, father_field, cnic_field, ntn_field,
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

    def open_asset_form(client_id: int, asset=None):
        """Create or edit one tax asset and persist it through the service."""
        editing = asset is not None
        category_field = ft.Dropdown(
            label="Record category *",
            value=(asset.category if editing and asset.category else "Other"),
            options=[
                ft.dropdown.Option("Property"),
                ft.dropdown.Option("Motor vehicle"),
                ft.dropdown.Option("Income source"),
                ft.dropdown.Option("Other"),
            ],
        )
        saved_fiscal_year = (
            asset.fiscal_year if editing and asset.fiscal_year else ""
        )
        saved_tax_year = (
            str(int(saved_fiscal_year[:4]) + 1)
            if len(saved_fiscal_year) == 7
            and saved_fiscal_year[:4].isdecimal()
            else ""
        )
        tax_year_field = ft.TextField(
            label="Tax year (ending year)",
            hint_text="For example: 2026",
            keyboard_type=ft.KeyboardType.NUMBER,
            value=saved_tax_year,
        )
        fiscal_year_field = ft.TextField(
            label="Fiscal year *",
            hint_text="For example: 2025-26",
            value=saved_fiscal_year,
        )

        def derive_fiscal_year(e):
            """Map a tax year to the fiscal year ending in that year."""
            raw_year = (tax_year_field.value or "").strip()
            if len(raw_year) == 4 and raw_year.isdecimal():
                year = int(raw_year)
                fiscal_year_field.value = (
                    f"{year - 1}-{year % 100:02d}"
                    if 1901 <= year <= 9999
                    else ""
                )
            else:
                fiscal_year_field.value = ""
            page.update()

        def sync_tax_year_from_fiscal(e):
            """Keep the ending tax year in sync with a manually entered FY."""
            value = (fiscal_year_field.value or "").strip()
            parts = value.split("-")
            if (
                len(parts) == 2
                and len(parts[0]) == 4
                and len(parts[1]) == 2
                and parts[0].isdecimal()
                and parts[1].isdecimal()
            ):
                tax_year_field.value = str(int(parts[0]) + 1)
            else:
                tax_year_field.value = ""
            page.update()

        tax_year_field.on_change = derive_fiscal_year
        fiscal_year_field.on_change = sync_tax_year_from_fiscal
        asset_type_field = ft.TextField(
            label="Asset or source name *",
            hint_text="House, plot, car, employer, business, etc.",
            value=asset.asset_type if editing else "",
        )
        location_field = ft.TextField(
            label="Property location / address",
            value=(asset.location or "") if editing else "",
            multiline=True,
        )
        make_model_field = ft.TextField(
            label="Vehicle make and model",
            value=(asset.make_model or "") if editing else "",
        )
        registration_field = ft.TextField(
            label="Vehicle registration number",
            value=(asset.registration_number or "") if editing else "",
        )
        purchase_date_field = ft.TextField(
            label="Purchase date (YYYY-MM-DD)",
            value=(asset.purchase_date.isoformat() if editing and asset.purchase_date else ""),
        )
        sale_date_field = ft.TextField(
            label="Sale date (YYYY-MM-DD)",
            value=(asset.sale_date.isoformat() if editing and asset.sale_date else ""),
        )
        purchase_price_field = ft.TextField(
            label="Purchase price (PKR)",
            keyboard_type=ft.KeyboardType.NUMBER,
            value=str(asset.purchase_price or "") if editing else "",
        )
        sale_price_field = ft.TextField(
            label="Sale price (PKR)",
            keyboard_type=ft.KeyboardType.NUMBER,
            value=str(asset.sale_price or "") if editing else "",
        )
        income_source_field = ft.TextField(
            label="Income source type",
            hint_text="Salary, business, rent, agriculture, investment, etc.",
            value=(asset.income_source_type or "") if editing else "",
        )
        annual_income_field = ft.TextField(
            label="Annual income from this source (PKR)",
            keyboard_type=ft.KeyboardType.NUMBER,
            value=str(asset.annual_income or "") if editing else "",
        )
        current_value_field = ft.TextField(
            label="Current value (PKR)",
            keyboard_type=ft.KeyboardType.NUMBER,
            value=str(asset.valuation_amount or "") if editing else "",
        )
        description_field = ft.TextField(
            label="Additional details",
            multiline=True,
            value=(asset.description or "") if editing else "",
        )
        error_text = ft.Text("", color="#B91C1C", size=12)

        ownership_group = ft.Column(
            controls=[
                ft.Text("Purchase and sale history", weight=ft.FontWeight.BOLD),
                ft.ResponsiveRow(controls=[
                    purchase_date_field, sale_date_field,
                    purchase_price_field, sale_price_field,
                ]),
            ],
            spacing=8,
        )
        property_group = ft.Column(controls=[location_field])
        vehicle_group = ft.Column(controls=[make_model_field, registration_field])
        income_group = ft.Column(
            controls=[
                ft.Text("Income source", weight=ft.FontWeight.BOLD),
                income_source_field,
                annual_income_field,
            ],
            spacing=8,
        )

        def update_category_fields(e=None):
            """Show only fields relevant to the selected record category."""
            category = category_field.value or "Other"
            ownership_group.visible = category != "Income source"
            property_group.visible = category == "Property"
            vehicle_group.visible = category == "Motor vehicle"
            income_group.visible = category == "Income source"
            if e is not None:
                page.update()

        category_field.on_select = update_category_fields
        update_category_fields()

        def save_asset(e):
            try:
                data = {
                    "client_id": client_id,
                    "category": category_field.value or "Other",
                    "fiscal_year": fiscal_year_field.value or "",
                    "tax_year": tax_year_field.value or "",
                    "asset_type": asset_type_field.value or "",
                    "description": description_field.value or "",
                    "location": location_field.value or "",
                    "make_model": make_model_field.value or "",
                    "registration_number": registration_field.value or "",
                    "purchase_date": purchase_date_field.value or "",
                    "sale_date": sale_date_field.value or "",
                    "purchase_price": purchase_price_field.value or None,
                    "sale_price": sale_price_field.value or None,
                    "income_source_type": income_source_field.value or "",
                    "annual_income": annual_income_field.value or None,
                    "valuation_amount": current_value_field.value or None,
                }
                if editing:
                    update_asset(asset_id=asset.id, **data)
                else:
                    create_asset(**data)

                close_dialog(page, dialog)
                notify(page, "Asset details saved.")
                open_assets(client_id)
            except ClientServiceError as exc:
                error_text.value = str(exc)
                page.update()

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Edit tax record" if editing else "Add tax record"),
            content=ft.Container(
                content=ft.Column(
                    controls=[
                        error_text,
                        ft.Text(
                            "Record category separates property, vehicles, "
                            "income sources, and other tax information."
                        ),
                        category_field,
                        ft.Text("Tax year selection", weight=ft.FontWeight.BOLD),
                        tax_year_field,
                        fiscal_year_field,
                        asset_type_field,
                        property_group,
                        vehicle_group,
                        ownership_group,
                        income_group,
                        current_value_field,
                        description_field,
                    ],
                    scroll=ft.ScrollMode.AUTO,
                    spacing=10,
                ),
                width=650,
                height=600,
            ),
            actions=[
                text_button("Cancel", on_click=lambda event: close_dialog(page, dialog)),
                ft.Button("Save changes" if editing else "Add record", on_click=save_asset),
            ],
        )
        open_dialog(page, dialog)

    def open_assets(client_id: int):
        """Show one client's tax assets and provide per-record edit actions."""
        assets = list_assets(client_id)
        asset_rows = []
        for asset in assets:
            summary = [f"Fiscal year: {asset.fiscal_year or 'Not recorded'}"]
            if asset.location:
                summary.append(f"Location: {asset.location}")
            if asset.make_model:
                summary.append(f"Vehicle: {asset.make_model}")
            if asset.registration_number:
                summary.append(f"Registration: {asset.registration_number}")
            if asset.purchase_date:
                summary.append(f"Purchased: {asset.purchase_date.isoformat()}")
            if asset.sale_date:
                summary.append(f"Sold: {asset.sale_date.isoformat()}")
            if asset.purchase_price is not None:
                summary.append(f"Purchase PKR {asset.purchase_price:,.2f}")
            if asset.sale_price is not None:
                summary.append(f"Sale PKR {asset.sale_price:,.2f}")
            if asset.income_source_type:
                summary.append(f"Income: {asset.income_source_type}")
            if asset.annual_income is not None:
                summary.append(f"Annual PKR {asset.annual_income:,.2f}")
            if asset.valuation_amount is not None:
                summary.append(f"Current value PKR {asset.valuation_amount:,.2f}")
            if asset.description:
                summary.append(asset.description)

            asset_rows.append(
                ft.Row(
                    controls=[
                        ft.Column(
                            controls=[
                                ft.Text(
                                    f"{asset.category or 'Other'} · {asset.asset_type}",
                                    weight=ft.FontWeight.BOLD,
                                ),
                                ft.Text("  ·  ".join(summary) or "No extra details"),
                            ],
                            spacing=4,
                            expand=True,
                        ),
                        ft.IconButton(
                            ft.Icons.EDIT_OUTLINED,
                            tooltip="Edit this tax record",
                            on_click=lambda event, record=asset: (
                                close_dialog(page, dialog),
                                open_asset_form(client_id, record),
                            ),
                        ),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                )
            )

        if not asset_rows:
            asset_rows.append(ft.Text("No tax assets or income sources have been recorded."))

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Client tax assets and income"),
            content=ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Text(
                            "Properties, vehicles, income sources, and their "
                            "purchase/sale details for this client."
                        ),
                        ft.Divider(),
                        *asset_rows,
                    ],
                    scroll=ft.ScrollMode.AUTO,
                    spacing=10,
                ),
                width=760,
                height=520,
            ),
            actions=[
                text_button("Close", on_click=lambda event: close_dialog(page, dialog)),
                ft.Button(
                    "Add asset or income source",
                    on_click=lambda event: (
                        close_dialog(page, dialog),
                        open_asset_form(client_id),
                    ),
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

    def open_export_clients_dialog(e=None):
        """Export clients to CSV/XLSX/ODS (passwords are never exported)."""
        export_picker = ft.FilePicker()
        if not any(
            isinstance(service, ft.FilePicker) for service in page.services
        ):
            page.services.append(export_picker)
        else:
            export_picker = next(
                service for service in page.services
                if isinstance(service, ft.FilePicker)
            )
        format_dropdown = ft.Dropdown(
            label="File format", width=160, value="xlsx",
            options=[
                ft.dropdown.Option("xlsx", "Excel (.xlsx)"),
                ft.dropdown.Option("csv", "CSV (.csv)"),
                ft.dropdown.Option("ods", "OpenDocument (.ods)"),
            ],
        )
        scope_dropdown = ft.Dropdown(
            label="Which clients", width=260, value="filtered",
            options=[
                ft.dropdown.Option(
                    "filtered", "Only the currently filtered clients"),
                ft.dropdown.Option("all", "All clients"),
            ],
        )
        export_status = ft.Text("", size=12, color="#475569")

        async def run_export(_=None):
            file_format = format_dropdown.value or "xlsx"
            try:
                if (scope_dropdown.value or "filtered") == "filtered":
                    records = list_clients(
                        search=search_field.value or "",
                        status=status_filter.value or "",
                        sort_by=sort_filter.value or "name",
                        sort_direction=(
                            sort_direction_filter.value or "asc"
                        ),
                    )
                else:
                    records = list_clients()
                if not records:
                    export_status.value = "No clients to export."
                    export_status.color = "#B45309"
                    page.update()
                    return
                payload = await asyncio.to_thread(
                    export_clients, records, file_format,
                )
            except Exception as exc:
                export_status.value = f"Could not build the export: {exc}"
                export_status.color = "#B91C1C"
                page.update()
                return
            stamp = date.today().isoformat()
            try:
                destination = await export_picker.save_file(
                    dialog_title="Save the client export",
                    file_name=f"clients_{stamp}.{file_format}",
                    allowed_extensions=[file_format],
                    src_bytes=payload,
                )
            except Exception as exc:
                export_status.value = f"Could not save the file: {exc}"
                export_status.color = "#B91C1C"
                page.update()
                return
            export_status.value = (
                f"Exported {len(records)} client(s) to {destination}."
                if destination
                else "Export prepared; save dialog cancelled."
            )
            export_status.color = "#15803D"
            page.update()

        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("Export clients"),
            content=ft.Column([
                ft.Text(
                    "Exports the client columns exactly as stored (CSV, "
                    "Excel, or OpenDocument). Passwords and PINs are "
                    "never included.",
                    size=11, color="#64748B",
                ),
                ft.Row([format_dropdown, scope_dropdown], wrap=True,
                       spacing=8),
                export_status,
            ], spacing=8, tight=True),
            actions=[
                ft.TextButton("Close", on_click=lambda _: page.pop_dialog()),
                ft.Button("Export", on_click=run_export),
            ],
        ))

    def open_import_clients_dialog(e=None):
        """Import clients from a spreadsheet, CSV/ODS file, or a paste.

        Google Sheets exports (xlsx/csv/ods) and direct copies (tab-
        separated paste) both work; columns are auto-mapped with manual
        override, and a default password (stored encrypted like manual
        entries) is applied to the imported rows (developer requests
        2026-11-20).
        """
        import_file_picker = ft.FilePicker()
        if not any(
            isinstance(service, ft.FilePicker) for service in page.services
        ):
            page.services.append(import_file_picker)
        else:
            import_file_picker = next(
                service for service in page.services
                if isinstance(service, ft.FilePicker)
            )
        paste_field = ft.TextField(
            label="Paste client rows (copy from the Google Sheet: one header row + data)",
            multiline=True, min_lines=3, max_lines=5, expand=True,
        )
        file_label = ft.Text("", size=11, color="#64748B")
        load_status = ft.Text("", size=11, color="#64748B")
        mapping_row = ft.Row(wrap=True, spacing=8)
        password_field = ft.TextField(
            label="Default password (optional — rows with a Password column use their own)",
            value="",
            hint_text="Leave empty when the file already has passwords",
            width=420,
            password=True,
            can_reveal_password=True,
        )
        preview_summary = ft.Text("", size=12, color="#475569")
        preview_table = ft.Column(spacing=4, scroll=ft.ScrollMode.AUTO)
        state: dict[str, Any] = {"headers": [], "rows": [], "mapping": {}}

        async def load_file(_=None):
            try:
                result = await import_file_picker.pick_files(
                    dialog_title="Open the client spreadsheet",
                    allowed_extensions=["xlsx", "xls", "ods", "csv"],
                    allow_multiple=False,
                    with_data=True,
                )
            except Exception as exc:
                load_status.value = f"Could not open the file picker: {exc}"
                load_status.color = "#B91C1C"
                page.update()
                return
            if not result:
                return
            # flet 1.0.4 returns the chosen files as a plain list here;
            # older shapes returned a result object with ``files``.
            chosen_files = getattr(result, "files", result)
            if not chosen_files:
                return
            chosen = chosen_files[0]
            try:
                headers, rows = read_client_table(
                    file_bytes=chosen.bytes, filename=chosen.name,
                )
            except (ClientImportError, Exception) as exc:
                load_status.value = f"Could not read the file: {exc}"
                load_status.color = "#B91C1C"
                page.update()
                return
            state["headers"], state["rows"] = headers, rows
            state["mapping"] = suggest_client_column_mapping(headers)
            file_label.value = f"File loaded: {chosen.name} ({len(rows)} rows)"
            load_status.value = (
                f"{len(rows)} data row(s) loaded from {len(headers)} "
                "columns. Check the mapping, then build the preview."
            )
            load_status.color = "#166534"
            mapping_row.controls = [_render_mapping()]
            page.update()

        def load_paste(_=None):
            try:
                headers, rows = read_client_table(
                    pasted_text=paste_field.value or "",
                )
            except ClientImportError as exc:
                load_status.value = str(exc)
                load_status.color = "#B91C1C"
                page.update()
                return
            except Exception as exc:
                load_status.value = f"Could not read the pasted table: {exc}"
                load_status.color = "#B91C1C"
                page.update()
                return
            state["headers"], state["rows"] = headers, rows
            state["mapping"] = suggest_client_column_mapping(headers)
            file_label.value = ""
            load_status.value = (
                f"{len(rows)} data row(s) loaded from the paste. Check the "
                "mapping, then build the preview."
            )
            load_status.color = "#166534"
            mapping_row.controls = [_render_mapping()]
            page.update()

        def _render_mapping() -> ft.Control:
            controls: list[ft.Control] = []
            for field, label in CLIENT_FIELD_LABELS.items():
                options = [ft.dropdown.Option(key="-1", text="(not mapped)")]
                for index, header in enumerate(state["headers"]):
                    options.append(ft.dropdown.Option(
                        key=str(index), text=f"{index + 1}. {header}"[:50],
                    ))
                dropdown = ft.Dropdown(
                    label=label, width=250, options=options,
                    value=str(state["mapping"].get(field))
                    if state["mapping"].get(field) is not None else "-1",
                )

                def on_map_change(_e, field=field, dropdown=dropdown):
                    value = int(dropdown.value)
                    state["mapping"][field] = value if value >= 0 else None

                dropdown.on_change = on_map_change
                controls.append(dropdown)
            return ft.Row(controls, wrap=True, spacing=8)

        # Shared preview rendering: the simple checklist or the grid swap
        # view (developer request 2026-11-20: select columns and rows to
        # start swapping from).
        selected_columns: set[str] = set()
        selected_rows: set[int] = set()
        grid_state: dict[str, int] = {"limit": 40}

        def _revalidate_preview(rotated_values: list[dict[str, str]],
                                row_numbers: list[int]) -> None:
            """Re-run the real validation after a swap, keeping row numbers."""
            state["preview"] = build_client_import_preview(
                state["headers"],
                [
                    [values.get(field, "") for field in CLIENT_FIELD_LABELS]
                    for values in rotated_values
                ],
                {
                    field: index for index, field
                    in enumerate(CLIENT_FIELD_LABELS)
                },
                default_password=password_field.value or "",
                required=current_requirements(),
                split_names=split_names.value,
            )
            for row, number in zip(state["preview"]["rows"], row_numbers):
                row["row_number"] = number

        def _simple_preview_rows(preview: dict[str, Any]) -> list[ft.Control]:
            return [
                ft.Container(
                    ft.Row([
                        ft.Text(
                            f"Row {row['row_number']}", size=11, width=60,
                        ),
                        ft.Text(
                            f"{row['values'].get('name') or '(no name)'} · "
                            f"{row['values'].get('cnic') or row['values'].get('ntn_number') or '—'} · "
                            f"{row['values'].get('phone_number') or '—'}",
                            size=11, expand=True,
                        ),
                        ft.Text(
                            row["problem"] or "ready", size=11,
                            color="#B91C1C" if row["problem"] else "#166534",
                            width=340,
                        ),
                        *([ft.TextButton(
                            "Fix",
                            on_click=lambda _, r=row: fix_row_dialog(
                                r, render_preview_body,
                            ),
                        )] if row["problem"] else []),
                    ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    padding=4,
                )
                for row in preview["rows"][:60]
            ] + ([ft.Text("… older rows appear on import.", size=11,
                          color="#64748B")]
                 if len(preview["rows"]) > 60 else [])

        def _toggle_row_selection(e, number: int, checkbox: ft.Checkbox):
            if checkbox.value:
                selected_rows.add(number)
            else:
                selected_rows.discard(number)

        def _toggle_column_selection(e, field: str, checkbox: ft.Checkbox):
            if checkbox.value:
                selected_columns.add(field)
            else:
                selected_columns.discard(field)

        def clear_grid_selection(_=None):
            selected_columns.clear()
            selected_rows.clear()
            render_preview_body()

        def _revalidate_single_row(row: dict[str, Any]) -> dict[str, Any]:
            """Re-validate one row with the current settings, in place."""
            fresh = build_client_import_preview(
                state["headers"],
                [[row["values"].get(field, "") for field in CLIENT_FIELD_LABELS]],
                {field: index for index, field in enumerate(CLIENT_FIELD_LABELS)},
                default_password=password_field.value or "",
                required=current_requirements(),
                split_names=split_names.value,
            )["rows"][0]
            row["values"] = fresh["values"]
            row["problem"] = fresh["problem"]
            row["notes"] = fresh["notes"]
            return row

        def fix_row_dialog(row: dict[str, Any], after_fix: Callable[[], None]):
            """Edit one problem row's values, re-validating on save."""
            error_text = ft.Text("", size=11, color="#B91C1C")
            field_controls: dict[str, ft.TextField] = {}
            for field, label in CLIENT_FIELD_LABELS.items():
                field_controls[field] = ft.TextField(
                    label=label, value=str(row["values"].get(field) or ""),
                    width=280,
                )

            def do_fix(_=None):
                row["values"] = {
                    field: control.value.strip() if control.value else ""
                    for field, control in field_controls.items()
                }
                _revalidate_single_row(row)
                if row["problem"]:
                    error_text.value = (
                        f"Still flagged: {row['problem']} — adjust and "
                        "save again, or Cancel."
                    )
                    page.update()
                    return
                page.pop_dialog()
                preview_summary.value = (
                    f"Row {row['row_number']} fixed and ready to import."
                )
                after_fix()

            page.show_dialog(ft.AlertDialog(
                modal=True,
                title=ft.Text(f"Fix row {row['row_number']}"),
                content=ft.Column([
                    ft.Text(
                        "Current problem: "
                        + (row["problem"] or "none"),
                        size=11, color="#B91C1C",
                    ),
                    ft.Column(
                        list(field_controls.values()),
                        spacing=6, scroll=ft.ScrollMode.AUTO, height=340,
                    ),
                    error_text,
                ], spacing=8),
                actions=[
                    ft.TextButton("Cancel", on_click=lambda _: page.pop_dialog()),
                    ft.Button("Save row", on_click=do_fix),
                ],
            ))

        def export_problem_rows(_=None):
            """Write every flagged row to a new Excel file for fixing."""
            preview = state.get("preview")
            if not preview:
                load_status.value = "Build the preview first."
                load_status.color = "#B91C1C"
                page.update()
                return
            problem_rows = [
                row for row in preview["rows"] if row["problem"]
            ]
            if not problem_rows:
                load_status.value = "No problem rows — everything is ready."
                load_status.color = "#166534"
                page.update()
                return
            from services.dc_rate_service import _export_simple_table

            headers = ["Source row", *CLIENT_FIELD_LABELS.values(), "Problems"]
            export_rows = [
                [row["row_number"], *(
                    row["values"].get(field, "")
                    for field in CLIENT_FIELD_LABELS
                ), row["problem"]]
                for row in problem_rows
            ]
            try:
                payload = _export_simple_table(
                    headers, export_rows, "xlsx", "Client import problems",
                )
            except Exception as exc:
                load_status.value = f"Could not build the Excel file: {exc}"
                load_status.color = "#B91C1C"
                page.update()
                return
            from datetime import date as _date

            async def save_problems():
                try:
                    destination = await import_file_picker.save_file(
                        dialog_title="Save the problem rows",
                        file_name=(
                            "client_import_problems_"
                            f"{_date.today().isoformat()}.xlsx"
                        ),
                        allowed_extensions=["xlsx"],
                        src_bytes=payload,
                    )
                except Exception as exc:
                    load_status.value = f"Could not save the file: {exc}"
                    load_status.color = "#B91C1C"
                    page.update()
                    return
                load_status.value = (
                    f"{len(problem_rows)} problem row(s) exported to "
                    f"{destination}." if destination
                    else "Save dialog cancelled."
                )
                load_status.color = "#475569"
                page.update()

            if hasattr(page, "run_task"):
                page.run_task(save_problems)
            else:
                load_status.value = (
                    "Export problem rows works in the running application."
                )
                page.update()

        def swap_grid_selection(_=None):
            """Rotate the selected columns' values in the selected rows."""
            preview = state.get("preview")
            if not preview:
                load_status.value = "Build the preview first."
                load_status.color = "#B91C1C"
                page.update()
                return
            if len(selected_columns) < 2:
                load_status.value = (
                    "Tick at least two column headers to swap."
                )
                load_status.color = "#B91C1C"
                page.update()
                return
            if not selected_rows:
                load_status.value = (
                    "Tick at least one row to start swapping from."
                )
                load_status.color = "#B91C1C"
                page.update()
                return
            fields_in_order = [
                field for field in CLIENT_FIELD_LABELS
                if field in selected_columns
            ]
            updated = 0
            rotated_values: list[dict[str, str]] = []
            row_numbers: list[int] = []
            for row in preview["rows"]:
                if row["row_number"] not in selected_rows:
                    rotated_values.append(row["values"])
                    row_numbers.append(row["row_number"])
                    continue
                try:
                    rotated_values.append(
                        swap_import_row_fields(row["values"], fields_in_order)
                    )
                except ClientImportError as exc:
                    load_status.value = str(exc)
                    load_status.color = "#B91C1C"
                    page.update()
                    return
                row_numbers.append(row["row_number"])
                updated += 1
            _revalidate_preview(rotated_values, row_numbers)
            preview_summary.value = (
                f"Swapped {len(selected_columns)} column(s) in {updated} "
                "selected row(s). Review, then import."
            )
            render_preview_body()

        def render_preview_body():
            preview = state.get("preview")
            if not preview:
                preview_table.controls = []
                return
            if not grid_mode.value:
                preview_table.controls = _simple_preview_rows(preview)
                page.update()
                return
            rows_shown = preview["rows"][: grid_state["limit"]]
            column_controls: list[ft.Control] = [
                ft.Text("No.", size=11, weight=ft.FontWeight.BOLD),
            ]
            for field, label in CLIENT_FIELD_LABELS.items():
                header_checkbox = ft.Checkbox(
                    value=field in selected_columns, scale=0.8,
                )
                header_checkbox.on_change = lambda e, f=field, c=header_checkbox: (
                    _toggle_column_selection(e, f, c)
                )
                column_controls.append(ft.Row([
                    header_checkbox,
                    ft.Text(label.split(" (")[0], size=10),
                ], spacing=2, vertical_alignment=ft.CrossAxisAlignment.CENTER))
            data_columns = [
                ft.DataColumn(ft.Container(control, width=95))
                for control in column_controls
            ]
            data_rows = []
            for row in rows_shown:
                row_checkbox = ft.Checkbox(
                    value=row["row_number"] in selected_rows, scale=0.8,
                )
                row_checkbox.on_change = lambda e, n=row["row_number"], c=row_checkbox: (
                    _toggle_row_selection(e, n, c)
                )
                cells = [
                    ft.DataCell(ft.Container(ft.Text(
                        f"{row['row_number']}"
                        + (" ⚠" if row["problem"] else ""),
                        size=10,
                    ), width=30)),
                ]
                for field in CLIENT_FIELD_LABELS:
                    value = str(row["values"].get(field) or "")
                    cells.append(ft.DataCell(ft.Container(
                        ft.Text(value[:26], size=10,
                                color="#B91C1C" if row["problem"] else None,
                                tooltip=value or None),
                        width=95,
                    )))
                if row["problem"]:
                    cells.append(ft.DataCell(ft.TextButton(
                        "Fix",
                        on_click=lambda _, r=row: fix_row_dialog(
                            r, render_preview_body,
                        ),
                    )))
                data_rows.append(ft.DataRow(cells=[
                    ft.DataCell(row_checkbox), *cells,
                ]))
            preview_table.controls = [
                ft.Row([
                    ft.OutlinedButton(
                        "Swap selected columns in selected rows",
                        on_click=swap_grid_selection,
                    ),
                    ft.TextButton("Clear selection",
                                  on_click=clear_grid_selection),
                    *([ft.TextButton(
                        "Show 40 more rows",
                        on_click=lambda _: _show_more_grid(),
                    )] if len(preview["rows"]) > grid_state["limit"] else []),
                ], wrap=True, spacing=8),
                ft.Text(
                    "Tick the column headers and the rows to include in the "
                    "swap; values rotate within the selection. ⚠ marks a "
                    "row with a validation problem.",
                    size=10, color="#64748B",
                ),
                ft.Row([
                    ft.DataTable(
                        columns=data_columns, rows=data_rows,
                        column_spacing=6, heading_row_height=34,
                        data_row_height=30,
                    ),
                ], scroll=ft.ScrollMode.AUTO),
            ]
            page.update()

        def _show_more_grid():
            grid_state["limit"] += 40
            render_preview_body()

        def build_preview(_=None):
            try:
                preview = build_client_import_preview(
                    state["headers"], state["rows"], state["mapping"],
                    default_password=password_field.value or "",
                    required=current_requirements(),
                    split_names=split_names.value,
                )
            except ClientImportError as exc:
                load_status.value = str(exc)
                load_status.color = "#B91C1C"
                page.update()
                return
            state["preview"] = preview
            preview_summary.value = (
                f"{preview['ready']} row(s) ready to import, "
                f"{len(preview['rows']) - preview['ready']} with problems."
            )
            render_preview_body()

        def run_import(_=None):
            preview = state.get("preview")
            if not preview:
                load_status.value = "Build the preview first."
                load_status.color = "#B91C1C"
                page.update()
                return
            ready_rows = [row for row in preview["rows"] if not row["problem"]]

            def do_import(_=None):
                try:
                    summary = import_clients(
                        ready_rows,
                        default_password=password_field.value or "",
                        required=current_requirements(),
                    )
                except ClientImportError as exc:
                    # Close both the confirm dialog and the import dialog
                    # so the error is actually visible.
                    page.pop_dialog()
                    page.pop_dialog()
                    notify(page, f"Import failed: {exc}")
                    refresh_clients()
                    return
                except Exception as exc:
                    page.pop_dialog()
                    page.pop_dialog()
                    notify(page, f"Import failed unexpectedly: {exc}")
                    refresh_clients()
                    return
                # Close BOTH the confirm dialog and the import dialog —
                # otherwise the summary snackbar renders behind the still
                # open import dialog and the refreshed list stays hidden
                # (developer report 2026-11-21).
                page.pop_dialog()
                page.pop_dialog()
                errors = "; ".join(summary["errors"][:3]) if summary["errors"] else ""
                notify(page, (
                    f"Import finished: {summary['created']} client(s) "
                    f"created, {summary['skipped']} row(s) skipped."
                    + (f" Issues: {errors}" if errors else "")
                ))
                refresh_clients()

            if not ready_rows:
                load_status.value = "No importable rows — fix the problems first."
                load_status.color = "#B91C1C"
                page.update()
                return
            page.show_dialog(ft.AlertDialog(
                modal=True,
                title=ft.Text(f"Import {len(ready_rows)} client(s)?"),
                content=ft.Text(
                    "Rows with a Password column use their own password; "
                    "the others get the default password (stored "
                    "encrypted). Rows with problems are skipped.",
                    size=13,
                ),
                actions=[
                    ft.TextButton("Cancel", on_click=lambda _: page.pop_dialog()),
                    ft.Button("Import", on_click=do_import),
                ],
            ))

        def swap_row_fields(_=None):
            """Swap/rotate values between chosen fields in every preview row."""
            choices = [swap_a.value, swap_b.value, swap_c.value]
            picked = [name for name in choices if name and name != "none"]
            preview = state.get("preview")
            if not preview:
                load_status.value = "Build the preview first."
                load_status.color = "#B91C1C"
                page.update()
                return
            try:
                rotated_rows = [
                    {**row,
                     "values": swap_import_row_fields(row["values"], picked)}
                    for row in preview["rows"]
                ]
            except ClientImportError as exc:
                load_status.value = str(exc)
                load_status.color = "#B91C1C"
                page.update()
                return
            # Re-validate after the swap so problems stay accurate.
            _revalidate_preview(
                [rotated["values"] for rotated in rotated_rows],
                [rotated["row_number"] for rotated in rotated_rows],
            )
            preview_summary.value = (
                f"Values swapped between {len(picked)} field(s) for all "
                "preview rows. Review, then import."
            )
            render_preview_body()

        field_options = [
            ft.dropdown.Option(key, label)
            for key, label in CLIENT_FIELD_LABELS.items()
        ]
        swap_a = ft.Dropdown(label="Swap field 1", width=240,
                             options=field_options)
        swap_b = ft.Dropdown(label="Swap field 2", width=240,
                             options=field_options)
        swap_c = ft.Dropdown(
            label="Field 3 (optional, 3-way rotate)", width=240,
            options=[ft.dropdown.Option(key="none", text="(not used)"),
                     *field_options],
            value="none",
        )
        grid_mode = ft.Checkbox(
            label="Grid swap view (select columns and rows to start swapping from)",
            value=False,
            on_change=lambda _: render_preview_body(),
        )
        split_names = ft.Checkbox(
            label="Split name & parentage (usman ali so haider ali / fatima do mushtaq / rashida wid majid)",
            value=True,
        )
        requirement_checks: dict[str, ft.Checkbox] = {}
        for field, default_on in REQUIREMENT_DEFAULTS.items():
            checkbox = ft.Checkbox(
                label=CLIENT_FIELD_LABELS[field].split(" (")[0],
                value=default_on,
            )
            requirement_checks[field] = checkbox

        def current_requirements() -> dict[str, bool]:
            return {
                field: bool(checkbox.value)
                for field, checkbox in requirement_checks.items()
            }
        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("Import clients"),
            content=ft.Column([
                ft.Text(
                    "Source: paste rows copied from a Google Sheet, or load "
                    "an exported .xlsx / .xls / .ods / .csv file. Columns "
                    "are detected automatically; fix any mapping below. "
                    "Wrong-column mistakes can be corrected before "
                    "importing with the swap fields tool or the grid view.",
                    size=11, color="#64748B",
                ),
                paste_field,
                ft.Row([
                    ft.OutlinedButton("Load pasted rows", on_click=load_paste),
                    ft.OutlinedButton("Load file…", on_click=load_file),
                    file_label,
                ], wrap=True, spacing=8,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
                load_status,
                ft.Container(mapping_row),
                ft.Column([
                    ft.Text(
                        "Mandatory fields (everything is optional by "
                        "default; tick a field to flag rows that leave "
                        "it empty):",
                        size=11, color="#64748B",
                    ),
                    ft.Row(
                        [requirement_checks[field]
                         for field in REQUIREMENT_DEFAULTS],
                        wrap=True, spacing=10,
                    ),
                    ft.Text(
                        "Rows without any password import with no stored "
                        "credentials, and an empty name cell imports as "
                        "\"(unnamed — row N)\" so the row is never lost.",
                        size=11, color="#64748B",
                    ),
                    split_names,
                ], spacing=4),
                ft.Row([swap_a, swap_b, swap_c,
                        ft.OutlinedButton("Swap these fields in all rows",
                                          on_click=swap_row_fields)],
                       wrap=True, spacing=8,
                       vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Row([grid_mode, password_field], wrap=True, spacing=16,
                       vertical_alignment=ft.CrossAxisAlignment.CENTER),
                preview_summary,
                ft.Row([
                    ft.OutlinedButton(
                        "Export problem rows (Excel)",
                        on_click=export_problem_rows,
                        tooltip="Save a new Excel file containing only the "
                        "flagged rows with their problems",
                    ),
                ], wrap=True, spacing=8),
                preview_table,
            ], spacing=8, scroll=ft.ScrollMode.AUTO),
            actions=[
                ft.TextButton("Close", on_click=lambda _: page.pop_dialog()),
                ft.OutlinedButton("Build preview", on_click=build_preview),
                ft.Button("Import ready rows", on_click=run_import),
            ],
        ))

    def open_saved_column_swap_dialog(e=None):
        """Swap/rotate saved values between two or three client columns."""
        field_keys = list(_SWAPPABLE_FIELDS)
        field_options = [
            ft.dropdown.Option(key, label)
            for key, label in _SWAPPABLE_FIELDS.items()
        ]
        field_a = ft.Dropdown(label="Column A", width=230,
                              options=field_options, value="city")
        field_b = ft.Dropdown(label="Column B", width=230,
                              options=field_options, value="tehsil")
        field_c = ft.Dropdown(
            label="Column C (optional, 3-way rotate)", width=230,
            options=[ft.dropdown.Option(key="none", text="(not used)"),
                     *field_options],
            value="none",
        )
        swap_status = ft.Text("", size=12, color="#475569")
        swap_preview = ft.Column(spacing=4, scroll=ft.ScrollMode.AUTO)

        def _picked():
            picked = [field_a.value, field_b.value, field_c.value]
            return [name for name in picked if name and name != "none"]

        def build_swap_preview(_=None):
            try:
                preview = preview_saved_column_swap(_picked())
            except ClientImportError as exc:
                swap_status.value = str(exc)
                swap_status.color = "#B91C1C"
                page.update()
                return
            swap_preview.controls = [
                ft.Container(
                    ft.Column([
                        ft.Text(item["client_name"], size=12,
                                weight=ft.FontWeight.W_600),
                        *[
                            ft.Text(
                                f"{change['label']}: "
                                f"'{change['before'] or '—'}' → "
                                f"'{change['after'] or '—'}'",
                                size=11, color="#64748B",
                            )
                            for change in item["changes"]
                        ],
                    ], spacing=2),
                    padding=6,
                    bgcolor="#F8FAFC",
                    border=ft.Border.all(1, "#E2E8F0"),
                    border_radius=6,
                )
                for item in preview["affected"][:40]
            ] + ([ft.Text("… and more.", size=11, color="#64748B")]
                 if preview["count"] > 40 else [])
            swap_status.value = (
                f"{preview['count']} client(s) would change. Review, then "
                "apply." if preview["count"]
                else "No client changes — the chosen columns hold the same "
                "values everywhere."
            )
            swap_status.color = "#475569"
            page.update()

        def apply_swap(_=None):
            picked = _picked()

            def do_apply(_=None):
                try:
                    summary = apply_saved_column_swap(picked)
                except ClientImportError as exc:
                    page.pop_dialog()
                    swap_status.value = str(exc)
                    swap_status.color = "#B91C1C"
                    page.update()
                    return
                page.pop_dialog()
                swap_status.value = (
                    f"Swapped saved values for {summary['updated']} "
                    "client(s)."
                )
                swap_status.color = "#15803D"
                refresh_clients()

            page.show_dialog(ft.AlertDialog(
                modal=True,
                title=ft.Text("Swap the saved column values?"),
                content=ft.Text(
                    "This rewrites the values in the database for every "
                    "listed client. A backup taken before this change is "
                    "recommended (Settings → Automatic backups → Back up "
                    "now).", size=13,
                ),
                actions=[
                    ft.TextButton("Cancel", on_click=lambda _: page.pop_dialog()),
                    ft.Button("Swap values", on_click=do_apply),
                ],
            ))

        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("Swap saved column data"),
            content=ft.Column([
                ft.Text(
                    "Fix rows where information was typed into the wrong "
                    "column: two columns exchange their values, three "
                    "columns rotate (A → B → C → A). Only clients whose "
                    "values actually differ are changed.",
                    size=11, color="#64748B",
                ),
                ft.Row([field_a, field_b, field_c], wrap=True, spacing=8),
                ft.Row([
                    ft.OutlinedButton("Preview changes",
                                      on_click=build_swap_preview),
                    ft.Button("Apply swap", on_click=apply_swap),
                ], wrap=True, spacing=8),
                swap_status,
                swap_preview,
            ], spacing=8, scroll=ft.ScrollMode.AUTO),
            actions=[ft.TextButton("Close", on_click=lambda _: page.pop_dialog())],
        ))

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
            ft.OutlinedButton(
                "Import clients",
                on_click=open_import_clients_dialog,
            ),
            ft.OutlinedButton(
                "Export clients",
                on_click=open_export_clients_dialog,
            ),
            ft.OutlinedButton(
                "Swap saved columns",
                on_click=open_saved_column_swap_dialog,
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
