
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import threading

from sqlalchemy import create_engine, event, Engine
from sqlalchemy import inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.engine import URL, make_url

# our declared db path and the connection options file
from core.config import DATABASE_PATH
from core import database_config


# Base class for all database models.
class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------
# Engine configuration
#
# The engine is created from the connection options file (data/database.json),
# falling back to the built-in SQLite path when the file is absent or
# unusable. ``reconfigure_database`` below can swap the engine at runtime, so
# ``SessionLocal`` is NOT a plain sessionmaker: it is a proxy object that
# always dispatches to the *current* factory. Every module that imported
# ``SessionLocal`` keeps the same object and follows a live database switch.
# --------------------------------------------------------------------------

#: Serialises engine rebuilds while one is in progress.
_engine_lock = threading.Lock()


class SessionFactoryProxy:
    """Callable stand-in for the current :class:`sessionmaker`.

    Service modules run ``from core.database import SessionLocal`` once at
    import time, so re-binding ``core.database.SessionLocal`` alone would
    leave them holding a stale factory bound to the previous engine. The
    proxy avoids that without touching any other module: calls (and
    ``.begin()`` and attribute access) resolve against the live factory,
    which points at whatever database the settings page last connected to.
    """

    def __call__(self, *args, **kwargs):
        """Return a new Session bound to the currently configured engine."""
        return _current_sessionmaker()(*args, **kwargs)

    def begin(self, *args, **kwargs):
        """Forward a ``begin()`` context-manager call to the live factory."""
        return _current_sessionmaker().begin(*args, **kwargs)

    def __getattr__(self, name):
        # Forward any other sessionmaker attribute (for example ``.kw``, used
        # by tests to read the bound engine).
        return getattr(_current_sessionmaker(), name)


# ``configure_engine`` populates the live factory during module import, so a
# proxy call before that point is a programming error, never a user error.
SessionLocal = SessionFactoryProxy()


def _current_sessionmaker():
    """Return the live session factory bound to the current engine."""
    factory = globals().get("_active_sessionmaker")
    if factory is None:  # pragma: no cover - import always configures first
        raise RuntimeError("The database engine has not been configured yet.")
    return factory


