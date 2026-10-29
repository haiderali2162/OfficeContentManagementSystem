"""Additive valuation-column migrations tested on temporary SQLite files."""

from sqlalchemy import create_engine, inspect

from core.database import migrate_valuation_columns


def test_legacy_location_table_gets_mauza_column_additively(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'legacy.sqlite3').as_posix()}")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE property_locations (id INTEGER PRIMARY KEY, name VARCHAR(200) NOT NULL)"
            )
            connection.exec_driver_sql(
                "INSERT INTO property_locations (id, name) VALUES (1, 'Existing location')"
            )

        migrate_valuation_columns(engine)

        columns = {item["name"] for item in inspect(engine).get_columns("property_locations")}
        assert "mauza" in columns
        with engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT name FROM property_locations WHERE id=1"
            ).scalar_one() == "Existing location"
    finally:
        engine.dispose()
