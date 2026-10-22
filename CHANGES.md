# Changes and application flow

## Fixes

- Updated client dialogs and notifications for the installed Flet 1.x API. `show_dialog()` adds a dialog to Flet's active stack; `pop_dialog()` closes the current dialog and removes it from that stack. Previously, save/delete changed only the dialog's `open` flag, so later dialogs and actions could behave incorrectly.
- Kept the client save exception message available to the user so an unexpected failure is diagnosable instead of being hidden behind a generic message.
- Enabled SQLite foreign key checks on every database connection. This makes the client model's `ON DELETE CASCADE` relationships effective for assets and custom field values.
- Added comments where UI refresh, service calls, persistence, and model relationships meet.
- Changed the client save callback to refresh the list from SQLite immediately after commit, before dismissing the form. The database write can therefore no longer succeed while a dialog dismissal problem prevents the list refresh.
- Added a **Refresh** control to the FBR Clients toolbar to reload records and reapply the current search and status filters.
- Set the VS Code workspace interpreter to the project's `.env` Python and added `run_app.bat`, so startup uses the same environment that contains Flet and SQLAlchemy. The system `python` on this machine has no Flet installed, so using that interpreter cannot reliably launch this application.
- Expanded the Clients page comments to trace filter/form events into service functions, database models, and the SQLite file. Empty results and load/render errors now show the active database path to identify database location mismatches.
- Changed the results area to an expanding, independently scrollable `ListView`. The bounded search width lets the refresh and column controls remain visible and wrap on smaller windows.
- Kept the initial SQLite fetch when the Clients page is built during navigation, but deferred its page update until `AppShell` mounts the new page tree.

## How the application parts connect

1. `app.py` calls `initialize_database()` and then starts `AppShell`.
2. `ui/app_shell.py` uses `ui/navigation.py` to select and build the clients page.
3. `ui/pages/clients.py` builds the client list. Its form and card event handlers call functions in `services/client_service.py`.
4. `client_service.py` validates input and opens a `SessionLocal` session to create, query, update, or delete SQLAlchemy models.
5. `core/models.py` maps clients, assets, custom field definitions, and custom values to SQLite tables. Their foreign keys connect dependent records to their client.
6. After a successful client create/update/delete, the page calls `refresh_clients()`, which queries the service again and replaces the displayed cards.

## Verification performed

- Python compile check passed for `app.py`, `core`, `services`, and `ui`.
- Temporary SQLite CRUD smoke check passed for client create/read/search/update/delete, custom field value save/read, asset create/read, and cascading asset deletion.
- `git diff --check` passed.
- Verified that invoking the same Flet save callback persists a client and adds its card to the live clients-page control tree.
- Verified that `.env` Python loads the configured database and returns the existing client records; system `python` is a different interpreter and cannot import Flet.
- Verified that building the FBR Clients page immediately queries and renders all eight saved records, uses a scrollable ListView, and includes all four toolbar controls.
