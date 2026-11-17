# Portable EXE — Quick Runbook (do it yourself)

Short version of `BUILD-EXE.md`: just the commands, in order, plus how to
find the import problems that break a build. Follow top to bottom.

---

## A. Build the portable exe (every time)

```powershell
cd D:\Projects\Python\LandOffice

# 1) Run the tests first — never ship a red suite
.\.env\Scripts\python.exe -m pytest -q

# 2) Bump the version in core/config.py  (APP_VERSION = "0.2.9" etc.)

# 3) Register every new module you added since the last build (see section C)
#    -> open app.spec and add them to the hiddenimports lists

# 4) Build (one-file exe lands in dist_new\)
.\.env\Scripts\python.exe -m PyInstaller app.spec --noconfirm --distpath dist_new --workpath build_new
```

Build takes 1–3 minutes. Result: **one single file** `dist_new\LandOfficeManager.exe`
(~145 MB). Distribute only that file — nothing else is needed.

## B. Verify the exe before giving it out

```powershell
# 1) Copy ONLY the exe to a fresh folder
New-Item -ItemType Directory -Force -Path "$env:TEMP\lom_test" | Out-Null
Copy-Item "dist_new\LandOfficeManager.exe" "$env:TEMP\lom_test\"

# 2) Run it from there
Start-Process "$env:TEMP\lom_test\LandOfficeManager.exe" -WorkingDirectory "$env:TEMP\lom_test"
```

Checklist while it runs (first start is a few seconds slower — self-extraction):

- [ ] The window opens with the title "Land Office Manager v<your new version>".
- [ ] A `data\land_office.sqlite3` file appears next to the exe
      (`$env:TEMP\lom_test\data\land_office.sqlite3`) with all 16+ tables.
- [ ] The Naqsha page opens (newest module — proves the new imports are packed).
- [ ] Settings → database switching still works.

Clean up: close the app, then `Remove-Item "$env:TEMP\lom_test" -Recurse -Force`.

## C. Find dynamic imports BEFORE building (the errors that killed earlier exes)

PyInstaller scans **static imports only**. A module loaded with
`__import__(...)`, `importlib.import_module(...)`, or `exec/eval` is never
packed → runtime error `No module named '<name>'`.

### C.1 Scan the source for dynamic import patterns

```powershell
cd D:\Projects\Python\LandOffice
Select-String -Path *.py,core\*.py,services\*.py,ui\*.py,ui\pages\*.py `
  -Pattern "__import__|importlib|eval\(|exec\(" | `
  ForEach-Object { "$($_.Filename):$($_.LineNumber): $($_.Line.Trim())" }
```

- `__import__` / `importlib.import_module` hits on **first-party modules**
  (`ui.*`, `services.*`, `core.*`) must be fixed: replace with a static
  import (e.g. `from ui.pages import dc_rates` at the top of the file).
  This exact bug is what produced "No module named 'ui.pages.dc_rates'".
- Hits inside third-party libraries (`.env\Lib\site-packages\...`) can be
  ignored — they are not part of your scan.

### C.2 Register every first-party module in app.spec

Open `app.spec` and make sure every module of the project appears in
`hiddenimports`:

```powershell
Select-String -Path app.spec -Pattern "core\.|services\.|ui\." | `
  ForEach-Object { $_.Line.Trim() }
# compare against the actual files:
Get-ChildItem core,services,ui,ui\pages -Filter *.py | `
  ForEach-Object { $_.FullName.Replace((Get-Location).Path + "\", "").Replace("\", ".") }
```

Any file missing from the list (e.g. the Naqsha modules
`services.naqsha_service` / `ui.pages.naqsha` added in 0.2.7) → add the
dotted module name to `app.spec`. Also add any new data files to `datas`.

### C.3 Check the build diagnostics after every build

```powershell
Select-String -Path build_new\app\warn-app.txt `
  -Pattern "missing module named '?(ui|services|core)\." | `
  ForEach-Object { $_.Line }
```

- Any `missing module named 'services.xxx'` / `'ui.xxx'` / `'core.xxx'`
  line = a module of yours is not packed. Add it to `hiddenimports` (or fix
  a dynamic import) and rebuild.
- Also search for **excluded** modules:
  `Select-String -Path build_new\app\warn-app.txt -Pattern "excluded module named"`
  — never exclude something imported at top level (the `PIL` crash came
  from this: flet_platform_assets/reportlab import Pillow top-level, so
  `PIL` must NOT be in the spec's `excludes`).

### C.4 If the exe exits with code 1 and no window

Build a console variant to see the traceback:

```powershell
# copy app.spec, change name="LandOfficeManagerDebug", debug=False→True, console=False→True
.\.env\Scripts\python.exe -m PyInstaller app_debug.spec --noconfirm --distpath dist_debug --workpath build_new
.\dist_debug\LandOfficeManagerDebug.exe        # console shows the real traceback
```

Delete `app_debug.spec`, `dist_debug\`, `build_new\` afterwards.

## D. Troubleshooting quick table

| Symptom | Fix |
|---|---|
| `No module named '<name>'` at runtime | Add `<name>` to `hiddenimports` in `app.spec`, rebuild (see C.3) |
| Exe exits code 1, no window | Run the console build (C.4), read the traceback; check `warn-app.txt` for excluded modules |
| `python313.dll not found` after copying | You built/copied the wrong artifact — the current `app.spec` is one-file; use `dist_new\LandOfficeManager.exe` |
| Downloads something on first start | The Flet client zip is missing in the venv — see §3.2 of `BUILD-EXE.md`, then rebuild |
| Antivirus flags the exe | Normal PyInstaller onefile false positive; restore/exclude it |
| Build fails: `flet-windows.zip` not found | Re-provision the Flet client (§3.2 of `BUILD-EXE.md`) |

## E. One-page summary

```powershell
cd D:\Projects\Python\LandOffice
.\.env\Scripts\python.exe -m pytest -q                                   # 1. test
# 2. bump APP_VERSION in core/config.py
# 3. add new modules to app.spec hiddenimports (scan: section C.1/C.2)
.\.env\Scripts\python.exe -m PyInstaller app.spec --noconfirm --distpath dist_new --workpath build_new   # 4. build
Select-String -Path build_new\app\warn-app.txt -Pattern "missing module named '?(ui|services|core)\."   # 5. check
Copy-Item dist_new\LandOfficeManager.exe $env:TEMP\lom_test\             # 6. verify alone
Start-Process $env:TEMP\lom_test\LandOfficeManager.exe
# 7. distribute dist_new\LandOfficeManager.exe (single file only)
```
