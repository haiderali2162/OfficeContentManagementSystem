"""Multi-database backend support tests.

Everything here stays on: (a) option dictionaries validated in memory,
(b) ``database.json`` written to a temporary directory, and (c) real engine
switches to temporary SQLite files. The live ``data/land_office.sqlite3``
database is never written to or reconfigured to in a way that persists past
the test (``reconfigure_database`` restores the startup URL in a fixture).
"""

import base64
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from core import database_config
from core.database import Base, SessionLocal, reconfigure_database
from core.security import decrypt_secret


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def options_home(tmp_path, monkeypatch):
    """Point database_config's storage at a temporary directory.

    ``save_options`` writes ``database.json`` through the module-global
    ``CONFIG_FILE_PATH`` next to ``DATA_DIR``; both globals are redirected so
    no test ever touches the real ``data/`` folder.
    """
    config_dir = tmp_path / "app_data"
    config_dir.mkdir()
    monkeypatch.setattr(database_config, "CONFIG_FILE_PATH", config_dir / "database.json")
    monkeypatch.setattr(database_config, "DATA_DIR", config_dir)
    return config_dir


@pytest.fixture
def restore_startup_engine():
    """Restore the startup database after a test that reconfigures it.

    ``reconfigure_database`` mutates module globals in ``core.database``; the
    fixture captures the startup URL and reconfigures back so later tests (and
    any manual runs in the same session) keep using the original database.
    """
    from core import database as core_database

    startup_url = str(core_database.engine.url)
    yield
    core_database.reconfigure_database(startup_url)


# --------------------------------------------------------------------------
# Defaults, loading, and validation
# --------------------------------------------------------------------------

def test_default_options_point_to_the_bundled_sqlite_database():
    defaults = database_config.default_options()
    assert defaults["backend"] == "sqlite"
    # The default path must be absolute so the choice cannot depend on the
    # directory the app was launched from.
    assert Path(defaults["path"]).is_absolute()


def test_load_options_falls_back_to_defaults_when_file_is_missing(options_home):
    assert database_config.load_options() == database_config.default_options()


def test_load_options_falls_back_when_file_is_malformed(options_home):
    database_config.CONFIG_FILE_PATH.write_text("{not json", encoding="utf-8")
    assert database_config.load_options() == database_config.default_options()


def test_load_options_rejects_unknown_backend(options_home):
    database_config.CONFIG_FILE_PATH.write_text(
        json.dumps({"backend": "oracle"}), encoding="utf-8"
    )
    assert database_config.load_options()["backend"] == "sqlite"


def test_normalize_options_requires_sqlite_absolute_path():
    with pytest.raises(database_config.DatabaseConfigError) as excinfo:
        database_config.normalize_options({"backend": "sqlite", "path": "relative.db"})
    assert "absolute" in str(excinfo.value)


def test_normalize_options_requires_existing_sqlite_folder(options_home):
    with pytest.raises(database_config.DatabaseConfigError) as excinfo:
        database_config.normalize_options({
            "backend": "sqlite",
            "path": str(options_home / "missing-folder" / "land_office.sqlite3"),
        })
    assert "folder" in str(excinfo.value)


def test_normalize_options_requires_server_fields(monkeypatch):
    # A driver is assumed present for URL assembly; driver presence is tested
    # separately so validation does not depend on which packages are installed.
    monkeypatch.setattr(
        database_config, "_resolve_driver", lambda *_args, **_kw: ("psycopg2", "")
    )
    options = {
        "backend": "postgres", "host": "localhost",
        "port": "5432", "database": "landoffice", "user": "landoffice",
        "password": "secret",
    }
    normalized = database_config.normalize_options(options)
    assert normalized["host"] == "localhost"
    assert normalized["port"] == "5432"


def test_normalize_options_reports_the_first_missing_server_field():
    with pytest.raises(database_config.DatabaseConfigError) as excinfo:
        database_config.normalize_options({"backend": "postgres", "host": "db1"})
    assert "database name" in str(excinfo.value)


