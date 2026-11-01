
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from sqlalchemy import create_engine, event
from sqlalchemy import inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

# our declared db path
from core.config import DATABASE_PATH


# Base class for all database models.
class Base(DeclarativeBase):
    pass


# Create the database engine.
# SQLite stores our data in a local file.
engine = create_engine(
    f"sqlite:///{DATABASE_PATH.as_posix()}",
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def enable_sqlite_foreign_keys(connection, _record) -> None:
    # SQLite leaves FK checks off unless enabled for every connection;
    # this makes Client relationships' ON DELETE CASCADE guarantees real.
    cursor = connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


# Each operation can obtain its own database session.
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
)


def initialize_database() -> None:
    """Create missing tables, then apply safe legacy-schema migrations."""
    # Import every model so create_all can register new module tables. It only
    # creates missing tables; it does not drop or rewrite existing user data.
    import core.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    migrate_client_columns()
    migrate_valuation_columns()
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
