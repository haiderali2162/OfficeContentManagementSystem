import sys
from pathlib import Path

# Project root directory.
# In a PyInstaller standalone build (onefile/onedir) module files live in a
# temporary extraction folder (sys._MEIPASS) that is wiped on exit, so the
# database must live NEXT TO THE EXECUTABLE instead. In normal runs the
# project root stays the repo folder (developer request: the portable app
# creates and keeps its own SQLite file where the portable file lives).
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent.parent

# Local application data directory.
DATA_DIR = BASE_DIR / "data"

# SQLite database file.
DATABASE_PATH = DATA_DIR / "land_office.sqlite3"

# Create the data directory if it does not exist.
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Application information.
APP_NAME = "Land Office Manager"
APP_VERSION = "0.2.3"