def test_normalize_options_validates_the_port():
    bad = {
        "backend": "mysql", "host": "h", "database": "d", "user": "u",
        "port": "99999",
    }
    with pytest.raises(database_config.DatabaseConfigError) as excinfo:
        database_config.normalize_options(bad)
    assert "between 1 and 65535" in str(excinfo.value)

    not_a_number = dict(bad, port="five")
    with pytest.raises(database_config.DatabaseConfigError) as excinfo:
        database_config.normalize_options(not_a_number)
    assert "port must be a number" in str(excinfo.value)


def test_normalize_options_requires_a_full_custom_url():
    with pytest.raises(database_config.DatabaseConfigError) as excinfo:
        database_config.normalize_options({"backend": "custom", "url": "localhost/db"})
    assert "full SQLAlchemy URL" in str(excinfo.value)


# --------------------------------------------------------------------------
# URL building (driver presence stubbed so server tests do not need it)
# --------------------------------------------------------------------------

@pytest.fixture
def fake_any_driver(monkeypatch):
    """Stub driver resolution so URL tests do not need installed drivers.

    Each backend's canonical driver name is returned without importing it;
    driver-detection behavior (missing driver messages) is tested separately.
    """
    def resolve(backend, requested=""):
        return {
            "postgres": ("psycopg2", ""),
            "mysql": ("pymysql", ""),
            "mssql": ("pyodbc", ""),
        }.get(backend, ("", ""))

    monkeypatch.setattr(database_config, "_resolve_driver", resolve)


def test_build_url_sqlite_uses_forward_slashes(fake_any_driver):
    target = Path.home() / "land_office_backend_test.sqlite3"
    url = database_config.build_url({"backend": "sqlite", "path": str(target)})
    assert url.startswith("sqlite:///")
    assert url.endswith("land_office_backend_test.sqlite3")
    assert "\\" not in url  # no raw Windows separators in the URL


def test_build_url_postgres_quotes_credentials(fake_any_driver):
    url = database_config.build_url({
        "backend": "postgres", "host": "other host", "port": "5432",
        "database": "land office", "user": "user one", "password": "p@ss word",
    })
    # quote_plus encodes spaces in credentials as '+'; the database and host
    # path segments use percent-encoding instead.
    assert url == (
        "postgresql+psycopg2://user+one:p%40ss+word@other%20host:5432/"
        "land%20office"
    )


def test_build_url_mysql_and_mssql_preset_shapes(fake_any_driver):
    mysql = database_config.build_url({
        "backend": "mysql", "host": "db", "database": "landoffice",
        "user": "root",
    })
    assert mysql == "mysql+pymysql://root@db/landoffice"

    mssql = database_config.build_url({
        "backend": "mssql", "host": "db", "database": "landoffice",
        "user": "sa",
    })
    # The ODBC driver query parameter selects the SQL Server protocol.
    assert mssql.startswith("mssql+pyodbc://sa@db/landoffice?")
    assert "driver=ODBC+Driver+17+for+SQL+Server" in mssql


def test_build_url_reports_missing_server_driver(monkeypatch):
    monkeypatch.setattr(
        database_config, "_resolve_driver", lambda *_a, **_k: (None, "pip install x")
    )
    with pytest.raises(database_config.DatabaseConfigError) as excinfo:
        database_config.build_url({
            "backend": "postgres", "host": "db", "database": "d", "user": "u",
        })
    assert "driver" in str(excinfo.value).lower()


# --------------------------------------------------------------------------
# Password encryption at rest
# --------------------------------------------------------------------------

def test_password_is_encrypted_in_the_saved_file(options_home):
    database_config.save_options({
        "backend": "postgres", "host": "db", "port": "5432",
        "database": "landoffice", "user": "landoffice",
        "password": "plain-sekretpassword-123",
    })
    raw_text = database_config.CONFIG_FILE_PATH.read_text(encoding="utf-8")
    stored = json.loads(raw_text)
    # The plain password must never appear on disk.
    assert "plain-sekretpassword-123" not in raw_text
    assert stored["password"].startswith(database_config._ENCRYPTED_PREFIX)
    # The temporary atomic-write file must be gone.
    assert not (options_home / "database.json.tmp").exists()


