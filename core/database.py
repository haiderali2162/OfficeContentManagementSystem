
from sqlalchemy import create_engine, event
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
    """Create all registered database tables."""
    # Import models so SQLAlchemy registers their tables.
    import core.models  # noqa: F401

    Base.metadata.create_all(bind=engine)


def check_database_connection() -> bool:
    """Return True if the database connection works."""
    from sqlalchemy import text

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
