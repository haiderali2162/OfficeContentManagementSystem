"""Multi-database backend support tests.

Everything here stays on: (a) option dictionaries validated in memory,
(b) ``database.json`` written to a temporary directory, and (c) real engine
switches to temporary SQLite files. The live ``data/land_office.sqlite3``
database is never written to or reconfigured to in a way that persists past
the test (``reconfigure_database`` restores the startup URL in a fixture).
"""

import asyncio
import base64
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core import database_config
from core.database import Base, SessionLocal, reconfigure_database
from core.security import decrypt_secret, encrypt_secret


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


def test_build_url_sqlite_uses_forward_slashes(fake_postgres_driver, options_home):
    target = Path.home() / "land_office_backend_test.sqlite3"
    url = database_config.build_url({"backend": "sqlite", "path": str(target)})
    assert url.startswith("sqlite:///")
    assert url.endswith("land_office_backend_test.sqlite3")
    assert "\\" not in url  # no raw Windows separators in the URL


def test_build_url_postgres_quotes_credentials(fake_postgres_driver):
    url = database_config.build_url({
        "backend": "postgres", "host": "other host", "port": "5432",
        "database": "land office", "user": "user one", "password": "p@ss word",
    })
    assert url == (
        "postgresql+psycopg2://user+one:p%40ss+word@other%20host:5432/"
        "land%20office"
    )


def test_build_url_mysql_and_mssql_preset_shapes(fake_postgres_driver):
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


def test_connection_failure_has_a_secret_free_message(tmp_path):
    ok, message = database_config.test_connection({
        "backend": "sqlite",
        "path": str(tmp_path / "missing-dir" / "x.sqlite3"),
    })
    assert not ok
    # No local paths or passwords in the message.
    assert "missing-dir" not in message


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
    # The old engine was disposed (a fresh session on it must fail).
    old_engine.dispose()
    with pytest.raises(Exception):
        with old_engine.connect():
            pass


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
        core_database, "migrate_khewat_schema", lambda: calls.append("khewat")
    )

    core_database.initialize_database()

    assert calls == ["create_all", "client", "valuation", "khewat"]