def test_saved_password_round_trip(options_home):
    database_config.save_options({
        "backend": "postgres", "host": "db", "port": "5432",
        "database": "landoffice", "user": "landoffice",
        "password": "plain-sekretpassword-123",
    })
    encrypted = database_config.load_options()["password"]
    assert encrypted.startswith(database_config._ENCRYPTED_PREFIX)
    # Decrypting through the same helper used by build_url reconstructs the
    # plain password the user typed.
    token = encrypted[len(database_config._ENCRYPTED_PREFIX):]

    assert decrypt_secret(base64.b64decode(token)) == "plain-sekretpassword-123"


def test_encrypt_password_is_idempotent():
    once = database_config._encrypt_password("pw")
    twice = database_config._encrypt_password(once)
    assert once.startswith("enc:")
    assert twice == once


def test_empty_password_stays_empty():
    assert database_config._encrypt_password("") == ""
    assert database_config._decrypt_password("") == ""


# --------------------------------------------------------------------------
# save/load round trip and atomic writes
# --------------------------------------------------------------------------

def test_save_and_load_round_trip_preserves_options(options_home):
    options = {
        "backend": "postgres", "host": "db.local", "port": "5433",
        "database": "landoffice", "user": "office",
        "password": "round-trip-pw", "driver": "psycopg2",
    }
    database_config.save_options(options)
    loaded = database_config.load_options()
    assert loaded["backend"] == "postgres"
    assert loaded["host"] == "db.local"
    assert loaded["port"] == "5433"
    assert loaded["database"] == "landoffice"
    assert loaded["user"] == "office"
    assert loaded["driver"] == "psycopg2"
    # The password round-trips through build/normalize rather than staying
    # visible; only its encrypted form is stored.
    assert loaded["password"].startswith(database_config._ENCRYPTED_PREFIX)


def test_save_options_rejects_invalid_options_without_writing(options_home):
    with pytest.raises(database_config.DatabaseConfigError):
        database_config.save_options({"backend": "postgres"})
    assert not database_config.CONFIG_FILE_PATH.exists()


# --------------------------------------------------------------------------
# Connection test helper
# --------------------------------------------------------------------------

def test_connection_success_on_temporary_sqlite(tmp_path):
    target = tmp_path / "probe.sqlite3"
    # Create the file so the URL points at a real SQLite file.
    source_engine = create_engine(f"sqlite:///{target.as_posix()}")
    source_engine.dispose()
    ok, message = database_config.test_connection({
        "backend": "sqlite", "path": str(target),
    })
    assert ok, message
    assert "Connected" in message


def test_connection_failure_message_is_clear_and_secret_free(tmp_path):
    target = tmp_path / "missing-dir" / "x.sqlite3"
    password = "very-secret-db-password"
    ok, message = database_config.test_connection({
        "backend": "sqlite", "path": str(target), "password": password,
    })
    # The validation failure names the problem (the folder does not exist);
    # passwords must never surface in any message.
    assert not ok
    assert "folder" in message
    assert password not in message


# --------------------------------------------------------------------------
# Live engine switch (temporary SQLite only; restored afterwards)
# --------------------------------------------------------------------------

def test_reconfigure_database_repoints_session_factory(
    tmp_path, restore_startup_engine
):
    from core import database as core_database

    target = tmp_path / "switched.sqlite3"
    url = f"sqlite:///{target.as_posix()}"

    old_engine = core_database.engine
    new_engine = reconfigure_database(url)

    assert new_engine is not old_engine
    # The proxy factory now resolves placeholders/bind to the new engine.
    assert SessionLocal.kw["bind"] is new_engine
    with SessionLocal() as session:
        session.execute(text("CREATE TABLE probe_switch (value INTEGER)"))
        session.execute(
            text("INSERT INTO probe_switch (value) VALUES (42)")
        )
        stored = session.execute(
            text("SELECT value FROM probe_switch")
        ).scalar()
        session.commit()
    assert stored == 42
    # The writes landed in the temporary file, not the startup database.
    with create_engine(f"sqlite:///{target.as_posix()}").connect() as check:
        assert check.execute(
            text("SELECT value FROM probe_switch")
        ).scalar() == 42
    # The old engine instance is retired (the proxy no longer points at it).
    old_engine.dispose()
    assert core_database.engine is new_engine


