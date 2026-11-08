"""Database connection configuration, stored outside the database.

The user can point the application at any supported SQL backend (SQLite file,
PostgreSQL, MySQL/MariaDB, Microsoft SQL Server, or an arbitrary SQLAlchemy
URL). Because this choice determines *which* database is opened, it cannot be
stored inside the ``app_settings`` table — it lives in ``data/database.json``
next to the data instead.

Design notes:
* ``load_options`` is defensive: a missing, empty, or malformed file falls
  back to the built-in SQLite default so the app always starts.
* Connection passwords are never written as plain text. They are encrypted
  with the same machine-local Fernet key that protects client portal
  credentials (``core.security``) and are prefixed ``enc:`` in the file.
* Secrets never appear in error messages or logs: SQLAlchemy already masks
  the password when a URL is rendered, and this module strips connection
  errors to a single trailing line.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

from core.config import DATABASE_PATH, DATA_DIR
from core.security import encrypt_secret, decrypt_secret

#: Location of the connection preference file. Tests may monkeypatch this.
CONFIG_FILE_PATH = DATA_DIR / "database.json"

#: Prefix marking an encrypted password inside the JSON file.
_ENCRYPTED_PREFIX = "enc:"

#: User-facing backend choices. ``dialect``/``driver`` describe the
#: SQLAlchemy dialect and the optional driver package each preset needs.
_DATABASE_BACKENDS: dict[str, dict[str, Any]] = {
    "sqlite": {
        "label": "SQLite file",
        "dialect": "sqlite",
        "portable": True,
    },
    "postgres": {
        "label": "PostgreSQL",
        "dialect": "postgresql",
        "default_port": 5432,
        # Candidates in preference order; the first importable one wins.
        "driver_candidates": {
            "psycopg": "psycopg",
            "psycopg2": "psycopg2",
        },
        "install_hint": "pip install psycopg2-binary (or psycopg[binary])",
    },
    "mysql": {
        "label": "MySQL / MariaDB",
        "dialect": "mysql",
        "default_port": 3306,
        "driver_candidates": {"pymysql": "pymysql"},
        "install_hint": "pip install pymysql",
    },
    "mssql": {
        "label": "Microsoft SQL Server",
        "dialect": "mssql",
        "default_port": 1433,
        "driver_candidates": {"pyodbc": "pyodbc"},
        "install_hint": "pip install pyodbc (plus a Microsoft ODBC driver)",
        "needs_odbc_driver": True,
    },
    "custom": {
        "label": "Custom SQLAlchemy URL",
        "dialect": "",
        "portable": True,
    },
}


class DatabaseConfigError(ValueError):
    """Raised when database connection options are not usable."""


def backend_keys() -> list[str]:
    """Return the supported backend keys in offering order."""
    return list(_DATABASE_BACKENDS)


def backend_label(key: str) -> str:
    """Return the display name of a backend key."""
    return _DATABASE_BACKENDS.get(
        key, {"label": key or "Unsupported backend"}
    )["label"]


def default_options() -> dict[str, Any]:
    """Return the built-in default options (the original SQLite file).

    The SQLite path is resolved to an absolute string so a relative entry in
    the JSON file cannot silently depend on the launch directory.
    """
    return {
        "backend": "sqlite",
        "path": str(Path(DATABASE_PATH)),
        "host": "",
        "port": "",
        "database": "",
        "user": "",
        "password": "",
        "driver": "",
        "odbc_driver": "",
        "url": "",
    }


def load_options() -> dict[str, Any]:
    """Read the saved connection options, falling back to the defaults.

    A missing file, unreadable JSON, or unknown backend each return the
    default SQLite configuration instead of raising, because the engine
    builder must always be able to start. ``password`` stays encrypted in the
    returned dictionary until :func:`build_url` needs it.
    """
    options = default_options()
    try:
        stored = json.loads(CONFIG_FILE_PATH.read_text(encoding="utf-8"))
        if isinstance(stored, dict):
            options.update({
                key: stored[key]
                for key in default_options()
                if key in stored and stored[key] is not None
            })
    except json.JSONDecodeError:
        # A partially written file (crash during save) must not brick the app.
        pass
    except OSError:
        # No file or unreadable volume: first launch is the common case.
        pass
    if options.get("backend") not in _DATABASE_BACKENDS:
        options["backend"] = "sqlite"
    return options


def save_options(options: dict[str, Any]) -> dict[str, Any]:
    """Validate the options and write the configuration file atomically.

    The file is written to a temporary file first and then replaced, so a
    crash mid-write cannot leave a truncated JSON behind. The password is
    stored encrypted (:func:`encrypt_secret` with the machine-local key);
    writing a database password as plain text would weaken every export and
    backup workflow.
    """
    normalized = normalize_options(options)
    payload = dict(normalized)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_FILE_PATH.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, CONFIG_FILE_PATH)
    return normalized


def _encrypt_password(password: str) -> str:
    """Prefix-mask an encrypted password for JSON storage.

    The operation is idempotent on already-encrypted values: options loaded
    from the file keep their stored token when normalized again, so the same
    token is never doubly wrapped.
    """
    if not password:
        return ""
    if password.startswith(_ENCRYPTED_PREFIX):
        return password
    token = encrypt_secret(password)
    return _ENCRYPTED_PREFIX + base64.b64encode(token).decode("ascii")


def _decrypt_password(stored: Any) -> str:
    """Return a stored password's plain text, tolerating legacy plain text."""
    if not stored:
        return ""
    if not isinstance(stored, str):
        raise DatabaseConfigError("The stored database password is not text.")
    if not stored.startswith(_ENCRYPTED_PREFIX):
        # Entries written before encryption was added stay readable; they are
        # re-encrypted the next time options are saved.
        return stored
    try:
        token = base64.b64decode(stored[len(_ENCRYPTED_PREFIX):])
        return decrypt_secret(token) or ""
    except (ValueError, TypeError):
        return ""


