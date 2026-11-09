"""Additive valuation-column migrations tested on temporary SQLite files."""

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from core.database import (
    migrate_khewat_schema,
    migrate_tax_rule_columns,
    migrate_valuation_columns,
)
from core.models import KhewatReference, PropertyKhewat


def test_legacy_tax_rules_table_gets_threshold_column_additively(tmp_path):
    """Old tax_fee_rules tables gain percentage_threshold without data loss."""
    engine = create_engine(f"sqlite:///{(tmp_path / 'legacy_tax.sqlite3').as_posix()}")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE tax_fee_rules ("
                "id INTEGER PRIMARY KEY, name VARCHAR(200) NOT NULL, "
                "rate_value NUMERIC(12, 8), fixed_amount NUMERIC(18, 2))"
            )
            connection.exec_driver_sql(
                "INSERT INTO tax_fee_rules (id, name, rate_value, fixed_amount) "
                "VALUES (1, 'Legacy rule', 2.5, NULL)"
            )

        migrate_tax_rule_columns(engine)

        columns = {item["name"] for item in inspect(engine).get_columns("tax_fee_rules")}
        assert "percentage_threshold" in columns
        with engine.connect() as connection:
            # Pre-existing rows keep their values and a NULL threshold,
            # which the tax service treats as "method does not use one".
            row = connection.exec_driver_sql(
                "SELECT name, rate_value, fixed_amount, percentage_threshold "
                "FROM tax_fee_rules WHERE id=1"
            ).first()
            assert row[0] == "Legacy rule" and row[1] is not None
            assert row[3] is None
    finally:
        engine.dispose()


def test_tax_rule_migration_is_a_noop_on_current_tables(tmp_path):
    """A table created from the current model already has the column."""
    from core.database import Base
    from core.models import TaxFeeRule  # noqa: F401 - registers the table

    engine = create_engine(f"sqlite:///{(tmp_path / 'current_tax.sqlite3').as_posix()}")
    try:
        Base.metadata.create_all(
            engine, tables=[TaxFeeRule.__table__]
        )
        # Running twice must be safe: the column exists after the first pass.
        migrate_tax_rule_columns(engine)
        migrate_tax_rule_columns(engine)
        columns = {item["name"] for item in inspect(engine).get_columns("tax_fee_rules")}
        assert "percentage_threshold" in columns
    finally:
        engine.dispose()


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


def test_khewat_rhz_migration_preserves_rows_and_allows_new_year_versions(tmp_path):
    database_path = tmp_path / "legacy_khewats.sqlite3"
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("""
                CREATE TABLE property_khewats (
                    id INTEGER PRIMARY KEY,
                    province VARCHAR(120) NOT NULL DEFAULT '',
                    district VARCHAR(120) NOT NULL DEFAULT '',
                    tehsil VARCHAR(120) NOT NULL DEFAULT '',
                    qanoongoi VARCHAR(200) NOT NULL DEFAULT '',
                    patwar_circle VARCHAR(200) NOT NULL DEFAULT '',
                    mauza_name VARCHAR(200) NOT NULL,
                    hadbast_number VARCHAR(50) NOT NULL DEFAULT '',
                    khewat_number VARCHAR(100) NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    CONSTRAINT uq_property_khewat_mauza_number UNIQUE (
                        province, district, tehsil, qanoongoi, patwar_circle,
                        mauza_name, hadbast_number, khewat_number
                    )
                )
            """)
            connection.exec_driver_sql("""
                CREATE TABLE khewat_references (
                    id INTEGER PRIMARY KEY,
                    khewat_id INTEGER NOT NULL,
                    reference_type VARCHAR(10) NOT NULL,
                    reference_value VARCHAR(100) NOT NULL,
                    CONSTRAINT ck_khewat_reference_type CHECK (reference_type IN ('square', 'kila')),
                    CONSTRAINT uq_khewat_reference_value UNIQUE (
                        khewat_id, reference_type, reference_value
                    ),
                    FOREIGN KEY (khewat_id) REFERENCES property_khewats(id) ON DELETE CASCADE
                )
            """)
            connection.exec_driver_sql("""
                INSERT INTO property_khewats (
                    id, province, district, tehsil, qanoongoi, patwar_circle,
                    mauza_name, hadbast_number, khewat_number, created_at, updated_at
                ) VALUES (9, 'Punjab', 'Test District', 'Test Tehsil', '', '',
                          'Legacy Mauza', '88', '30/2/1', '2020-01-01', '2020-01-01')
            """)
            connection.exec_driver_sql(
                "INSERT INTO khewat_references VALUES (5, 9, 'square', '12')"
            )

        migrate_khewat_schema(engine)

        columns = {column["name"] for column in inspect(engine).get_columns("property_khewats")}
        assert {"rhz_year", "khewat_size"} <= columns
        with engine.connect() as connection:
            unique_indexes = connection.exec_driver_sql("PRAGMA index_list(property_khewats)").all()
            unique_columns = [
                connection.exec_driver_sql(f"PRAGMA index_info('{item[1]}')").all()
                for item in unique_indexes if item[2]
            ]
        assert any(any(column[2] == "rhz_year" for column in columns) for columns in unique_columns)
        with Session(engine) as session:
            legacy = session.get(PropertyKhewat, 9)
            assert legacy.khewat_number == "30/2/1"
            assert legacy.rhz_year is None
            assert session.scalar(select(KhewatReference.reference_value)) == "12"
            session.add_all([
                PropertyKhewat(
                    province="Punjab", district="Test District", tehsil="Test Tehsil",
                    qanoongoi="", patwar_circle="", mauza_name="Legacy Mauza",
                    hadbast_number="88", khewat_number="30/2/1", rhz_year="2019-20",
                ),
                PropertyKhewat(
                    province="Punjab", district="Test District", tehsil="Test Tehsil",
                    qanoongoi="", patwar_circle="", mauza_name="Legacy Mauza",
                    hadbast_number="88", khewat_number="30/2/1", rhz_year="2020-21",
                ),
            ])
            session.commit()

        backups = list(tmp_path.glob("legacy_khewats.pre_khewat_rhz_*.sqlite3"))
        assert len(backups) == 1
        migrate_khewat_schema(engine)
        assert len(list(tmp_path.glob("legacy_khewats.pre_khewat_rhz_*.sqlite3"))) == 1
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()
