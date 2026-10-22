"""Encryption helpers for client portal credentials."""

import os

from cryptography.fernet import Fernet, InvalidToken

from core.config import DATA_DIR

KEY_ENV_VAR = "LANDOFFICE_FERNET_KEY"
KEY_PATH = DATA_DIR / ".credentials.key"


def _fernet() -> Fernet:
    """Load the configured Fernet key, creating a local key on first use."""
    configured = os.environ.get(KEY_ENV_VAR)
    if configured:
        key = configured.encode("ascii")
    else:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            # Exclusive creation avoids replacing a key if two app instances
            # start at once. The database and key are ignored by git.
            descriptor = os.open(
                KEY_PATH,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            key = KEY_PATH.read_bytes().strip()
        else:
            key = Fernet.generate_key()
            with os.fdopen(descriptor, "wb") as key_file:
                key_file.write(key)
            try:
                os.chmod(KEY_PATH, 0o600)
            except OSError:
                # Windows ACLs govern file access; chmod is best effort there.
                pass

    try:
        return Fernet(key)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            f"{KEY_ENV_VAR} or {KEY_PATH} must contain a valid Fernet key."
        ) from exc


def encrypt_secret(value: str) -> bytes:
    """Encrypt a credential before it is written to the client row."""
    return _fernet().encrypt(value.encode("utf-8"))


def decrypt_secret(value: bytes | None) -> str | None:
    """Decrypt a saved credential only when an authorized UI requests it."""
    if value is None:
        return None
    try:
        return _fernet().decrypt(value).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError(
            "Credential could not be decrypted. Check the configured key."
        ) from exc


def generate_secure_password(length: int = 20) -> str:
    """Return a cryptographically random password with mixed character sets."""
    import secrets
    import string

    if not 12 <= length <= 64:
        raise ValueError("Password length must be between 12 and 64.")

    groups = [
        string.ascii_lowercase,
        string.ascii_uppercase,
        string.digits,
        "!@#$%^&*()-_=+",
    ]
    characters = [secrets.choice(group) for group in groups]
    alphabet = "".join(groups)
    characters.extend(
        secrets.choice(alphabet) for _ in range(length - len(characters))
    )
    secrets.SystemRandom().shuffle(characters)
    return "".join(characters)
