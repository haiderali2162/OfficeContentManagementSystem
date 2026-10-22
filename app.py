
import flet as ft

from core.config import APP_NAME, APP_VERSION
from core.database import (
    check_database_connection,
    initialize_database,
)
from ui.app_shell import AppShell


def main(page: ft.Page) -> None:
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