def _install_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    """Enable foreign-key enforcement on every new SQLite connection.

    SQLite leaves foreign-key checks off unless enabled per connection; this
    makes Client relationships' ON DELETE CASCADE guarantees real. The listener
    is only registered for SQLite engines (see :func:`_build_engine`), so
    server dialects — which enforce referential integrity themselves — never
    receive SQLite ``PRAGMA`` statements.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _build_engine(url: str | URL) -> Engine:
    """Create an engine for ``url`` with dialect-appropriate connection hooks.

    SQLite-specific behavior (``check_same_thread=False`` so Flet's UI thread
    and worker threads may share pool connections, plus the foreign-key
    pragma listener) is applied only when the URL really targets SQLite:
    drivers such as psycopg2, pymysql, and pyodbc reject unknown connection
    arguments, so hard-coding them would break every server backend.
    """
    url_object = make_url(url)
    is_sqlite = url_object.get_backend_name() == "sqlite"
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False} if is_sqlite else {},
    )
    if is_sqlite:
        event.listen(engine, "connect", _install_sqlite_pragmas)
    return engine


def configure_engine(url: str | URL) -> Engine:
    """Build and install the engine/sessionmaker pair for ``url``.

    Returns the new engine. Runtime switches should use
    :func:`reconfigure_database`, which also disposes the old engine.
    """
    new_engine = _build_engine(url)
    new_factory = sessionmaker(
        bind=new_engine,
        autoflush=False,
        expire_on_commit=False,
    )
    with _engine_lock:
        globals()["engine"] = new_engine
        globals()["_active_sessionmaker"] = new_factory
    return new_engine


def reconfigure_database(url: str | URL) -> Engine:
    """Point the whole application at ``url`` without restarting.

    The new engine is installed first, then the previous engine is disposed,
    so in-flight operations finish on the old engine and every operation
    started afterwards uses the new database. No data is copied between
    databases; the previous database file/server remains untouched.
    """
    old_engine = globals().get("engine")
    new_engine = configure_engine(url)
    if old_engine is not None and old_engine is not new_engine:
        old_engine.dispose()
    return new_engine


# Startup connection: the saved options file when present, otherwise the
# original SQLite file. An unusable options file falls back to the SQLite
# default instead of blocking the startup path (the same defensive posture
# the settings service takes for app preferences).
try:
    _startup_url = database_config.build_url(database_config.load_options())
except Exception:  # noqa: BLE001 - startup must not crash on bad options
    _startup_url = f"sqlite:///{DATABASE_PATH.as_posix()}"

engine = configure_engine(_startup_url)


def initialize_database() -> None:
    """Create missing tables, then apply safe legacy-schema migrations.

    Legacy migrations (khewat rebuild, valuation/client column backfills) were
    written against SQLite and use SQLite-only statements such as
    ``PRAGMA table_info`` and SQLite ``ALTER TABLE`` forms. When the app runs
    on PostgreSQL, MySQL, SQL Server, or a custom backend, a fresh database
    gets the full current schema from ``create_all`` directly, so the SQLite
    migrations are deliberately skipped — running them would either fail on
    dialect-specific SQL or perform no useful work.
    """
    # Import every model so create_all can register new module tables. It only
    # creates missing tables; it does not drop or rewrite existing user data.
    import core.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    # Only apply the historical SQLite migrations on the SQLite dialect; they
    # use PRAGMA and SQLite ALTER TABLE forms (see their bodies below).
    if engine.dialect.name == "sqlite":
        migrate_client_columns()
        migrate_valuation_columns()
        migrate_tax_rule_columns()
        migrate_khewat_schema()


def _backup_sqlite_database(database_engine) -> Path | None:
    """Create a non-overwriting safety copy before rebuilding Khewat tables."""
    database_name = database_engine.url.database
    if not database_name or database_name == ":memory:":
        return None
    database_path = Path(database_name)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    backup_path = database_path.with_name(
        f"{database_path.stem}.pre_khewat_rhz_{stamp}{database_path.suffix}"
    )
    source = database_engine.raw_connection()
    destination = None
    try:
        # Exclusive file creation prevents replacing an earlier safety copy.
        with backup_path.open("xb"):
            pass
        destination = sqlite3.connect(str(backup_path))
        source.driver_connection.backup(destination)
        destination.commit()
    except Exception:
        backup_path.unlink(missing_ok=True)
        raise
    finally:
        if destination is not None:
            destination.close()
        source.close()
    return backup_path


def migrate_khewat_schema(database_engine=None) -> None:
    """Safely add RHZ fields and permit the same Khewat number in later years.

    SQLite cannot drop the old unique constraint in place. This migration
    rebuilds the two Khewat tables in one transaction, copies all identifiers
    and references, and gives legacy rows a NULL year rather than guessing.
    """
    database_engine = database_engine or engine
    inspector = inspect(database_engine)
    if "property_khewats" not in inspector.get_table_names():
        return

    current_columns = {column["name"] for column in inspector.get_columns("property_khewats")}
    expected_unique = [
        "province", "district", "tehsil", "qanoongoi", "patwar_circle",
        "mauza_name", "hadbast_number", "khewat_number", "rhz_year",
    ]
    with database_engine.connect() as connection:
        index_rows = connection.exec_driver_sql("PRAGMA index_list(property_khewats)").all()
        unique_indexes = [row[1] for row in index_rows if row[2]]
        unique_columns = [
            [column[2] for column in connection.exec_driver_sql(
                f"PRAGMA index_info('{index_name}')"
            ).all()]
            for index_name in unique_indexes
        ]
    has_year_unique = expected_unique in unique_columns
    if {"rhz_year", "khewat_size"} <= current_columns and has_year_unique:
        return

    references_exist = "khewat_references" in inspector.get_table_names()
    with database_engine.connect() as connection:
        count = connection.exec_driver_sql("SELECT COUNT(*) FROM property_khewats").scalar_one()
        reference_count = (
            connection.exec_driver_sql("SELECT COUNT(*) FROM khewat_references").scalar_one()
            if references_exist else 0
        )
    if count or reference_count:
        _backup_sqlite_database(database_engine)

    raw = database_engine.raw_connection()
    dbapi_connection = raw.driver_connection
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute("BEGIN IMMEDIATE")
        if references_exist:
            cursor.execute("ALTER TABLE khewat_references RENAME TO khewat_references_legacy")
        cursor.execute("ALTER TABLE property_khewats RENAME TO property_khewats_legacy")
        cursor.execute("""
            CREATE TABLE property_khewats (
                id INTEGER NOT NULL PRIMARY KEY,
                province VARCHAR(120) NOT NULL DEFAULT '',
                district VARCHAR(120) NOT NULL DEFAULT '',
                tehsil VARCHAR(120) NOT NULL DEFAULT '',
                qanoongoi VARCHAR(200) NOT NULL DEFAULT '',
                patwar_circle VARCHAR(200) NOT NULL DEFAULT '',
                mauza_name VARCHAR(200) NOT NULL,
                hadbast_number VARCHAR(50) NOT NULL DEFAULT '',
                khewat_number VARCHAR(100) NOT NULL,
                rhz_year VARCHAR(7),
                khewat_size VARCHAR(40),
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                CONSTRAINT uq_property_khewat_mauza_number_year UNIQUE (
                    province, district, tehsil, qanoongoi, patwar_circle,
                    mauza_name, hadbast_number, khewat_number, rhz_year
                )
            )
        """)

        old_columns = {row[1] for row in cursor.execute(
            "PRAGMA table_info(property_khewats_legacy)"
        ).fetchall()}
        target_columns = [
            "id", "province", "district", "tehsil", "qanoongoi", "patwar_circle",
            "mauza_name", "hadbast_number", "khewat_number", "rhz_year", "khewat_size",
            "created_at", "updated_at",
        ]
        fallback = {
            "province": "''", "district": "''", "tehsil": "''", "qanoongoi": "''",
            "patwar_circle": "''", "hadbast_number": "''", "rhz_year": "NULL",
            "khewat_size": "NULL", "created_at": "CURRENT_TIMESTAMP",
            "updated_at": "CURRENT_TIMESTAMP",
        }
        source_expressions = [
            f'"{column}"' if column in old_columns else fallback.get(column, "NULL")
            for column in target_columns
        ]
        cursor.execute(
            f"INSERT INTO property_khewats ({', '.join(target_columns)}) "
            f"SELECT {', '.join(source_expressions)} FROM property_khewats_legacy"
        )

        if references_exist:
            cursor.execute("""
                CREATE TABLE khewat_references (
                    id INTEGER NOT NULL PRIMARY KEY,
                    khewat_id INTEGER NOT NULL,
                    reference_type VARCHAR(10) NOT NULL,
                    reference_value VARCHAR(100) NOT NULL,
                    CONSTRAINT ck_khewat_reference_type
                        CHECK (reference_type IN ('square', 'kila')),
                    CONSTRAINT uq_khewat_reference_value
                        UNIQUE (khewat_id, reference_type, reference_value),
                    FOREIGN KEY (khewat_id) REFERENCES property_khewats(id) ON DELETE CASCADE
                )
            """)
            cursor.execute("""
                INSERT INTO khewat_references (id, khewat_id, reference_type, reference_value)
                SELECT id, khewat_id, reference_type, reference_value
                FROM khewat_references_legacy
            """)
            cursor.execute("DROP TABLE khewat_references_legacy")
        else:
            cursor.execute("""
                CREATE TABLE khewat_references (
                    id INTEGER NOT NULL PRIMARY KEY,
                    khewat_id INTEGER NOT NULL,
                    reference_type VARCHAR(10) NOT NULL,
                    reference_value VARCHAR(100) NOT NULL,
                    CONSTRAINT ck_khewat_reference_type
                        CHECK (reference_type IN ('square', 'kila')),
                    CONSTRAINT uq_khewat_reference_value
                        UNIQUE (khewat_id, reference_type, reference_value),
                    FOREIGN KEY (khewat_id) REFERENCES property_khewats(id) ON DELETE CASCADE
                )
            """)
        cursor.execute("DROP TABLE property_khewats_legacy")
        cursor.execute(
            "CREATE INDEX ix_property_khewat_mauza "
            "ON property_khewats (mauza_name, district, tehsil)"
        )
        cursor.execute("CREATE INDEX ix_property_khewats_rhz_year ON property_khewats (rhz_year)")
        cursor.execute("CREATE INDEX ix_khewat_references_khewat_id ON khewat_references (khewat_id)")
        foreign_key_errors = cursor.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise RuntimeError(
                f"Khewat migration found invalid references: {foreign_key_errors[:5]}"
            )
        dbapi_connection.commit()
    except Exception:
        dbapi_connection.rollback()
        raise
    finally:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
        raw.close()



def migrate_valuation_columns(database_engine=None) -> None:
    """Add schedule dimensions to already-created valuation tables safely."""
    # An engine argument also lets migration tests use a temporary SQLite file;
    # normal application startup continues to use the configured office DB.
    database_engine = database_engine or engine
    tables = set(inspect(database_engine).get_table_names())
    location_additions = {
        "serial_no": "VARCHAR(80)",
        # Mauza is used by the calculator and formula conditions. Older
        # location tables may predate the structured schedule fields.
        "mauza": "VARCHAR(200)",
        "land_type": "VARCHAR(20)",
        "committee_town": "VARCHAR(200)",
        "revenue_circle": "VARCHAR(200)",
        "property_area": "VARCHAR(200)",
        "qanoongoi": "VARCHAR(200)",
        "patwar_circle": "VARCHAR(200)",
        "square_no": "VARCHAR(80)",
        "kila_no": "VARCHAR(80)",
        "khasra_no": "VARCHAR(100)",
    }
    rate_additions = {
        "schedule_type": "VARCHAR(10) NOT NULL DEFAULT 'dc'",
        "floor": "VARCHAR(100)",
        "published_year": "INTEGER",
        "structure_rate": "NUMERIC(20, 8)",
        "per_marla_value": "NUMERIC(20, 8)",
    }
    with database_engine.begin() as connection:
        if "property_locations" in tables:
            present = {c["name"] for c in inspect(database_engine).get_columns("property_locations")}
            for name, sql_type in location_additions.items():
                if name not in present:
                    connection.execute(text(
                        f'ALTER TABLE property_locations ADD COLUMN "{name}" {sql_type}'
                    ))
        if "dc_valuation_rates" in tables:
            present = {c["name"] for c in inspect(database_engine).get_columns("dc_valuation_rates")}
            for name, sql_type in rate_additions.items():
                if name not in present:
                    connection.execute(text(
                        f'ALTER TABLE dc_valuation_rates ADD COLUMN "{name}" {sql_type}'
                    ))
            # Keep old schedule rows useful in the new year-based editor.
            connection.execute(text(
                "UPDATE dc_valuation_rates SET published_year = "
                "CAST(strftime('%Y', effective_from) AS INTEGER) "
                "WHERE published_year IS NULL"
            ))


def migrate_tax_rule_columns(database_engine=None) -> None:
    """Add the ``fixed_or_percentage`` threshold column to tax rules safely.

    The hybrid calculation method stores its cut-over value in
    ``tax_fee_rules.percentage_threshold``. SQLite's ``create_all`` cannot add
    a column to an existing table, so startup inspects the table and performs
    a plain additive ``ALTER TABLE`` only when the column is missing. Older
    rows keep ``NULL`` there, which is correct: only the new method uses the
    threshold, and the tax service enforces which methods may supply one.
    """
    # An optional engine argument keeps migration tests on temporary SQLite
    # files exactly like the other column migrations above.
    database_engine = database_engine or engine
    tables = set(inspect(database_engine).get_table_names())
    if "tax_fee_rules" not in tables:
        return
    tax_rule_additions = {
        "percentage_threshold": "NUMERIC(18, 2)",
    }
    with database_engine.begin() as connection:
        present = {
            column["name"]
            for column in inspect(database_engine).get_columns("tax_fee_rules")
        }
        for name, sql_type in tax_rule_additions.items():
            if name not in present:
                connection.execute(text(
                    f'ALTER TABLE tax_fee_rules ADD COLUMN "{name}" {sql_type}'
                ))


def migrate_client_columns() -> None:
    """Apply additive SQLite changes for newer client and asset fields.

    SQLite's ``create_all`` does not alter an existing table. Inspecting the
    actual columns makes this migration safe to run each time the app starts.
    New fields remain nullable at the schema level so older rows stay valid;
    the service enforces required values for new and edited records.
    """
    additions = {
        "cnic_expiry_date": "DATE",
        "ntn_number": "VARCHAR(30)",
        "date_of_birth": "DATE",
        "registration_date": "DATE",
        "status_updated_date": "DATE",
        "phone_number": "VARCHAR(30)",
        "email": "VARCHAR(254)",
        "address": "TEXT",
        "tehsil": "VARCHAR(120)",
        "district": "VARCHAR(120)",
        "city": "VARCHAR(120)",
        "province": "VARCHAR(120)",
        "payment_amount": "NUMERIC(12, 2)",
        "amount_received": "NUMERIC(12, 2)",
    }
    inspector = inspect(engine)
    if "clients" not in inspector.get_table_names():
        return

    existing = {column["name"] for column in inspector.get_columns("clients")}
    with engine.begin() as connection:
        for name, sql_type in additions.items():
            if name not in existing:
                connection.execute(
                    text(f'ALTER TABLE clients ADD COLUMN "{name}" {sql_type}')
                )
        # A nullable unique index supports records identified by NTN only,
        # while still enforcing uniqueness once a number is provided.
        connection.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_clients_ntn_number "
            "ON clients (ntn_number)"
        ))

    asset_additions = {
        "category": "VARCHAR(40)",
        "fiscal_year": "VARCHAR(7)",
        "location": "TEXT",
        "make_model": "VARCHAR(160)",
        "registration_number": "VARCHAR(60)",
        "purchase_date": "DATE",
        "sale_date": "DATE",
        "purchase_price": "NUMERIC(14, 2)",
        "sale_price": "NUMERIC(14, 2)",
        "income_source_type": "VARCHAR(100)",
        "annual_income": "NUMERIC(14, 2)",
    }
    asset_tables = inspect(engine).get_table_names()
    if "client_assets" not in asset_tables:
        return

    existing_asset_columns = {
        column["name"] for column in inspect(engine).get_columns("client_assets")
    }
    with engine.begin() as connection:
        for name, sql_type in asset_additions.items():
            if name not in existing_asset_columns:
                connection.execute(text(
                    f'ALTER TABLE client_assets ADD COLUMN "{name}" {sql_type}'
                ))


def check_database_connection() -> bool:
    """Return True if the database connection works."""
    from sqlalchemy import text

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