def normalize_options(options: dict[str, Any]) -> dict[str, Any]:
    """Validate a connection-options dictionary and return its clean form.

    Rules:
    * every backend needs ``backend``; SQLite needs an absolute ``path`` whose
      parent folder exists; server backends need ``host``, ``database`` and
      ``user``; the custom backend needs a full ``url``;
    * ``port`` must be an integer between 1 and 65535 when supplied;
    * unknown keys are ignored so settings from a newer build never stop an
      older one from connecting.
    Raises:
        DatabaseConfigError: with one clear, secret-free message for the user.
    """
    if not isinstance(options, dict):
        raise DatabaseConfigError("Database options must be a mapping.")
    normalized = default_options()
    normalized.update({
        key: options[key]
        for key in normalized
        if options.get(key) is not None
    })
    backend = normalized["backend"]
    if backend not in _DATABASE_BACKENDS:
        raise DatabaseConfigError(
            f"Unknown database type '{backend}'. "
            f"Choose one of: {', '.join(backend_keys())}."
        )

    if backend == "sqlite":
        raw_path = str(normalized["path"] or "").strip()
        if not raw_path:
            raise DatabaseConfigError(
                "A SQLite database needs a file path."
            )
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            raise DatabaseConfigError(
                "The SQLite file path must be absolute. "
                f"Use something like {DATABASE_PATH}"
            )
        parent = candidate.parent
        if not parent.exists() or not parent.is_dir():
            raise DatabaseConfigError(
                f"The folder for this SQLite file does not exist: {parent}"
            )
        if candidate.exists() and candidate.is_dir():
            raise DatabaseConfigError(
                "The SQLite path is a folder, not a database file."
            )
        if candidate.resolve() == CONFIG_FILE_PATH.resolve():
            raise DatabaseConfigError(
                "The SQLite path must not be the settings file itself."
            )
        normalized["path"] = str(candidate)
    else:
        if backend == "custom":
            url = str(normalized["url"] or "").strip()
            if "://" not in url:
                raise DatabaseConfigError(
                    "A custom database must be a full SQLAlchemy URL, "
                    "for example postgresql+psycopg2://user:pass@host:5432/landoffice"
                )
            normalized["url"] = url
        else:
            for field in ("host", "database", "user"):
                required = str(normalized[field] or "").strip()
                if not required:
                    label = {"host": "server host", "database": "database name",
                             "user": "user name"}[field]
                    raise DatabaseConfigError(
                        f"A {backend_label(backend)} connection needs a {label}."
                    )
                normalized[field] = required
            port_text = str(normalized["port"] or "").strip()
            if port_text:
                try:
                    port = int(port_text)
                except ValueError as exc:
                    raise DatabaseConfigError(
                        "The port must be a number."
                    ) from exc
                if not 1 <= port <= 65535:
                    raise DatabaseConfigError(
                        "The port must be between 1 and 65535."
                    )
                normalized["port"] = str(port)
            else:
                normalized["port"] = ""
            odbc_driver = str(normalized["odbc_driver"] or "").strip()
            if _DATABASE_BACKENDS[backend].get("needs_odbc_driver") and not odbc_driver:
                # SQL Server identifies itself through an ODBC driver name.
                odbc_driver = "ODBC Driver 17 for SQL Server"
            normalized["odbc_driver"] = odbc_driver

    # Any non-empty password is encrypted here so it is never plain on disk.
    normalized["password"] = _encrypt_password(
        str(normalized["password"] or "")
    )
    return normalized


