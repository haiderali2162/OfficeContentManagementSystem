"""Naqsha module tests on temporary SQLite databases (step 1)."""

import pytest

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from core.database import Base
from services import naqsha_service


@pytest.fixture
def naqsha_database(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'naqsha.sqlite3').as_posix()}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    isolated_sessions = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False
    )
    monkeypatch.setattr(naqsha_service, "SessionLocal", isolated_sessions)
    try:
        yield engine
    finally:
        engine.dispose()


def _land_sample():
    headers, rows = naqsha_service.parse_pasted_table(
        "Khewat No.\tKhatoni No.\tMurabba\tKilla\tQism Arazi\tRaqba\n"
        "12\t101\t5\t23\tNehri\t2-0-0\n"
        "12\t101\t5\t24\tNehri\t2-0-0\n"
        "12\t101\t5\t25\tBarani\t10k5m\n"
    )
    mapping = naqsha_service.suggest_column_mapping(headers, "land")
    return naqsha_service.build_land_rows(headers, rows, mapping)


def _owner_sample(khewat: str = "12"):
    headers, rows = naqsha_service.parse_pasted_table(
        "Name of Owner\tQaum\tHissa\tRaqba\n"
        "Ali s/o Muhammad\tRajput\t1/4\t1-0-0\n"
        "Ahmed d/o Fatima\tRajput\t\t20m\n"
        "Sara w/o Bilal\tJat\t2/4\t2-0-0\n"
        f"Imran widow of Kasim\tJat\t\t40m\n"
    )
    mapping = naqsha_service.suggest_column_mapping(headers, "owner")
    rows = naqsha_service.build_owner_rows(headers, rows, mapping)
    for row in rows:
        row["khewat_no"] = khewat
    return rows


def test_parse_pasted_table_handles_tabs_and_commas():
    headers, rows = naqsha_service.parse_pasted_table("A\tB\tC\n1\t2\t3\n")
    assert headers == ["A", "B", "C"] and rows == [["1", "2", "3"]]
    headers, rows = naqsha_service.parse_pasted_table("A,B\n1,2\n")
    assert headers == ["A", "B"] and rows == [["1", "2"]]
    with pytest.raises(naqsha_service.NaqshaError):
        naqsha_service.parse_pasted_table("   ")


def test_suggest_column_mapping_recognizes_portal_headers_and_ignores_extras():
    land_mapping = naqsha_service.suggest_column_mapping(
        ["Serial", "Khewat No", "Khatoni No", "Murabba No", "Killa No",
         "Qism Arazi", "Raqba (Area)", "Remarks"],
        "land",
    )
    assert land_mapping["khewat_no"] == 1
    assert land_mapping["khatoni_no"] == 2
    assert land_mapping["square_no"] == 3
    assert land_mapping["killa_no"] == 4
    assert land_mapping["land_type"] == 5
    assert land_mapping["area"] == 6  # "Serial" and "Remarks" stay unmapped
    owner_mapping = naqsha_service.suggest_column_mapping(
        ["Name of Owner", "Qaum", "Hissa", "Raqba"], "owner",
    )
    assert owner_mapping["owner_name"] == 0
    assert owner_mapping["caste"] == 1
    assert owner_mapping["share"] == 2
    assert owner_mapping["area"] == 3


def test_parse_owner_name_relations():
    assert naqsha_service.parse_owner_name("Ali s/o Muhammad") == (
        "Ali", "s/o", "Muhammad",
    )
    assert naqsha_service.parse_owner_name("Ahmed d/o Fatima") == (
        "Ahmed", "d/o", "Fatima",
    )
    assert naqsha_service.parse_owner_name("Sara w/o Bilal") == (
        "Sara", "w/o", "Bilal",
    )
    assert naqsha_service.parse_owner_name("Imran widow of Kasim") == (
        "Imran", "widow of", "Kasim",
    )
    assert naqsha_service.parse_owner_name("Just A Name") == ("Just A Name", "", "")


def test_land_rows_normalize_areas_and_report_problems():
    rows = naqsha_service.build_land_rows(
        ["Khewat", "Area", "Square"],
        [["12", "3K 15M", "5"], ["12", "banana", "6"], ["", "1-0-0", "7"]],
        {"khewat_no": 0, "area": 1, "square_no": 2},
    )
    assert rows[0]["area_marla"] == 75  # 3 kanal (60) + 15 marla
    assert rows[0]["problem"] == ""
    assert rows[1]["problem"] != "" and rows[1]["area_marla"] is None
    assert "Khewat" in rows[2]["problem"]