def test_session_local_proxy_begin_and_call_dispatch(
    tmp_path, restore_startup_engine
):
    """Proxy's .begin() and __call__ must use the currently live factory."""
    from core import database as core_database

    target = tmp_path / "beginswitch.sqlite3"
    reconfigure_database(f"sqlite:///{target.as_posix()}")

    with SessionLocal.begin() as transaction:  # context-manager passthrough
        transaction.execute(text("CREATE TABLE probe_begin (value INTEGER)"))
        transaction.execute(
            text("INSERT INTO probe_begin (value) VALUES (1)")
        )
    # The proxy also supports plain calls (the sessions services use).
    with SessionLocal() as session:
        assert session.execute(
            text("SELECT value FROM probe_begin")
        ).scalar() == 1


def test_initialize_database_skips_sqlite_migrations_on_other_dialects(monkeypatch):
    from core import database as core_database

    calls: list[str] = []

    class FakeDialect:
        name = "postgresql"

    class FakeEngine:
        dialect = FakeDialect()

    monkeypatch.setattr(core_database, "engine", FakeEngine())
    monkeypatch.setattr(
        core_database.Base.metadata,
        "create_all",
        lambda *, bind=None: calls.append("create_all"),
    )
    monkeypatch.setattr(
        core_database, "migrate_client_columns", lambda: calls.append("client")
    )
    monkeypatch.setattr(
        core_database, "migrate_valuation_columns", lambda: calls.append("valuation")
    )
    monkeypatch.setattr(
        core_database, "migrate_khewat_schema", lambda: calls.append("khewat")
    )

    core_database.initialize_database()

    assert calls == ["create_all"]


def test_initialize_database_runs_migrations_on_sqlite_dialects(monkeypatch):
    from core import database as core_database

    calls: list[str] = []

    class FakeDialect:
        name = "sqlite"

    class FakeEngine:
        dialect = FakeDialect()

    monkeypatch.setattr(core_database, "engine", FakeEngine())
    monkeypatch.setattr(
        core_database.Base.metadata,
        "create_all",
        lambda *, bind=None: calls.append("create_all"),
    )
    monkeypatch.setattr(
        core_database, "migrate_client_columns", lambda: calls.append("client")
    )
    monkeypatch.setattr(
        core_database, "migrate_valuation_columns", lambda: calls.append("valuation")
    )
    monkeypatch.setattr(
        core_database, "migrate_tax_rule_columns", lambda: calls.append("tax_rules")
    )
    monkeypatch.setattr(
        core_database, "migrate_khewat_schema", lambda: calls.append("khewat")
    )

    core_database.initialize_database()

    assert calls == ["create_all", "client", "valuation", "tax_rules", "khewat"]


# --------------------------------------------------------------------------
# Settings page: database section
# --------------------------------------------------------------------------
# These tests exercise build_database_section with a fake Flet page. Every
# slow or stateful step (options file, connection test, live reconfigure,
# initialize_database) is monkeypatched, so no test here touches the real
# data/ folder or the live database.

import flet as ft  # noqa: E402

from ui.pages import settings as settings_page  # noqa: E402


class _FakePage:
    """Minimal stand-in for a Flet page (services + status updates)."""

    def __init__(self):
        self.services = []
        self.updates = 0

    def update(self):
        self.updates += 1


def _walk(control):
    """Yield a control and every descendant (Flet exposes children via
    ``controls`` lists and single ``content`` properties)."""
    yield control
    for child in getattr(control, "controls", []) or []:
        yield from _walk(child)
    content = getattr(control, "content", None)
    if isinstance(content, ft.Control):
        yield from _walk(content)


def _button(root, label: str):
    """Find a button by its label (Flet 1.0 stores the label in ``content``)."""
    for control in _walk(root):
        if isinstance(control, (ft.Button, ft.OutlinedButton)):
            if control.content == label:
                return control
    raise AssertionError(f"Button '{label}' not found on the settings page.")


def _field(root, label: str):
    for control in _walk(root):
        if isinstance(control, ft.TextField) and control.label == label:
            return control
    raise AssertionError(f"Field '{label}' not found on the settings page.")


