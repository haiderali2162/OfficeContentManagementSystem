
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
    """Create tables and add newer client/asset columns without data loss."""
    # Import models so SQLAlchemy registers their tables.
    import core.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    migrate_client_columns()


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
