"""Symmetric encryption helpers for sensitive settings stored in the DB.

**The database holds no key material.**  Both of this app's keys resolve the
same way — an environment variable, else a 0600 file in ``DATA_DIR`` — so a
stolen DB backup contains ciphertext, password hashes, and nothing that opens
either.  The encryption key is here; the JWT signing key is
``auth.get_secret_key`` (``DATA_DIR/signing.key``).

Resolution order for the encryption key:

1. ``SHELF_ENCRYPTION_KEY`` env var, if set (recommended for deployments —
   the DB *and* the data dir are then useless without it).
2. A key file at ``DATA_DIR/encryption.key``, auto-generated on first use
   with 0600 permissions.  The backup download is ``VACUUM INTO`` (DB only),
   so the key never rides along in a backup.

Historically the key was derived from the JWT secret (which itself lived in
the DB until 0.30); ``migrate_sensitive_settings()`` re-encrypts such legacy
values on startup, opening them with ``get_secret_key()`` — which is why the
signing key's relocation preserves its *value* rather than regenerating it.

Values are stored as Fernet tokens (base64url, self-describing, with HMAC
authentication).  Plaintext values (pre-migration) are detected by the
absence of the Fernet token prefix and transparently re-encrypted on read.
A token that *is* well-formed but will not open under the current key is a
key mismatch, not legacy plaintext: ``decrypt_value`` warns once per setting
and reads as unset rather than handing ciphertext to a provider.
"""

import base64
import hashlib
import logging
import os
import secrets

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# All Fernet tokens are base64url(0x80 || timestamp || ...), so they share
# this prefix; used to tell ciphertext from legacy plaintext values.
_FERNET_PREFIX = "gAAAAA"

# Settings keys that contain third-party credentials — encrypted at rest
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "abs_token",
        "komga_api_key",
        "romm_api_token",
        # Both native and retained fork credential names are protected at rest.
        "romm_token",
        "anthropic_api_key",
        "openai_api_key",
        "hardcover_token",
        "google_books_api_key",
        "librarything_token",
        "isbndb_api_key",
        "tmdb_api_key",
        "igdb_client_id",
        "igdb_client_secret",
        "discogs_token",
        # An ntfy topic URL is effectively a credential — anyone holding it
        # can post to (and often read) the topic
        "notify_url",
    }
)


_cached_encryption_key: str | None = None

# Setting keys already warned about for an undecryptable value, so a wrong
# encryption key costs one log line per setting rather than one per request.
# Reset per test in tests/conftest.py's _isolated_db (G13).
_warned_undecryptable: set[str] = set()


def _keyfile_path(name: str):
    from app import config  # attribute lookup at call time — tests patch DATA_DIR
    return config.DATA_DIR / name


def _read_keyfile(name: str) -> str | None:
    """Return the key in *name*, or None if it is absent, empty or unreadable.

    None means "no usable key file" — the caller falls through to its next
    source rather than distinguishing the three cases, because the remedy is
    the same for all of them: write a good file.
    """
    try:
        return _keyfile_path(name).read_text().strip() or None
    except OSError:
        return None