@pytest.fixture
def isolated_database_section(options_home, monkeypatch):
    """Stub out every live side effect for build_database_section tests."""
    fake_engine = object()
    recorded = {"reconfigured": None, "initialized": 0}

    def fake_reconfigure(url):
        recorded["reconfigured"] = url
        return fake_engine

    def fake_initialize():
        recorded["initialized"] += 1

    def fake_test_connection(options):
        return True, "Connected (fake success)."

    monkeypatch.setattr(settings_page, "reconfigure_database", fake_reconfigure)
    monkeypatch.setattr(settings_page, "initialize_database", fake_initialize)
    monkeypatch.setattr(settings_page.database_config, "test_connection", fake_test_connection)
    return recorded


def test_database_section_offers_backends_and_default_sqlite(options_home):
    root = settings_page.build_database_section(_FakePage(), None)
    labels = [
        control.content for control in _walk(root)
        if isinstance(control, (ft.Button, ft.OutlinedButton))
    ]
    assert "Save & connect" in labels
    assert "Test connection" in labels
    # The bundled SQLite path is pre-filled for the default backend.
    assert str(settings_page.database_config.default_options()["path"]) == (
        _field(root, "SQLite database file").value
    )


def test_database_section_save_and_connect_records_every_step(
    isolated_database_section, options_home
):
    """Save & connect must validate, save, reconfigure, then initialize."""
    recorded = isolated_database_section
    root = settings_page.build_database_section(_FakePage(), None)
    target = options_home.parent / "chosen.sqlite3"
    _field(root, "SQLite database file").value = str(target)

    import asyncio

    asyncio.run(_button(root, "Save & connect").on_click(None))

    # The live engine swap and table creation were requested with the chosen
    # SQLite URL (monkeypatched, so no real engine was touched).
    assert recorded["reconfigured"] == f"sqlite:///{target.as_posix()}"
    assert recorded["initialized"] == 1
    # The choice was persisted into the temporary configuration file.
    stored = json.loads(
        (options_home / "database.json").read_text(encoding="utf-8")
    )
    assert stored["backend"] == "sqlite"
    assert stored["path"] == str(target)
    # The user was told the previous database was not changed.
    texts = " ".join(
        control.value or "" for control in _walk(root)
        if isinstance(control, ft.Text)
    )
    assert "no data was copied" in texts


def test_database_section_blocks_a_failed_connection_before_saving(
    isolated_database_section, options_home, monkeypatch
):
    """A connection failure must not persist options or swap the engine."""
    recorded = isolated_database_section

    def failing_test(options):
        return False, "Could not connect: fake failure."

    monkeypatch.setattr(
        settings_page.database_config, "test_connection", failing_test
    )
    root = settings_page.build_database_section(_FakePage(), None)
    save_button = _button(root, "Save & connect")

    import asyncio

    asyncio.run(save_button.on_click(None))

    assert recorded["reconfigured"] is None
    assert recorded["initialized"] == 0
    texts = " ".join(
        control.value or "" for control in _walk(root)
        if isinstance(control, ft.Text)
    )
    assert "Not connected" in texts
    assert not (options_home / "database.json").exists()


def test_database_section_empty_password_keeps_the_saved_one(
    isolated_database_section, options_home
):
    """Editing details with an empty password field must not drop it."""
    # Save a server backend with a password first (the stored form becomes
    # the encrypted "enc:" token).
    database_config.save_options({
        "backend": "postgres", "host": "db", "port": "5432",
        "database": "landoffice", "user": "office",
        "password": "start-password",
    })
    root = settings_page.build_database_section(_FakePage(), None)

    def capture_test_connection(options):
        capture_test_connection.captured = dict(options)
        return True, "Connected."

    capture_test_connection.captured = {}
    settings_page.database_config.test_connection = capture_test_connection

    import asyncio

    asyncio.run(_button(root, "Save & connect").on_click(None))

    sent = capture_test_connection.captured
    # The empty password field resolved to the saved (encrypted) password.
    assert sent["password"].startswith(database_config._ENCRYPTED_PREFIX)