def test_area_normalization_and_share_examples():
    from decimal import Decimal

    # 3 kanal 15 marla = 75 marla.
    assert naqsha_service.parse_area_marla("3K 15M") == 75
    assert naqsha_service.parse_area_marla("10-5-0") == 205
    assert naqsha_service.parse_area_marla("100f") == pytest.approx(
        Decimal("100") / Decimal("272")
    )
    # Owner 1 kanal (20 marla) of a 4 kanal (80 marla) khewat -> 20/80.
    share, num, den = naqsha_service.corrected_share_text(20, 80)
    assert (share, num, den) == ("20/80", 20, 80)
    # Sarsahi fallback for fractional marlas (20.5 marla = 184.5 sarsahi,
    # not whole, so the exact reduced fraction is shown).
    share, num, den = naqsha_service.corrected_share_text(
        __import__("decimal").Decimal("20.5"), 80,
    )
    assert (share, num, den) == ("41/160", 41, 160)
    # Zero total yields no share.
    assert naqsha_service.corrected_share_text(20, 0) == ("", None, None)


def test_preview_totals_checkage_and_share_correction():
    land_rows = _land_sample()
    owner_rows = _owner_sample()
    groups = naqsha_service.build_naqsha_preview(land_rows, owner_rows)
    assert len(groups) == 1
    group = groups[0]
    assert group["khewat_no"] == "12"
    # 2-0-0 + 2-0-0 + 10k5m = 40 + 40 + 205 = 285 marla.
    assert group["land_total_marla"] == 285
    # 1-0-0 + 20m + 2-0-0 + 40m = 20 + 20 + 40 + 40 = 120 marla.
    assert group["owner_total_marla"] == 120
    assert group["checkage_status"] == "mismatch"
    assert "120/285" in group["checkage_ratio"]
    corrected = {row["owner_name"]: row["share_text_corrected"]
                 for row in group["owner_rows"]}
    assert corrected["Ali"] == "20/285"
    assert corrected["Ahmed"] == "20/285"
    assert corrected["Sara"] == "40/285"
    assert corrected["Imran"] == "40/285"


def test_preview_pairs_owner_rows_by_khewat_and_reports_unmatched():
    land_rows = _land_sample()
    # Two khewats in the land dataset so unmarked owner rows cannot be
    # attached without guessing.
    land_rows[2]["khewat_no"] = "13"
    owners = _owner_sample("12")
    # Unmatched owners (no khewat in a multi-khewat land dataset).
    stray = naqsha_service.build_owner_rows(
        ["Name", "Hissa", "Raqba"],
        [["Z s/o Y", "1/1", "1-0-0"]],
        {"owner_name": 0, "share": 1, "area": 2},
    )
    groups = naqsha_service.build_naqsha_preview(land_rows, owners + stray)
    assert any(group["khewat_no"] == "(khewat not in the land dataset)"
               for group in groups)
    # Forced assignment: the UI's "unmatched owners belong to khewat" field.
    groups = naqsha_service.build_naqsha_preview(
        land_rows, owners + stray, unmatched_owner_khewat="12",
    )
    khewat_12 = next(group for group in groups if group["khewat_no"] == "12")
    assert any(row["owner_name"] == "Z" for row in khewat_12["owner_rows"])
    assert len(groups) == 2  # khewat 12 (owners) + khewat 13 (no owners)


