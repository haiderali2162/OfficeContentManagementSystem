from pathlib import Path

# Project root directory.
BASE_DIR = Path(__file__).resolve().parent.parent

# Local application data directory.
DATA_DIR = BASE_DIR / "data"

# SQLite database file.
DATABASE_PATH = DATA_DIR / "land_office.sqlite3"

# Create the data directory if it does not exist.
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Application information.
APP_NAME = "Land Office Manager"
APP_VERSION = "0.1.0"