def _write_keyfile(name: str, value: str) -> None:
    """Create the key file *name* holding *value*, 0600.

    Raises OSError on any failure — a read-only volume, a full disk, wrong
    ownership.  Nothing is swallowed here: the caller decides what a failed
    write means, and for the signing key that decision (keep using the
    database row) is the difference between a hardening step and an outage.

    An existing but *unusable* file (empty or truncated by an interrupted
    write) is truncated and rewritten; callers only reach this with a file
    ``_read_keyfile`` has already rejected.
    """
    key_file = _keyfile_path(name)
    try:
        fd = os.open(str(key_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        fd = os.open(str(key_file), os.O_WRONLY | os.O_TRUNC)
        os.fchmod(fd, 0o600)
    try:
        os.write(fd, value.encode())
    finally:
        os.close(fd)


def _unlink_keyfile(name: str) -> None:
    """Best-effort removal of a key file.  Never raises."""
    try:
        _keyfile_path(name).unlink()
    except OSError:
        pass


def _read_or_create_keyfile(name: str) -> str:
    """Read the key file *name*, creating it atomically (0600) if missing."""
    key_file = _keyfile_path(name)
    try:
        fd = os.open(str(key_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        key = key_file.read_text().strip()
        if key:
            return key
        # Truncated/empty file from an interrupted first write — regenerate
        key = secrets.token_hex(32)
        key_file.write_text(key)
        key_file.chmod(0o600)
        return key
    try:
        key = secrets.token_hex(32)
        os.write(fd, key.encode())
    finally:
        os.close(fd)
    return key


def get_encryption_key() -> str:
    """Encryption key for sensitive settings — env var, else local key file."""
    global _cached_encryption_key
    if _cached_encryption_key:
        return _cached_encryption_key
    key = os.environ.get("SHELF_ENCRYPTION_KEY", "").strip() or _read_or_create_keyfile("encryption.key")
    _cached_encryption_key = key
    return key


def get_existing_encryption_key() -> str:
    """Read the authoritative destination key WITHOUT creating/replacing it.

    Restore is not a key-initialization path. Honor the already active process
    key, then the normal external sources, but never generate a missing key.
    """
    key = (_cached_encryption_key or os.environ.get("SHELF_ENCRYPTION_KEY", "").strip()
           or _read_keyfile("encryption.key"))
    if not key:
        raise ValueError("Restore requires an existing destination encryption key")
    return key


def is_fernet_token(value: str) -> bool:
    """True if *value* looks like Fernet ciphertext rather than plaintext."""
    return value.startswith(_FERNET_PREFIX)


def _fernet(secret_key: str) -> Fernet:
    """Return a Fernet instance keyed from the app secret."""
    raw = hashlib.sha256(secret_key.encode()).digest()  # 32 bytes
    key = base64.urlsafe_b64encode(raw)
    return Fernet(key)


def encrypt_value(plaintext: str, secret_key: str) -> str:
    """Encrypt *plaintext* and return a Fernet token string."""
    if not plaintext:
        return plaintext
    return _fernet(secret_key).encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str, secret_key: str, *, key_name: str | None = None) -> str:
    """Decrypt a Fernet token.  Returns the original plaintext on success.

    Two failure arms, deliberately distinguishable:

    * *ciphertext* is **not** a Fernet token — a legacy plaintext value stored
      before encryption was introduced.  Returned unchanged, logged at debug.
      This is the only silent arm.
    * *ciphertext* **is** a Fernet token that will not open under *secret_key* —
      a key mismatch (a replaced or lost ``encryption.key``).  Returns ``""``
      rather than the ciphertext, and warns **once per key name per process**
      naming the setting.  Returning the ciphertext would hand a provider a
      garbage credential and read as a revoked key; every consumer already
      treats an empty credential as "not configured" and skips the call.

    The value is never logged — only *key_name*.  ``get_setting`` runs on every
    request, hence the once-per-key set: a warning per request would flood
    ``log_entries`` through ``SQLiteHandler``.
    """
    if not ciphertext:
        return ciphertext
    if not is_fernet_token(ciphertext):
        # Legacy plaintext value — callers should re-encrypt on next write
        logger.debug("decrypt_value: not a Fernet token, treating as plaintext")
        return ciphertext
    try:
        return _fernet(secret_key).decrypt(ciphertext.encode()).decode()
    except Exception:
        name = key_name or "?"
        if name not in _warned_undecryptable:
            _warned_undecryptable.add(name)
            logger.warning(
                "settings[%s]: stored ciphertext does not open under the current "
                "encryption key — re-enter the credential in Settings",
                name,
            )
        return ""


# --- Encrypted backup container ---------------------------------------------
# Layout: magic || 16-byte scrypt salt || 12-byte nonce || AES-256-GCM ciphertext.
# The magic doubles as GCM associated data, so a tampered header fails auth.

BACKUP_MAGIC = b"SHELFBAK1"
_BACKUP_SALT_LEN = 16
_BACKUP_NONCE_LEN = 12


def _backup_key(passphrase: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    return Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(passphrase.encode())


def is_encrypted_backup(data: bytes) -> bool:
    return data[: len(BACKUP_MAGIC)] == BACKUP_MAGIC


def encrypt_backup(data: bytes, passphrase: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt = os.urandom(_BACKUP_SALT_LEN)
    nonce = os.urandom(_BACKUP_NONCE_LEN)
    ciphertext = AESGCM(_backup_key(passphrase, salt)).encrypt(nonce, data, BACKUP_MAGIC)
    return BACKUP_MAGIC + salt + nonce + ciphertext


def decrypt_backup(data: bytes, passphrase: str) -> bytes:
    """Decrypt an encrypted backup. Raises ValueError on a wrong passphrase
    or a corrupted/tampered file."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not is_encrypted_backup(data):
        raise ValueError("Not an encrypted Shelf backup")
    offset = len(BACKUP_MAGIC)
    salt = data[offset: offset + _BACKUP_SALT_LEN]
    nonce = data[offset + _BACKUP_SALT_LEN: offset + _BACKUP_SALT_LEN + _BACKUP_NONCE_LEN]
    ciphertext = data[offset + _BACKUP_SALT_LEN + _BACKUP_NONCE_LEN:]
    try:
        return AESGCM(_backup_key(passphrase, salt)).decrypt(nonce, ciphertext, BACKUP_MAGIC)
    except InvalidTag:
        raise ValueError("Wrong passphrase or corrupted backup file")


def normalize_sensitive_settings(
    conn, *, encryption_key: str, signing_key: str, legacy_signing_keys: tuple[str, ...] = (),
) -> dict:
    """Normalize ONLY the supplied connection under explicit destination keys.

    No get_db, key resolution/generation, environment/global changes, commits or
    logging: a staging failure must not touch the serving database or key files.
    Backup signing keys are decryption candidates ONLY, never destination keys.
    Foreign/unknown ciphertext stays byte-for-byte encrypted and is reported by
    setting name so a restore cannot silently throw credentials away.
    """
    if not isinstance(encryption_key, str) or not encryption_key:
        raise ValueError("An explicit destination encryption key is required")
    if not isinstance(signing_key, str) or not signing_key:
        raise ValueError("An explicit destination signing key is required")
    if any(not isinstance(key, str) or not key for key in legacy_signing_keys):
        raise ValueError("Invalid legacy signing-key candidate")
    destination = _fernet(encryption_key)
    candidates = [_fernet(key) for key in dict.fromkeys((signing_key, *legacy_signing_keys))]
    rewritten = 0
    undecryptable = []
    for key, value in conn.execute("SELECT key, value FROM settings").fetchall():
        if key not in SENSITIVE_KEYS or not value:
            continue
        if not isinstance(value, str):
            raise ValueError(f"Invalid sensitive setting: {key}")
        if is_fernet_token(value):
            try:
                destination.decrypt(value.encode())
                continue
            except InvalidToken:
                pass
            for candidate in candidates:
                try:
                    plaintext = candidate.decrypt(value.encode()).decode()
                    break
                except (InvalidToken, UnicodeDecodeError):
                    continue
            else:
                undecryptable.append(key)
                continue
        else:
            plaintext = value
        conn.execute(
            "UPDATE settings SET value = ? WHERE key = ?",
            (encrypt_value(plaintext, encryption_key), key),
        )
        rewritten += 1
    return {"rewritten": rewritten, "undecryptable": sorted(undecryptable)}


def migrate_sensitive_settings() -> int:
    """Move sensitive settings onto the dedicated encryption key.

    Re-encrypts values still encrypted under the legacy JWT-derived key, and
    encrypts any legacy plaintext values.  Idempotent — runs on every
    startup, which also covers backups restored in place (restore requires a
    restart).  Values that decrypt with neither key (e.g. a backup from a
    different install) are left untouched and logged.

    Returns the number of rows rewritten.
    """
    from app.auth import get_secret_key
    from app.database import get_db

    new_key = get_encryption_key()
    legacy_key = get_secret_key()
    with get_db() as db:
        result = normalize_sensitive_settings(db, encryption_key=new_key, signing_key=legacy_key)
    # Log only after the transaction closes; SQLiteHandler may open a live DB.
    for key in result["undecryptable"]:
        logger.warning(
            "settings[%s]: undecryptable with current or legacy key; "
            "left as-is — re-enter the credential in Settings", key,
        )
    migrated = result["rewritten"]
    if migrated:
        logger.info("Re-encrypted %d sensitive setting(s) with the dedicated encryption key", migrated)
    return migrated