def test_save_round_trip_delete_and_duplicate_skip(naqsha_database):
    land_rows = _land_sample()
    owner_rows = _owner_sample()
    groups = naqsha_service.build_naqsha_preview(land_rows, owner_rows)
    result = naqsha_service.save_naqsha_records(
        {
            "district": "Sheikhupura", "tehsil": "Safdarabad",
            "qanoongoi": "Q1", "mauza": "Anmba", "province": "Punjab",
            "khatoni_no": "101", "source_note": "portal copy",
        },
        groups,
    )
    assert result["records"] == 1
    assert result["land_rows_added"] == 3
    assert result["owner_rows_added"] == 4
    assert result["skipped"] == 0

    records = naqsha_service.list_naqsha_records(khewat_no="12", mauza="anmb")
    assert len(records) == 1
    record = records[0]
    assert record["district"] == "Sheikhupura"
    assert record["khatoni_no"] == "101"
    assert record["source_note"] == "portal copy"
    assert len(record["land_rows"]) == 3
    assert len(record["owner_rows"]) == 4
    owner = next(row for row in record["owner_rows"] if row["owner_name"] == "Ali")
    assert owner["relation"] == "s/o" and owner["parent_name"] == "Muhammad"
    assert owner["share_text_corrected"] == "20/285"
    assert owner["share_numerator"] == 20 and owner["share_denominator"] == 285

    # Re-saving the same import skips every identical row, never duplicates.
    again = naqsha_service.save_naqsha_records(
        {"mauza": "Anmba", "district": "Sheikhupura",
         "tehsil": "Safdarabad", "qanoongoi": "Q1", "khatoni_no": "101"},
        naqsha_service.build_naqsha_preview(_land_sample(), _owner_sample()),
    )
    assert again["records"] == 0
    assert again["land_rows_added"] == 0
    assert again["owner_rows_added"] == 0
    assert again["skipped"] == 7

    # A changed land row is added beside the stored ones.
    changed = _land_sample()
    changed[0]["killa_no"] = "99"
    third = naqsha_service.save_naqsha_records(
        {"mauza": "Anmba", "district": "Sheikhupura",
         "tehsil": "Safdarabad", "qanoongoi": "Q1"},
        naqsha_service.build_naqsha_preview(changed, []),
    )
    assert third["land_rows_added"] == 1
    record = naqsha_service.list_naqsha_records(khewat_no="12")[0]
    assert len(record["land_rows"]) == 4

    summary = naqsha_service.summarize_naqsha_record(record)
    assert summary["land_total_marla"] == 325  # 285 stored + the added 40
    assert summary["checkage_status"] == "mismatch"

    valuation_rows = naqsha_service.naqsha_land_rows_for_valuation(record)
    assert valuation_rows[0]["mauza"] == "Anmba"
    assert valuation_rows[0]["square_no"] in {"5"}
    assert all("owner" not in row for row in valuation_rows)

    assert naqsha_service.delete_naqsha_record(record["id"]) is True
    assert naqsha_service.list_naqsha_records(khewat_no="12") == []
    assert naqsha_service.delete_naqsha_record(record["id"]) is False


def test_save_requires_mauza_and_rejects_problem_rows(naqsha_database):
    with pytest.raises(naqsha_service.NaqshaError):
        naqsha_service.save_naqsha_records(
            {"mauza": "", "khewat_no": "12"}, [],
        )
    land_rows = _land_sample()
    owner_rows = _owner_sample()
    owner_rows[1]["problem"] = "Cannot read the area 'oops'"
    groups = naqsha_service.build_naqsha_preview(land_rows, owner_rows)
    with pytest.raises(naqsha_service.NaqshaError):
        naqsha_service.save_naqsha_records(
            {"mauza": "Anmba", "khewat_no": "12"}, groups,
        )


def test_context_suggestions_and_get_record(naqsha_database):
    land_rows = _land_sample()
    naqsha_service.save_naqsha_records(
        {"mauza": "Anmba", "district": "Sheikhupura",
         "tehsil": "Safdarabad", "qanoongoi": "Q1"}, 
        naqsha_service.build_naqsha_preview(land_rows, []),
    )
    suggestions = naqsha_service.list_naqsha_context_suggestions()
    assert suggestions["mauza"] == ["Anmba"]
    assert suggestions["district"] == ["Sheikhupura"]
    record = naqsha_service.get_naqsha_record(
        naqsha_service.list_naqsha_records()[0]["id"],
    )
    assert record["mauza"] == "Anmba"
    assert naqsha_service.get_naqsha_record(99999) is None


def test_naqsha_page_builds_against_installed_flet_api():
    """The page control tree builds (catches removed-Flet-API mistakes)."""
    import flet as ft
    from ui.pages import naqsha

    class FakePage:
        def __init__(self):
            self.services = []
            self.dialogs = []

        def update(self):
            return None

        def show_dialog(self, dialog):
            self.dialogs.append(dialog)

        def pop_dialog(self):
            if self.dialogs:
                self.dialogs.pop()

        def run_task(self, handler, *args):
            handler(*args).close()
            return None

    root = naqsha.build_naqsha_page(FakePage())
    assert isinstance(root, ft.Control)
