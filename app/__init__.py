"""Shelf application package integration wiring."""

# Upstream 0.34's decrypt_value() deliberately accepts key_name so a lost or
# replaced encryption key can warn once per affected setting without ever
# logging the secret value. During the fork integration the database readers
# retained their older call shape and therefore reported every failure as
# settings[?]. Patch the public database helpers at package import time until
# their source definitions are folded together.
from app import database as _database


def _get_setting_with_named_decrypt(db, key: str) -> str:
    from app.config import get_setting_value
    from app.crypto import SENSITIVE_KEYS, decrypt_value, get_encryption_key

    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    raw = row["value"] if row else None
    if raw and key in SENSITIVE_KEYS:
        raw = decrypt_value(raw, get_encryption_key(), key_name=key)
    return get_setting_value(key, raw)


def _get_all_settings_with_named_decrypt(db) -> dict[str, str]:
    from app.config import get_setting_value
    from app.crypto import SENSITIVE_KEYS, decrypt_value, get_encryption_key

    rows = db.execute("SELECT key, value FROM settings").fetchall()
    secret = get_encryption_key()
    settings = {}
    for row in rows:
        key = row["key"]
        value = row["value"]
        if value and key in SENSITIVE_KEYS:
            value = decrypt_value(value, secret, key_name=key)
        settings[key] = value
    return {key: get_setting_value(key, value) for key, value in settings.items()}


_database.get_setting = _get_setting_with_named_decrypt
_database.get_all_settings = _get_all_settings_with_named_decrypt