def test_database_section_clear_password_checkbox_removes_it(
    isolated_database_section, options_home
):
    """Only the explicit checkbox clears a saved password."""
    database_config.save_options({
        "backend": "postgres", "host": "db", "port": "5432",
        "database": "landoffice", "user": "office",
        "password": "start-password",
    })
    root = settings_page.build_database_section(_FakePage(), None)

    def capture_test_connection(options):
        capture_test_connection.captured = dict(options)
        return True, "Connected."

    capture_test_connection.captured = {}
    settings_page.database_config.test_connection = capture_test_connection

    for control in _walk(root):
        if isinstance(control, ft.Checkbox) and control.label == (
            "Clear the saved password"
        ):
            control.value = True
            break

    import asyncio

    asyncio.run(_button(root, "Save & connect").on_click(None))

    # The saved, encrypted token was replaced by an empty string in the
    # options handed to the connection test (and therefore to the file).
    assert capture_test_connection.captured["password"] == ""


def _checkbox(root, label: str) -> ft.Checkbox:
    for control in _walk(root):
        if isinstance(control, ft.Checkbox) and control.label == label:
            return control
    raise AssertionError(f"Checkbox '{label}' not found on the settings page.")


def test_copy_data_checkbox_is_visible_only_for_sqlite(options_home):
    """The copy offer only makes sense for whole-file SQLite switches."""
    root = settings_page.build_database_section(_FakePage(), None)
    copy_box = _checkbox(root, "Copy current application data to this SQLite file")
    assert copy_box.visible is True

    dropdown = next(
        control for control in _walk(root) if isinstance(control, ft.Dropdown)
    )
    dropdown.value = "postgres"
    dropdown.on_change(None)
    assert copy_box.visible is False

    dropdown.value = "sqlite"
    dropdown.on_change(None)
    assert copy_box.visible is True


def test_ticked_copy_checkbox_copies_before_connecting(
    isolated_database_section, options_home, monkeypatch
):
    """Copy → test → save → reconfigure → initialize, in that order."""
    recorded = isolated_database_section
    copy_calls = []

    def fake_copy(source_engine, target_path):
        copy_calls.append((source_engine, target_path))
        return {
            "source": "source.sqlite3", "target": str(target_path),
            "tables": 5, "status": "copied",
        }

    monkeypatch.setattr(settings_page, "copy_sqlite_database", fake_copy)
    root = settings_page.build_database_section(_FakePage(), None)
    target = options_home.parent / "copied.sqlite3"
    _field(root, "SQLite database file").value = str(target)
    _checkbox(
        root, "Copy current application data to this SQLite file"
    ).value = True

    import asyncio

    asyncio.run(_button(root, "Save & connect").on_click(None))

    # The copy ran once, aimed at the chosen file, from the live engine.
    from core import database as core_database

    assert copy_calls == [(core_database.engine, str(target))]
    # The rest of the normal switching chain still happened.
    assert recorded["reconfigured"] == f"sqlite:///{target.as_posix()}"
    assert recorded["initialized"] == 1
    texts = " ".join(
        control.value or "" for control in _walk(root)
        if isinstance(control, ft.Text)
    )
    assert "data from the previous database was copied" in texts


def test_failed_copy_blocks_the_whole_switch(
    isolated_database_section, options_home, monkeypatch
):
    """A copy failure must not save options or swap the engine."""
    recorded = isolated_database_section
    from core.database_copy import DatabaseCopyError

    def failing_copy(source_engine, target_path):
        raise DatabaseCopyError("The folder does not exist, so the file cannot be created.")

    monkeypatch.setattr(settings_page, "copy_sqlite_database", failing_copy)
    root = settings_page.build_database_section(_FakePage(), None)
    _field(root, "SQLite database file").value = str(
        options_home.parent / "blocked.sqlite3"
    )
    _checkbox(
        root, "Copy current application data to this SQLite file"
    ).value = True

    import asyncio

    asyncio.run(_button(root, "Save & connect").on_click(None))

    assert recorded["reconfigured"] is None
    assert recorded["initialized"] == 0
    assert not (options_home / "database.json").exists()
    texts = " ".join(
        control.value or "" for control in _walk(root)
        if isinstance(control, ft.Text)
    )
    assert "Data was not copied" in texts
