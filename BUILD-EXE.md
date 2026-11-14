# Building the Standalone Portable EXE — Complete Guide

This document explains how the standalone portable build of Land Office
Manager works, what the original errors were and how they were fixed, and the
exact commands you need to rebuild the exe yourself after any code change.

---

## 1. How the portable build works

`app.spec` (in the project root) tells **PyInstaller** to pack everything into
**one executable**: `dist\LandOfficeManager.exe` (~145 MB). That single file
contains:

- The Python runtime and `python313.dll` (no Python installation needed).
- Every package: Flet, SQLAlchemy, openpyxl, reportlab, odfpy,
  arabic-reshaper, python-bidi, cryptography, PIL/Pillow, and all of this
  project's modules (`ui.*`, `services.*`, `core.*`).
- The **Flet desktop client** (`flet-windows.zip`) — the window renderer — so
  the app never needs an internet download on first start.
- The Flet client archive is provisioned into the venv first (see §3.2) and
  the spec bundles it via its `datas` entry.

**Database behaviour in the portable exe** (`core/config.py`):

- When the app runs as a frozen exe, the data directory is resolved from the
  **exe's own location**: `<folder of LandOfficeManager.exe>\data\`.
- On first run the app creates `data\land_office.sqlite3` there automatically
  (all 16 tables via `Base.metadata.create_all`).
- The connection-options file `data\database.json` lives in the same folder,
  so the Settings page's "change database" feature (SQLite / PostgreSQL /
  MySQL / custom URL) keeps working — SQLite paths in Settings must be
  **absolute** (validated), which is what makes switching work no matter
  where the exe was launched from.
- In normal source runs nothing changes: the repo's `data\` folder is used
  exactly as before.

---

## 2. The errors that were found and fixed

