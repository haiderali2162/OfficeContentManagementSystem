
import asyncio
import sys

import flet as ft

from core.config import APP_NAME, APP_VERSION
from core.database import (
    check_database_connection,
    initialize_database,
)
from ui.app_shell import AppShell


def main(page: ft.Page) -> None:
    # Flet's Windows desktop transport may report a peer reset while its
    # Proactor pipe is being closed. Ignore only that specific shutdown event;
    # every other asyncio error keeps the normal exception reporting path.
    if sys.platform == "win32":
        loop = asyncio.get_running_loop()
        default_exception_handler = loop.get_exception_handler()

        def handle_windows_pipe_shutdown(loop, context):
            error = context.get("exception")
            message = context.get("message", "")
            if (
                isinstance(error, ConnectionResetError)
                and getattr(error, "winerror", None) == 10054
                and "_call_connection_lost" in message
            ):
                return
            if default_exception_handler:
                default_exception_handler(loop, context)
            else:
                loop.default_exception_handler(context)

        loop.set_exception_handler(handle_windows_pipe_shutdown)

    # Initialize the database before displaying the UI.
    initialize_database()

    if not check_database_connection():
        page.title = APP_NAME
        page.add(
            ft.Text(
                "Unable to connect to the application database.",
                color="#B91C1C",
            )
        )
        return

    # Start the main application interface.
    app = AppShell(page)
    app.start()


if __name__ == "__main__":
    ft.run(main)