def _resolve_driver(backend: str, requested: str = "") -> tuple[str | None, str]:
    """Pick an importable DBAPI driver for the backend.

    Returns ``(dialect_name, install_hint)`` when a driver can be imported, or
    ``(None, hint)`` naming what the user must install. Driver support is
    never guessed: a missing package is a user-facing message, not a crash.
    """
    backend_spec = _DATABASE_BACKENDS.get(backend, {})
    try:
        if backend == "sqlite":
            import sqlite3 as _driver_module  # noqa: F401
            return "sqlite", ""
        if backend == "custom":
            # The caller's URL carries its own driver; import errors surface
            # naturally in test_connection with a readable message.
            return "", ""
        candidates = backend_spec.get("driver_candidates", {})
        hints = backend_spec.get("install_hint", "")
        source: dict[str, str] = dict(candidates)
        if requested:
            if requested not in candidates:
                raise DatabaseConfigError(
                    f"'{requested}' is not a recognised {backend_label(backend)} "
                    "driver. Leave the driver empty to auto-select."
                )
            source = {requested: candidates[requested]}
        for dialect_driver, module_name in source.items():
            try:
                __import__(module_name)
            except ImportError:
                continue
            return dialect_driver, ""
        return None, hints
    except ImportError as exc:  # pragma: no cover - defensive
        return None, str(exc)


def build_url(options: dict[str, Any]) -> str:
    """Build a SQLAlchemy connection URL from the options.

    Usernames and passwords are percent-encoded (`quote_plus`) because spaces
    or ``@`` characters in credentials are common. Custom URLs are returned
    verbatim; SQLite paths become ``sqlite:///...`` (three slashes plus the
    absolute path, which SQLAlchemy reads as a host-less local file).
    """
    normalized = normalize_options(options)
    backend = normalized["backend"]
    if backend == "sqlite":
        # Forward slashes make the URL portable across Windows/macOS/Linux.
        return f"sqlite:///{Path(normalized['path']).as_posix()}"
    if backend == "custom":
        return normalized["url"]
    password = _decrypt_password(normalized["password"])
    dialect, _hint = _resolve_driver(
        backend, str(normalized["driver"] or "")
    )
    if dialect is None:
        raise DatabaseConfigError(_driver_missing_message(backend, _hint))
    full_dialect = f"{_DATABASE_BACKENDS[backend]['dialect']}+{dialect}"
    host = normalized["host"]
    database = normalized["database"]
    credentials = (
        f"{quote_plus(normalized['user'])}"
        + (f":{quote_plus(password)}" if password else "")
    )
    netloc = f"{credentials}@{quote(host)}"
    if normalized["port"]:
        netloc += f":{normalized['port']}"
    url = f"{full_dialect}://{netloc}/{quote(database)}"
    if backend == "mssql":
        # ODBC drivers select the SQL Server protocol details; the value is
        # URL-encoded because ODBC names contain spaces.
        from urllib.parse import urlencode
        url += "?" + urlencode({"driver": normalized["odbc_driver"]})
    return url


def _driver_missing_message(backend: str, hint: str) -> str:
    """Return the install-hint sentence for a backend without a driver."""
    return (
        f"No database driver is installed for {backend_label(backend)}. "
        f"{backend_label(backend)} needs {hint}"
    )


def test_connection(options: dict[str, Any]) -> tuple[bool, str]:
    """Try to open one connection with the given options.

    Returns ``(True, description)`` on success or ``(False, message)`` on
    failure, with the message safe to show in the settings page. The test
    engine is disposed immediately so a server connection is never pooled.
    """
    from sqlalchemy import create_engine, text as sql_text

    try:
        url = build_url(options)
    except DatabaseConfigError as exc:
        return False, str(exc)

    driver_ok, hint = _resolve_driver(
        options.get("backend", ""),
        str(options.get("driver") or ""),
    )
    if driver_ok is None:
        return False, _driver_missing_message(options.get("backend", ""), hint)
    try:
        engine = create_engine(url)
        try:
            with engine.connect() as connection:
                connection.execute(sql_text("SELECT 1"))
            name = engine.dialect.name
        finally:
            engine.dispose()
    except ModuleNotFoundError as exc:
        return False, (
            f"The database driver '{exc.name}' is not installed. "
            "Install it with pip, then try again."
        )
    except Exception as exc:
        # SQLAlchemy masks passwords in URLs, but nested driver errors often
        # end with a platform message; show the final readable line only.
        message = str(exc).strip()
        last_line = message.splitlines()[-1] if message else "connection failed."
        return False, f"Could not connect: {last_line}"
    return True, f"Connected ({name} backend accepted a test query)."
