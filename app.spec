# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Land Office Manager SINGLE-FILE portable build.

Build with:  .\\.env\\Scripts\\python.exe -m PyInstaller app.spec --noconfirm

Everything (Python runtime, DLLs, packages, the Flet desktop client archive)
is packed into ONE executable: dist/LandOfficeManager.exe. Copy that single
file anywhere and run it — no other files are needed.

The database is created and kept NEXT TO THE EXE (core/config.py resolves
the data directory from the executable location when frozen, so the
portable app carries its own SQLite file). The Settings page can switch to
a different database at any time; SQLite paths there must be absolute,
which keeps the switch working no matter where the exe was launched from.

Note: a one-file build extracts itself to a temp folder at every start, so
the first window appears a few seconds slower than an onedir build.
"""

block_cipher = None

first_party_modules = [
    "core.__init__",
    "core.area_engine",
    "core.config",
    "core.database",
    "core.database_config",
    "core.database_copy",
    "core.models",
    "core.search",
    "core.security",
    "ui.__init__",
    "ui.app_shell",
    "ui.file_location",
    "ui.flet_compat",
    "ui.navigation",
    "ui.pages.__init__",
    "ui.pages.area_calculator",
    "ui.pages.clients",
    "ui.pages.dashboard",
    "ui.pages.dc_rates",
    "ui.pages.placeholder",
    "ui.pages.settings",
    "ui.pages.valuation",
    "services.__init__",
    "services.area_export_service",
    "services.area_history_service",
    "services.client_service",
    "services.dc_rate_duplicate_service",
    "services.dc_rate_service",
    "services.estimate_service",
    "services.registry_fee_service",
    "services.schedule_lookup_service",
    "services.settings_service",
    "services.tax_service",
    "services.valuation_report_service",
    "services.valuation_service",
]

third_party_modules = [
    # Flet desktop runtime + assets (the client window).
    "flet",
    "flet_desktop",
    "flet_desktop.version",
    "flet_desktop.win_taskbar",
    "flet_platform_assets",
    # Spreadsheet / report extras.
    "odf",
    "odf.element",
    "odf.grammar",
    "odf.attrconverters",
    "odf.namespaces",
    "odf.opendocument",
    "odf.style",
    "odf.table",
    "odf.text",
    "odf.teletype",
    "arabic_reshaper",
    "arabic_reshaper.config",
    "bidi",
    "bidi.algorithm",
    # Database driver (optional in source, required by the default build).
    "sqlite3",
]

hiddenimports = first_party_modules + third_party_modules

a = Analysis(
    ["app.py"],
    pathex=["."],
    binaries=[],
    datas=[
        # The Flet desktop client archive: bundled so the portable app does
        # not need a one-time download on first start.
        (
            ".env/Lib/site-packages/flet_desktop/app/flet-windows.zip",
            "flet_desktop/app",
        ),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Large frameworks the app never imports.
        "tkinter",
        "matplotlib",
        "numpy",
        "pandas",
        # NOTE: PIL/Pillow must NOT be excluded — flet_platform_assets and
        # reportlab import it at module top level, and excluding it broke
        # the frozen startup ("excluded module named PIL" killed the app).
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# SINGLE-FILE build: all binaries, packages, and data are packed into the
# EXE itself (no _internal folder, no COLLECT step). At runtime the bundle
# extracts to a temp folder (sys._MEIPASS) while the database is created
# next to the real exe (core/config.py uses sys.executable when frozen).
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="LandOfficeManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
