"""Short-lived, user/item-bound reviews; only selected catalogue fields change."""

import hashlib
import hmac
import json
import time

import jwt

from app import auth
from app.config import JWT_ALGORITHM

FIELD_LABELS = {"title": "Title", "authors": "Artist", "publisher": "Label", "publish_year": "Year",
                "upc": "Barcode", "description": "Description"}
MAX_TICKET = 120000


class ReviewError(ValueError):
    pass


def _key():
    # Separate this token family cryptographically from login/session JWTs.
    return hmac.new(auth.get_secret_key().encode(), b"shelf/music-review/v1", hashlib.sha256).digest()


def fingerprint(db, item_id):
    if not item_id:
        return "new"
    row = db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone()
    if not row:
        raise ReviewError("This item is no longer available.")
    from app.services import cover_sync_policy, music_catalog
    payload = [dict(row), music_catalog.get_release(db, item_id), cover_sync_policy.decision(db, item_id)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def sign(candidate, *, user_id, item_id, fingerprint, purpose="shelf-music-review-v1"):
    payload = {"purpose": purpose, "sub": str(user_id), "item": item_id,
               "fingerprint": fingerprint, "candidate": candidate, "exp": int(time.time()) + 900}
    ticket = jwt.encode(payload, _key(), algorithm=JWT_ALGORITHM)
    if len(ticket) > MAX_TICKET:
        raise ReviewError("That release contains too much metadata to preview safely.")
    return ticket


def verify(ticket, *, user_id, purpose="shelf-music-review-v1"):
    if not isinstance(ticket, str) or len(ticket) > MAX_TICKET:
        raise ReviewError("Reload the match preview and try again.")
    try:
        payload = jwt.decode(ticket, _key(), algorithms=[JWT_ALGORITHM],
                             options={"require": ["exp", "sub", "purpose", "item", "candidate", "fingerprint"]})
        if (payload["purpose"] != purpose or payload["sub"] != str(user_id)
                or type(payload["item"]) is not int or not 0 <= payload["item"] <= 2**63 - 1
                or not isinstance(payload["candidate"], dict)):
            raise ValueError
        return payload
    except (jwt.InvalidTokenError, ValueError, TypeError):
        raise ReviewError("That preview expired or changed. Preview the match again.") from None


def fields_for(release):
    date = release.get("release_date") or release.get("first_release_date") or ""
    year = int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None
    from app.services import upc
    barcode_ok, barcode = upc.canonical_retail_barcode(release.get("barcode") or "")
    return {key: value for key, value in {
        "title": release.get("title"), "authors": release.get("artist_credit"), "publisher": release.get("label"),
        "publish_year": year, "upc": barcode if barcode_ok else None,
    }.items() if value not in (None, "")}
