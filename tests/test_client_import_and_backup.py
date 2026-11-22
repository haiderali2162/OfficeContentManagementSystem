"""Client import / swap and automatic backup service tests (temp DBs)."""

from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from core.database import Base
from core.models import Client
from services import backup_service, client_import_service, client_service


@pytest.fixture
def import_database(tmp_path, monkeypatch):
    """Isolated SQLite engine for both new services."""
    engine = create_engine(f"sqlite:///{(tmp_path / 'clients.sqlite3').as_posix()}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    isolated = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(client_import_service, "SessionLocal", isolated)
    monkeypatch.setattr(client_import_service.client_service, "SessionLocal", isolated)
    monkeypatch.setattr(backup_service, "engine", engine)
    settings_store = {}
    monkeypatch.setattr(backup_service, "get_setting",
                        lambda key, default=None: settings_store.get(key, default))
    monkeypatch.setattr(backup_service, "set_setting",
                        lambda key, value: settings_store.__setitem__(key, value))
    try:
        yield engine
    finally:
        engine.dispose()


def _valid_row(name="Ali Raza", cnic="35201-1234567-1", phone="0300-1234567"):
    return {
        "name": name, "father_name": "Muhammad", "cnic": cnic,
        "ntn_number": "", "cnic_expiry_date": "2030-01-01",
        "phone_number": phone, "email": "ali@example.com",
        "address": "Street 1", "tehsil": "City", "district": "District",
        "city": "City", "province": "Punjab",
        "payment_amount": "5000", "amount_received": "1000",
        "current_status": "Active", "referred_by": "Referral",
    }


def test_read_and_map_spreadsheet_columns():
    headers, rows = client_import_service.read_client_table(pasted_text=(
        "Name\tFather Name\tCNIC\tPhone\tEmail\tPayment\tReferred By\n"
        "Ali\tMuhammad\t35201-1234567-1\t0300-1234567\tali@x.com\t5000\tRef\n"
    ))
    mapping = client_import_service.suggest_client_column_mapping(headers)
    assert mapping["name"] == 0
    assert mapping["father_name"] == 1
    assert mapping["cnic"] == 2
    assert mapping["phone_number"] == 3
    assert mapping["email"] == 4
    assert mapping["payment_amount"] == 5
    assert mapping["referred_by"] == 6
    assert rows[0][0] == "Ali"


def test_import_preview_reports_problems_and_duplicates(import_database):
    headers = list(_valid_row())
    good = [_valid_row()]
    bad = [_valid_row("Sara", cnic="bad-cnic", phone="123")]
    dup = [_valid_row("Ali Two", cnic="35201-1234567-1")]
    rows = [
        [row[field] for field in headers] for row in [*good, *bad, *dup]
    ]
    mapping = {field: index for index, field in enumerate(headers)}
    preview = client_import_service.build_client_import_preview(
        headers, rows, mapping, default_password="secret123",
    )
    assert preview["ready"] == 1
    problems = " | ".join(preview["problems"])
    assert "CNIC" in problems  # malformed CNIC and duplicate CNIC reported
    assert "35201-1234567-1" in problems

    # Passwords are optional per row: with neither a default nor a row
    # password, the row is still importable — it just carries an
    # informational note (no credentials are stored for it).
    preview = client_import_service.build_client_import_preview(
        headers, rows, mapping, default_password="",
    )
    assert preview["rows"][0]["problem"] == ""
    assert preview["ready"] == 1
    assert any(
        "imports without a password" in note for note in preview["rows"][0]["notes"]
    )

    # An empty name cell gets the recognizable placeholder instead of a
    # blocking problem.
    headers_no_name = ["CNIC", "Phone", "Referred By"]
    rows_no_name = [["35201-8888888-9", "0300-8888888", "Referral"]]
    mapping_no_name = {
        "cnic": 0, "phone_number": 1, "referred_by": 2, "name": None,
    }
    preview = client_import_service.build_client_import_preview(
        headers_no_name, rows_no_name, mapping_no_name,
        default_password="secret123",
    )
    assert preview["ready"] == 1
    assert preview["rows"][0]["values"]["name"] == "(unnamed — row 2)"

    # A mapped Password column satisfies the row without any default.
    headers_with_pw = [*headers, "Password"]
    rows_with_pw = [
        [*good[0], "row-password"], [*bad[0], ""], [*dup[0], "x"],
    ]
    mapping_pw = {field: index for index, field in enumerate(headers_with_pw)}
    preview = client_import_service.build_client_import_preview(
        headers_with_pw, rows_with_pw, mapping_pw, default_password="",
    )
    assert preview["rows"][0]["problem"].startswith("CNIC must use")
    assert preview["ready"] == 0  # malformed/duplicate rows still flagged
    assert "No password" not in preview["rows"][0]["problem"]


def test_import_clients_creates_validated_records(import_database):
    headers = list(_valid_row())
    rows = [
        {"row_number": 2, "values": _valid_row(), "problem": "", "notes": []},
        {
            "row_number": 3, "values": _valid_row("Sara", cnic="nope"),
            "problem": "CNIC must use 00000-0000000-0 format.", "notes": [],
        },
    ]
    summary = client_import_service.import_clients(
        rows, default_password="secret123",
    )
    assert summary["created"] == 1
    assert summary["skipped"] == 1
    assert summary["errors"], "the skipped row must be reported"

    with client_import_service.SessionLocal.begin() as session:
        clients = session.scalars(select(Client)).all()
    assert len(clients) == 1
    assert clients[0].name == "Ali Raza"
    assert clients[0].password_ciphertext  # encrypted, never plain text

    # Per-row passwords: a row with its own password uses it; a row with
    # neither its own nor a default is skipped with a clear error.
    rows = [
        {
            "row_number": 2,
            "values": dict(_valid_row("Kamran", cnic="35201-5555555-5"),
                           password="from-the-file"),
            "problem": "", "notes": [],
        },
        {
            "row_number": 3,
            "values": _valid_row("Noor", cnic="35201-6666666-6"),
            "problem": "No password: the row has none and no default "
                       "password was set.", "notes": [],
        },
    ]
    summary = client_import_service.import_clients(rows, default_password="")
    assert summary["created"] == 1
    assert summary["skipped"] == 1
    from core.security import decrypt_secret
    with client_import_service.SessionLocal.begin() as session:
        kamran = session.scalar(
            select(Client).where(Client.name == "Kamran")
        )
        assert decrypt_secret(kamran.password_ciphertext) == "from-the-file"


def test_row_field_swapping_rules():
    values = _valid_row()
    swapped = client_import_service.swap_import_row_fields(
        values, ["phone_number", "email"],
    )
    assert swapped["phone_number"] == "ali@example.com"
    assert swapped["email"] == "0300-1234567"
    # Three fields rotate: first -> second -> third -> first.
    rotated = client_import_service.swap_import_row_fields(
        values, ["phone_number", "email", "city"],
    )
    assert rotated["phone_number"] == "City"
    assert rotated["email"] == "0300-1234567"
    assert rotated["city"] == "ali@example.com"
    # The name and password columns can never be swapped.
    with pytest.raises(client_import_service.ClientImportError):
        client_import_service.swap_import_row_fields(values, ["name", "email"])
    with pytest.raises(client_import_service.ClientImportError):
        client_import_service.swap_import_row_fields(
            dict(values, password="x"), ["password", "email"],
        )
    with pytest.raises(client_import_service.ClientImportError):
        client_import_service.swap_import_row_fields(values, ["email"])
    # Four fields rotate the same way.
    four = client_import_service.swap_import_row_fields(
        values, ["phone_number", "email", "city", "district"],
    )
    assert four["phone_number"] == "District"  # old last value flows to first
    assert four["email"] == "0300-1234567"
    assert four["city"] == "ali@example.com"
    assert four["district"] == "City"


def test_name_parentage_split_tokens():
    split = client_import_service.split_name_parentage
    assert split("usman ali so haider ali") == ("usman ali", "s/o", "haider ali")
    assert split("fatima do mushtaq") == ("fatima", "d/o", "mushtaq")
    assert split("khalida wo ahmad") == ("khalida", "w/o", "ahmad")
    assert split("rashida wid majid") == ("rashida", "widow of", "majid")
    assert split("rashida widow of majid") == ("rashida", "widow of", "majid")
    assert split("ali s/o muhammad") == ("ali", "s/o", "muhammad")
    # A plain name without any token stays untouched.
    assert split("just a name") == ("just a name", "", "")
    # A token with no parent text on either side is not guessed away.
    assert split("wid majid") == ("wid majid", "", "")


def test_name_split_toggle_in_preview(import_database):
    headers = ["Name", "Phone", "Referred By"]
    rows = [["usman ali so haider ali", "0300-1234567", "Referral"]]
    mapping = {"name": 0, "phone_number": 1, "referred_by": 2}
    required = {"cnic": False, "ntn_number": False, "phone_number": False,
                "email": False, "referred_by": True}
    preview = client_import_service.build_client_import_preview(
        headers, rows, mapping, default_password="secret123",
        required=required, split_names=True,
    )
    values = preview["rows"][0]["values"]
    assert values["name"] == "usman ali"
    assert values["father_name"] == "haider ali"
    # Disabled: the whole text stays in the name field.
    preview = client_import_service.build_client_import_preview(
        headers, rows, mapping, default_password="secret123",
        required=required, split_names=False,
    )
    assert preview["rows"][0]["values"]["name"] == "usman ali so haider ali"
    assert preview["rows"][0]["values"]["father_name"] == ""


def test_mandatory_optional_field_control(import_database):
    headers = ["Name", "CNIC", "Phone", "Email", "Referred By"]
    rows = [["Ali Raza", "35201-1234567-1", "", "", "Referral"]]
    mapping = {
        "name": 0, "cnic": 1, "phone_number": 2, "email": 3,
        "referred_by": 4, "ntn_number": None,
    }
    # Default import posture: everything optional, so the row is ready
    # even with empty phone/e-mail.
    preview = client_import_service.build_client_import_preview(
        headers, rows, mapping, default_password="secret123",
    )
    assert preview["rows"][0]["problem"] == ""
    assert preview["ready"] == 1
    lenient_row = preview["rows"][0]
    # Marking a field mandatory flags it.
    preview = client_import_service.build_client_import_preview(
        headers, rows, mapping, default_password="secret123",
        required={"phone_number": True},
    )
    assert "Phone number is marked mandatory but missing." in (
        preview["rows"][0]["problem"]
    )

    assert "Phone number is marked mandatory but missing." in (
        preview["rows"][0]["problem"]
    )

    # Import the lenient row: empty values stay empty in the database.
    summary = client_import_service.import_clients(
        [lenient_row], default_password="secret123",
        required={"phone_number": False, "email": False},
    )
    assert summary["created"] == 1
    with client_import_service.SessionLocal.begin() as session:
        client = session.scalar(
            select(Client).where(Client.name == "Ali Raza")
        )
        assert client.phone_number is None and client.email is None

    # Re-marking a field mandatory flags it again.
    preview = client_import_service.build_client_import_preview(
        headers, rows, mapping, default_password="secret123",
        required={"phone_number": True},
    )
    assert "Phone number is marked mandatory" in preview["rows"][0]["problem"]


def test_mandatory_location_and_father_fields(import_database):
    """Province/city/tehsil/district/father start optional and can be enforced."""
    headers = ["Name", "CNIC", "Phone", "Email", "Referred By"]
    base_row = [
        "Ali Raza", "35201-1234567-1", "0300-1234567",
        "ali@example.com", "Referral",
    ]
    rows = [base_row]
    mapping = {
        "name": 0, "cnic": 1, "phone_number": 2, "email": 3,
        "referred_by": 4, "ntn_number": None,
    }
    # Default: address/location fields and father are optional — the row
    # imports even with none of them filled.
    preview = client_import_service.build_client_import_preview(
        headers, rows, mapping, default_password="secret123",
        required=dict(client_import_service.REQUIREMENT_DEFAULTS),
    )
    assert preview["ready"] == 1

    # Ticking them as mandatory flags the empty values.
    preview = client_import_service.build_client_import_preview(
        headers, rows, mapping, default_password="secret123",
        required={
            "cnic": True, "ntn_number": False, "phone_number": True,
            "email": True, "referred_by": True,
            "father_name": True, "tehsil": True, "district": True,
            "city": True, "province": True,
        },
    )
    problems = preview["rows"][0]["problem"]
    assert "Marked mandatory but missing:" in problems
    for label in ("Father/husband name", "Tehsil", "District", "City",
                  "Province"):
        assert label in problems

    # The name split generates the father value, satisfying its flag.
    preview = client_import_service.build_client_import_preview(
        ["Name", "CNIC", "Phone", "Email", "Referred By"],
        [[
            "usman ali so haider ali", "35201-9999999-9", "0300-1234567",
            "usman@example.com", "Referral",
        ]],
        mapping, default_password="secret123",
        required={
            "cnic": True, "ntn_number": False, "phone_number": True,
            "email": True, "referred_by": True, "father_name": True,
        },
        split_names=True,
    )
    assert preview["rows"][0]["problem"] == ""
    assert preview["rows"][0]["values"]["father_name"] == "haider ali"


def test_cnic_normalizes_dashed_and_plain_values(import_database):
    """CNIC cells with or without dashes import as 00000-0000000-0."""
    from core.security import decrypt_secret

    headers = ["Name", "CNIC", "Phone", "Referred By"]
    rows = [
        ["Ali", "3520112345671", "0300-1234567", "Ref"],
        ["Sara", "35202-7654321-8", "0300-7654321", "Ref"],
    ]
    mapping = {
        "name": 0, "cnic": 1, "phone_number": 2, "referred_by": 3,
    }
    required = {"cnic": True, "ntn_number": False, "phone_number": False,
                "email": False, "referred_by": True}
    preview = client_import_service.build_client_import_preview(
        headers, rows, mapping, default_password="secret123",
        required=required,
    )
    assert preview["ready"] == 2
    assert preview["rows"][0]["values"]["cnic"] == "35201-1234567-1"
    assert preview["rows"][1]["values"]["cnic"] == "35202-7654321-8"

    summary = client_import_service.import_clients(
        preview["rows"], default_password="secret123", required=required,
    )
    assert summary["created"] == 2
    with client_import_service.SessionLocal.begin() as session:
        cnics = {
            row for row in session.scalars(select(Client.cnic)).all()
        }
    assert cnics == {"35201-1234567-1", "35202-7654321-8"}

    # Wrong lengths are still rejected.
    bad = client_import_service.build_client_import_preview(
        headers, [["X", "12345", "0300-1234567", "Ref"]],
        mapping, default_password="secret123", required=required,
    )
    assert "CNIC" in bad["rows"][0]["problem"]


def test_expiry_date_formats_normalize(import_database):
    """DD-MM-YYYY, DD.MM.YYYY, DD/MM/YYYY, ISO, and datetime cells import."""
    headers = ["Name", "CNIC", "CNIC Expiry", "Phone", "Referred By"]
    rows = [
        ["Ali", "35201-1234567-1", "31-12-2027", "0300-1234567", "Ref"],
        ["Sara", "35201-2222222-2", "31.12.2027", "0300-2222222", "Ref"],
        ["Nida", "35201-3333333-3", "31/12/2027", "0300-3333333", "Ref"],
        ["Umar", "35201-4444444-4", "2027-12-31", "0300-4444444", "Ref"],
        ["Ahmed", "35201-5555555-5", "31-12-27", "0300-5555555", "Ref"],
    ]
    mapping = {
        "name": 0, "cnic": 1, "cnic_expiry_date": 2, "phone_number": 3,
        "referred_by": 4,
    }
    required = {"cnic": True, "ntn_number": False, "phone_number": False,
                "email": False, "referred_by": True}
    preview = client_import_service.build_client_import_preview(
        headers, rows, mapping, default_password="secret123",
        required=required,
    )
    assert preview["ready"] == 5
    for row in preview["rows"]:
        assert row["values"]["cnic_expiry_date"] == "2027-12-31"
    summary = client_import_service.import_clients(
        preview["rows"], default_password="secret123", required=required,
    )
    assert summary["created"] == 5
    with client_import_service.SessionLocal.begin() as session:
        dates = sorted(
            str(row) for row in session.scalars(
                select(Client.cnic_expiry_date)
            ).all()
        )
    assert dates == ["2027-12-31"] * 5

    # Unparseable dates are flagged with a clear message.
    bad = client_import_service.build_client_import_preview(
        headers, [["X", "35201-6666666-6", "soon", "0300-6666666", "Ref"]],
        mapping, default_password="secret123", required=required,
    )
    assert "not a recognized date" in bad["rows"][0]["problem"]


def test_export_clients_excludes_passwords(import_database):
    headers = ["Name", "CNIC", "Phone", "Password", "Referred By"]
    rows = [[
        "Ali Raza", "35201-1234567-1", "0300-1234567", "sekret", "Ref",
    ]]
    mapping = {
        "name": 0, "cnic": 1, "phone_number": 2, "password": 3,
        "referred_by": 4,
    }
    required = {"cnic": True, "ntn_number": False, "phone_number": False,
                "email": False, "referred_by": True}
    preview = client_import_service.build_client_import_preview(
        headers, rows, mapping, default_password="",
        required=required,
    )
    assert preview["ready"] == 1
    client_import_service.import_clients(
        preview["rows"], default_password="", required=required,
    )

    with client_import_service.SessionLocal() as session:
        records = session.scalars(select(Client)).all()
    csv_payload = client_import_service.export_clients(records, "csv").decode(
        "utf-8-sig"
    )
    assert "Ali Raza" in csv_payload
    assert "35201-1234567-1" in csv_payload
    assert "sekret" not in csv_payload  # the file password never leaks
    assert "password" not in csv_payload.lower()
    xlsx_payload = client_import_service.export_clients(records, "xlsx")
    assert xlsx_payload[:2] == b"PK"  # a real Excel workbook
    ods_payload = client_import_service.export_clients(records, "ods")
    assert ods_payload[:2] == b"PK"  # a real OpenDocument spreadsheet


def test_saved_column_swap_preview_and_apply(import_database):
    with client_import_service.SessionLocal.begin() as session:
        session.add(Client(
            name="Ali", cnic="35201-1111111-1", city="City A",
            tehsil="Tehsil A", referred_by="R",
        ))
        session.add(Client(
            name="Sara", cnic="35201-2222222-2", city="Same",
            tehsil="Same", referred_by="R",
        ))
    # Two-column swap: only the client with differing values changes.
    preview = client_import_service.preview_saved_column_swap(
        ["city", "tehsil"],
    )
    assert preview["count"] == 1
    assert preview["affected"][0]["client_name"] == "Ali"
    changes = preview["affected"][0]["changes"]
    assert changes[0] == {"field": "city", "label": "City",
                          "before": "City A", "after": "Tehsil A"}
    summary = client_import_service.apply_saved_column_swap(["city", "tehsil"])
    assert summary["updated"] == 1
    with client_import_service.SessionLocal() as session:
        ali = session.scalar(select(Client).where(Client.name == "Ali"))
        assert ali.city == "Tehsil A" and ali.tehsil == "City A"

    # Three-column rotation: A -> B -> C -> A. Sara joins in here because
    # rotating city/tehsil/name rewrites her name ("Sara" -> "Same") —
    # the preview shows every affected client before anything is written.
    preview = client_import_service.preview_saved_column_swap(
        ["city", "tehsil", "name"],
    )
    assert preview["count"] == 2
    client_import_service.apply_saved_column_swap(["city", "tehsil", "name"])
    with client_import_service.SessionLocal() as session:
        ali = session.scalar(select(Client).where(Client.cnic == "35201-1111111-1"))
        # Values flow A -> B -> C -> A: city keeps nothing, tehsil takes
        # the old city, name takes the old tehsil.
        assert (ali.city, ali.tehsil, ali.name) == ("Ali", "Tehsil A", "City A")
        sara = session.scalar(select(Client).where(Client.cnic == "35201-2222222-2"))
        assert (sara.city, sara.tehsil, sara.name) == ("Sara", "Same", "Same")

    # Non-text columns are refused.
    with pytest.raises(client_import_service.ClientImportError):
        client_import_service.preview_saved_column_swap(["city", "payment_amount"])


def test_backup_settings_validation(import_database):
    # Enabled without a folder is refused.
    with pytest.raises(backup_service.BackupServiceError):
        backup_service.save_backup_settings(
            enabled=True, frequency_hours=24, location="", keep_count=5,
        )
    # Bad numbers are refused.
    with pytest.raises(backup_service.BackupServiceError):
        backup_service.save_backup_settings(
            enabled=False, frequency_hours=0, location="", keep_count=5,
        )
    # A valid configuration is stored and defaults filled on read.
    saved = backup_service.save_backup_settings(
        enabled=False, frequency_hours=6, location="D:\\Backups",
        keep_count=3,
    )
    assert saved["frequency_hours"] == 6 and saved["keep_count"] == 3
    current = backup_service.get_backup_settings()
    assert current["frequency_hours"] == 6 and current["keep_count"] == 3
    assert current["enabled"] is False


def test_manual_backup_creates_and_prunes_files(import_database, tmp_path):
    location = tmp_path / "backups"
    location.mkdir()
    backup_service.save_backup_settings(
        enabled=False, frequency_hours=24, location=str(location),
        keep_count=2,
    )
    first = backup_service.create_backup_now()
    assert first["reason"] == "manual"
    second = backup_service.create_backup_now(reason="manual")
    third = backup_service.create_backup_now()
    first_name = Path(first["target"]).name
    assert third["removed_old"] == [first_name]
    backups = backup_service.list_backups(str(location))
    assert len(backups) == 2  # retention kept the newest two
    assert backups[0]["name"].startswith("land_office_backup_")
    assert backup_service.get_last_backup_time() != ""

    # A missing folder is refused before any file work.
    backup_service.save_backup_settings(
        enabled=False, frequency_hours=24,
        location=str(tmp_path / "missing"), keep_count=2,
    )
    with pytest.raises(backup_service.BackupServiceError):
        backup_service.create_backup_now()


def test_scheduler_due_check_respects_settings(import_database, tmp_path):
    location = tmp_path / "auto"
    location.mkdir()
    backup_service.save_backup_settings(
        enabled=False, frequency_hours=24, location=str(location),
        keep_count=2,
    )
    assert backup_service.run_backup_if_due() is None  # disabled
    backup_service.save_backup_settings(
        enabled=True, frequency_hours=24, location=str(location),
        keep_count=2,
    )
    first = backup_service.run_backup_if_due()
    assert first and first["reason"] == "auto"
    # Running again immediately is not due yet.
    assert backup_service.run_backup_if_due() is None
