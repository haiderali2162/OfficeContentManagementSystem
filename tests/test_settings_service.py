"""Rate-card appearance settings tests on temporary SQLite databases."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base
from services import settings_service


@pytest.fixture
def settings_database(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'settings.sqlite3').as_posix()}"
    )
    Base.metadata.create_all(engine)
    isolated_sessions = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False
    )
    monkeypatch.setattr(settings_service, "SessionLocal", isolated_sessions)
    try:
        yield isolated_sessions
    finally:
        engine.dispose()


def test_card_appearance_defaults_when_no_setting_is_stored(settings_database):
    assert settings_service.get_card_appearance() == settings_service.DEFAULT_CARD_APPEARANCE


def test_card_appearance_defaults_when_settings_table_is_missing(
    tmp_path, monkeypatch
):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'empty.sqlite3').as_posix()}"
    )
    isolated_sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(settings_service, "SessionLocal", isolated_sessions)
    try:
        assert (
            settings_service.get_card_appearance()
            == settings_service.DEFAULT_CARD_APPEARANCE
        )
    finally:
        engine.dispose()


def test_card_appearance_round_trip_and_partial_update(settings_database):
    saved = settings_service.set_card_appearance({
        "heading_template": "  {schedule} · {location}  ",
        "background_color": "#fff8e7",
        "border_color": "#c2410c",
        "heading_color": "#7c2d12",
        "body_color": "#1e293b",
    })

    assert saved == {
        "heading_template": "{schedule} · {location}",
        "background_color": "#FFF8E7",
        "border_color": "#C2410C",
        "heading_color": "#7C2D12",
        "body_color": "#1E293B",
    }
    assert settings_service.get_card_appearance() == saved

    partial = settings_service.set_card_appearance({"body_color": "#334155"})
    assert partial["body_color"] == "#334155"
    assert partial["heading_template"] == "{schedule} · {location}"
    assert partial["background_color"] == "#FFF8E7"


def test_card_appearance_validation_rejects_bad_values(settings_database):
    with pytest.raises(settings_service.SettingsError, match="cannot be empty"):
        settings_service.set_card_appearance({"heading_template": "   "})

    with pytest.raises(settings_service.SettingsError, match="Unsupported placeholder"):
        settings_service.set_card_appearance({"heading_template": "{unknown}"})

    with pytest.raises(settings_service.SettingsError, match="unmatched"):
        settings_service.set_card_appearance({"heading_template": "{location"})

    with pytest.raises(settings_service.SettingsError, match="hex colour"):
        settings_service.set_card_appearance({"background_color": "white"})

    assert (
        settings_service.get_card_appearance()
        == settings_service.DEFAULT_CARD_APPEARANCE
    )


def test_reset_card_appearance_restores_and_persists_defaults(settings_database):
    settings_service.set_card_appearance({"background_color": "#000000"})
    reset = settings_service.reset_card_appearance()

    assert reset == settings_service.DEFAULT_CARD_APPEARANCE
    assert settings_service.get_card_appearance() == settings_service.DEFAULT_CARD_APPEARANCE


def test_invalid_stored_fields_fall_back_individually(settings_database):
    settings_service.set_setting(
        settings_service.CARD_APPEARANCE_KEY,
        {
            "heading_template": "{unknown}",
            "background_color": "not-a-colour",
            "border_color": "#123456",
            "heading_color": 12,
        },
    )

    appearance = settings_service.get_card_appearance()
    assert appearance["heading_template"] == "{schedule} schedule · {location}"
    assert appearance["background_color"] == "#FFFFFF"
    assert appearance["border_color"] == "#123456"
    assert appearance["heading_color"] == "#0F172A"
    assert appearance["body_color"] == "#475569"


def test_format_card_heading_uses_default_and_custom_placeholders():
    record = {
        "schedule_type": "fbr",
        "location_name": "Test Mauza",
        "classification": "Residential",
        "property_type": "Residential",
        "district": "Test District",
        "tehsil": "Test Tehsil",
        "mauza": "Test Mauza",
        "land_type": "urban",
        "published_year": 2025,
    }

    assert (
        settings_service.format_card_heading(
            settings_service.DEFAULT_CARD_APPEARANCE["heading_template"], record
        )
        == "FBR schedule · Test Mauza"
    )
    assert (
        settings_service.format_card_heading(
            "{schedule}: {classification} in {tehsil} ({year})", record
        )
        == "FBR: Residential in Test Tehsil (2025)"
    )
    assert (
        settings_service.format_card_heading("{unknown}", record)
        == "FBR schedule · Test Mauza"
    )


def test_cadastral_storage_preference_round_trip(settings_database):
    assert settings_service.get_cadastral_storage_mode() == "ranges"
    settings_service.set_cadastral_storage_mode("individual")
    assert settings_service.get_cadastral_storage_mode() == "individual"

    with pytest.raises(settings_service.SettingsError):
        settings_service.set_cadastral_storage_mode("columns")


def test_default_file_location_round_trip_and_clear(settings_database, tmp_path):
    folder = tmp_path / "exports"
    folder.mkdir()

    assert settings_service.get_default_file_location() == ""
    saved = settings_service.set_default_file_location(str(folder))
    assert saved == str(folder.resolve())
    assert settings_service.get_default_file_location() == str(folder.resolve())

    assert settings_service.set_default_file_location("") == ""
    assert settings_service.get_default_file_location() == ""


def test_default_file_location_validation(settings_database, tmp_path):
    file_path = tmp_path / "existing.txt"
    file_path.write_text("test", encoding="utf-8")

    with pytest.raises(settings_service.SettingsError, match="absolute"):
        settings_service.set_default_file_location("relative/folder")
    with pytest.raises(settings_service.SettingsError, match="does not exist"):
        settings_service.set_default_file_location(str(tmp_path / "missing"))
    with pytest.raises(settings_service.SettingsError, match="not a file"):
        settings_service.set_default_file_location(str(file_path))

    assert settings_service.get_default_file_location() == ""


def test_missing_saved_default_folder_is_treated_as_unset(
    settings_database, tmp_path
):
    settings_service.set_setting(
        settings_service.FILE_LOCATION_KEY, str(tmp_path / "moved-away")
    )

    assert settings_service.get_default_file_location() == ""