| # | Error | Cause | Fix |
|---|-------|-------|-----|
| 1 | `The application encountered an error: No module named 'ui.pages.dc_rates'` | `ui/pages/valuation.py` loaded the DC/FBR rates page with a dynamic `__import__("ui.pages.dc_rates", ...)`. PyInstaller scans **static imports only**, so that module was never bundled. | Replaced with a static import at the top of `ui/pages/valuation.py`: `from ui.pages import dc_rates as _dc_rates_page_module` and used `_dc_rates_page_module.build_dc_rates_page(...)` where the dynamic call was. The lazy build/query behaviour is unchanged. |
| 2 | The exe exited with code 1 immediately, no window | The first `app.spec` excluded `PIL` as an "unused framework", but `flet_platform_assets` and `reportlab` import **Pillow at module top level** — every import of the app died inside the bundle. | Removed `PIL` from the spec's `excludes` list (Pillow is installed and is now bundled). Found by reading `build\LandOfficeManager\warn-app.txt` → `excluded module named PIL - imported by flet_platform_assets._icons (top-level) ...`. |
| 3 | `python313.dll not found` after copying only the exe | The first build was **onedir**: the exe needs the whole `LandOfficeManager\` folder (`_internal\` with python313.dll etc.). Copying the bare exe broke it. | Switched the spec to a **one-file** build: `a.binaries`, `a.zipfiles`, `a.datas` passed directly into `EXE(...)` with no `COLLECT`. Everything is packed into the single exe. |
| 4 | DB would not exist for end users | `core/config.py` used `Path(__file__).parent.parent` as the base dir — inside a frozen onefile exe that resolves into the temp extraction folder (`sys._MEIPASS`), which is wiped on exit. | `core/config.py` now uses `sys.executable`'s folder when frozen (`getattr(sys, "frozen", False)`), so `data\` is created and kept next to the exe. Source runs are untouched. |
| 5 | First-run internet download | The flet client zip is not part of the flet wheel, so a fresh portable exe downloaded it from GitHub on first start. | Provisioned `flet-windows.zip` (flet v1.0.4 client, ~40 MB, official GitHub release) into the venv and bundled it via the spec's `datas`, so distribution is fully offline. |
| 6 | No `app.spec` in the repo | The earlier broken exe was built ad-hoc (no spec committed). | `app.spec` created with explicit `hiddenimports` for every first-party module plus the extras PyInstaller misses (`flet_desktop`, `flet_platform_assets`, `odf.*`, `arabic_reshaper`/`bidi`, `sqlite3`). |

---

## 3. One-time setup (already done — repeat only after reinstalling the venv)

### 3.1 Ensure PyInstaller is installed

```powershell
cd D:\Projects\Python\LandOffice
.\.env\Scripts\activate
python -m pip install pyinstaller
```

(It is already listed in `requirements.txt` as `pyinstaller>=6.0`.)

### 3.2 Provision the Flet desktop client archive

The spec bundles this file: `.env\Lib\site-packages\flet_desktop\app\flet-windows.zip`.
If the folder is empty (flet wheels do not ship the client), download it once
from the official release matching your flet version (1.0.4 → v1.0.4):

```powershell
New-Item -ItemType Directory -Force -Path ".env\Lib\site-packages\flet_desktop\app" | Out-Null
Invoke-WebRequest -Uri "https://github.com/flet-dev/flet/releases/download/v1.0.4/flet-windows.zip" -OutFile ".env\Lib\site-packages\flet_desktop\app\flet-windows.zip"
```

Check the version matches your installed flet (`python -c "import flet; print(flet.version.__version__)"` in the venv) — if you upgrade flet, download the
matching release zip again, and `flet_desktop.get_artifact_filename()` tells
you the expected file name.

---

## 4. Rebuild the exe (every time after code changes)

### 4.1 Before building — checklist

1. Run the test suite:

   ```powershell
   .\.env\Scripts\python.exe -m pytest -q
   ```

2. **Bump the version** (`core/config.py` → `APP_VERSION`, currently 0.2.3) so
   the window title and the build are traceable. (Developer reminder: bump
   after each update.)
3. Make sure any **new module** you added is imported **statically**
   (`import x` / `from x import y`) somewhere reachable from `app.py`.
   Never use `__import__` or `importlib` for first-party modules — that is
   what broke the exe (error #1). If a module is only imported dynamically,
   add its name to `hiddenimports` in `app.spec`.
4. If you added **new data files** (fonts, templates, icons), add them to the
   `datas` list in `app.spec` (`(source_path, destination_folder)`).

### 4.2 Build

```powershell
cd D:\Projects\Python\LandOffice
.\.env\Scripts\python.exe -m PyInstaller app.spec --noconfirm --distpath dist_new --workpath build_new
```

- `--distpath dist_new` puts the exe in `dist_new\` and `--workpath build_new`
  keeps the intermediate build files separate from the old broken
  `dist\`/`build\` artefacts. You can drop both options to use the default
  `dist\` and `build\` folders instead.
- The result is the single file `dist_new\LandOfficeManager.exe`.
- Build time: roughly 1–2 minutes (analysis is cached in `build_new\`, so
  rebuilds after small changes are faster).
- Diagnostics land in `build_new\LandOfficeManager\warn-app.txt` — search it
  for `missing module named` when something misbehaves at runtime.

### 4.3 Test the portable exe

Copy **only the exe** to a different folder and run it from there:

```powershell
New-Item -ItemType Directory -Force -Path "C:\Temp\LOM_test" | Out-Null
Copy-Item "dist_new\LandOfficeManager.exe" "C:\Temp\LOM_test\LandOfficeManager.exe"
Start-Process -FilePath "C:\Temp\LOM_test\LandOfficeManager.exe" -WorkingDirectory "C:\Temp\LOM_test"
```

Expected: the window opens ("Land Office Manager v<version>") and
`C:\Temp\LOM_test\data\land_office.sqlite3` is created automatically with all
16 tables. Delete the test folder when done.

### 4.4 Distribute

Give users the **single exe file** only. On first run it creates
`data\land_office.sqlite3` beside itself. To move an installation together
with its data, copy the exe **and** the `data\` folder.

---

## 5. Database switching for end users (unchanged behaviour)

- The portable exe always creates and uses
  `<exe folder>\data\land_office.sqlite3` by default.
- Settings → change database still offers SQLite / PostgreSQL / MySQL /
  custom URL. SQLite paths must be **absolute** (validated) — for example
  `D:\Shared\office.sqlite3` — which is what keeps switching working
  regardless of the launch location.
- The connection settings live in
  `<exe folder>\data\database.json`; copying that file plus the `data\`
  folder moves the configuration with the installation.
- The khewat/schedule-lookup store lives inside the SQLite
  app-setting store, so it follows whichever database file the Settings page
  points to.

---

## 6. Troubleshooting

| Symptom | Check / fix |
|---------|-------------|
| `No module named '<name>'` at runtime | The module is missing from the bundle. Add `<name>` to `hiddenimports` in `app.spec` and rebuild. Check `build_new\LandOfficeManager\warn-app.txt` for `missing module named '<name>'`. |
| Exe exits immediately with code 1, no window | An import failed at startup. Read `build_new\LandOfficeManager\warn-app.txt`; look for `excluded module named X` — never exclude a module that is imported at top level (this was the `PIL` crash). Temporarily switch `console=False` → `console=True` in the spec to see the traceback in a console window. |
| `python313.dll not found` after copying the exe | You copied the exe of an **onedir** build. Either copy the whole folder, or rebuild with the current one-file `app.spec`. |
| The window opens but the exe downloads something on first start | The Flet client zip was not bundled — repeat §3.2 and rebuild. |
| Antivirus flags or deletes the exe | Normal false positive for PyInstaller onefile apps; restore/exclude it. Renaming the exe does not help. |
| The exe is slow to show the window | One-file builds self-extract to a temp folder at every start. An onedir build (`COLLECT` variant of the spec) starts faster if that matters. |
| Database seems lost after moving the exe | The DB lives in `<exe folder>\data\`. Copy that folder, or point Settings at an absolute SQLite path. |
| Build fails with a missing file for `flet-windows.zip` | Repeat §3.2 — the venv lost the provisioned client archive (for example after reinstalling the venv). |

---

## 7. Files involved in the portable build

| File | Role |
|------|------|
| `app.spec` | The PyInstaller build recipe (one-file exe, hiddenimports, Flet client data). |
| `core/config.py` | Frozen-aware base directory: exe folder when frozen, repo root in source runs. Owns `DATA_DIR`/`DATABASE_PATH` creation. |
| `ui/pages/valuation.py` | Static import of `ui.pages.dc_rates` (no `__import__`). |
| `core/database_config.py` | `data/database.json` connection-options file; validates absolute SQLite paths for the Settings DB switch. |
| `.env\Lib\site-packages\flet_desktop\app\flet-windows.zip` | The bundled Flet client archive (venv file, not part of the repo). |
| `dist_new\LandOfficeManager.exe` | The verified portable output. |

---

## 8. Quick command summary

```powershell
# one-time
cd D:\Projects\Python\LandOffice
.\.env\Scripts\activate
python -m pip install pyinstaller
New-Item -ItemType Directory -Force -Path ".env\Lib\site-packages\flet_desktop\app" | Out-Null
Invoke-WebRequest -Uri "https://github.com/flet-dev/flet/releases/download/v1.0.4/flet-windows.zip" -OutFile ".env\Lib\site-packages\flet_desktop\app\flet-windows.zip"

# every build (after code changes)
.\.env\Scripts\python.exe -m pytest -q            # test first
# bump APP_VERSION in core/config.py
.\.env\Scripts\python.exe -m PyInstaller app.spec --noconfirm --distpath dist_new --workpath build_new

# verify
Start-Process "dist_new\LandOfficeManager.exe"
# → window "Land Office Manager v<version>" opens,
# → data\land_office.sqlite3 created next to the exe

# distribute
# copy dist_new\LandOfficeManager.exe (single file) to users
```
