
import asyncio
import sys

import flet as ft

from core.config import APP_NAME, APP_VERSION
from core.database import (
    check_database_connection,
    initialize_database,
)
from services.backup_service import start_backup_scheduler
# v0.2.27: configure file logging before database/UI work so startup failures
# are recorded too. Crash hooks are installed for the whole process.
from services import logging_service
from ui.app_shell import AppShell


def main(page: ft.Page) -> None:
    logging_service.configure_logging()
    logging_service.install_exception_hooks()
    app_log = logging_service.get_logger("app")
    app_log.info("Starting Land Office Manager")

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
            # v0.2.27: record every real loop error in the rotating log and
            # a crash file before Flet's normal handler reports it.
            logging_service.log_async_exception(context)
            if default_exception_handler:
                default_exception_handler(loop, context)
            else:
                loop.default_exception_handler(context)

        loop.set_exception_handler(handle_windows_pipe_shutdown)

    # Initialize the database before displaying the UI.
    initialize_database()

    # The backup scheduler is a daemon thread checking once per minute
    # whether an automatic SQLite backup is due (Settings page controls it).
    start_backup_scheduler()

    if not check_database_connection():
        app_log.error("Application database connection check failed")
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
