"""Add-host entry actors, strict intent values and purpose-separated markers.

This is not an authentication resolver. In particular revalidate_actor only
uses its caller's connection, including when that caller owns a writer lock.
"""

from dataclasses import asdict, fields, replace
import math
import re
import time

import jwt

from app import auth
from app.add_media_types import CapturedAddActor, CapturedAddIntent
from app.config import BOOK_MEDIA_TYPES, MEDIA_TYPES, MUSIC_MEDIA_TYPES
from app.services import isbn, music_review, upc

CONTEXT_PURPOSE = "shelf-add-context-v1"
SELECTION_PURPOSE = "shelf-add-selection-v1"
CATALOGUE_PURPOSE = "shelf-add-catalogue-review-v1"
MUSIC_PURPOSE = "shelf-add-music-review-v1"
RECEIPT_PURPOSE = "shelf-add-receipt-v1"
MAX_CONTEXT = 4096
MAX_SELECTION = 6000
_ACTOR_KEYS = frozenset(field.name for field in fields(CapturedAddActor))
_INTENT_KEYS = frozenset(field.name for field in fields(CapturedAddIntent))
_REVIEW_KEYS = {"purpose", "sub", "item", "fingerprint", "candidate", "exp"}


class AddMediaError(music_review.ReviewError):
    """A fixed Add-host outcome, never provider exception text."""

    def __init__(self, code="stale_review", status_code=409):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class ActorChanged(AddMediaError):
    def __init__(self):
        super().__init__("actor_changed", 401)


class ProviderUnavailable(AddMediaError):
    def __init__(self, outcome):
        super().__init__("unresolved", 200)
        self.outcome = outcome if outcome in {
            "no_credential", "rejected", "rate_limited", "transport_failed",
            "search_timeout", "unresolved", "no_match",
        } else "unresolved"


def _live_expiry(value):
    try:
        return (type(value) in (int, float) and math.isfinite(value)
                and value > 0 and value > time.time())
    except OverflowError:
        return False


def _actor(value):
    if (not isinstance(value, dict) or set(value) != _ACTOR_KEYS
            or type(value["id"]) is not int or not 0 < value["id"] <= 2**63 - 1
            or not isinstance(value["username"], str) or not value["username"]
            or not isinstance(value["role"], str) or value["role"] not in {"editor", "admin"}
            or type(value["token_version"]) is not int or value["token_version"] < 1
            or not _live_expiry(value["expires_at"])):
        raise ActorChanged()
    return CapturedAddActor(**value)


def _same_identity(left, right):
    return (left.id == right.id and left.username.casefold() == right.username.casefold()
            and left.role == right.role and left.token_version == right.token_version)


def capture_actor(request, user) -> CapturedAddActor:
    """Capture the verified entry cookie BEFORE any body/provider await."""
    try:
        payload = auth.decode_token(request.cookies.get("access_token", ""))
    except (TypeError, ValueError, OverflowError):
        raise ActorChanged() from None
    if not isinstance(payload, dict) or not isinstance(user, dict) or not _live_expiry(payload.get("exp")):
        raise ActorChanged()
    actor = _actor({key: user.get(key) for key in _ACTOR_KEYS - {"expires_at"}}
                   | {"expires_at": payload["exp"]})
    try:
        same_id = int(payload["sub"]) == actor.id
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ActorChanged() from None
    version = payload.get("tv", 1)  # native pre-version cookies remain valid
    if (not same_id or type(version) is not int or version != actor.token_version
            or str(payload.get("username", "")).casefold() != actor.username.casefold()):
        raise ActorChanged()
    return actor


def revalidate_actor(db, actor) -> None:
    """No key resolution, logging, await or second connection here."""
    if not isinstance(actor, CapturedAddActor):
        raise ActorChanged()
    _actor(asdict(actor))
    row = db.execute(
        "SELECT username, role, token_version FROM users WHERE id = ?", (actor.id,),
    ).fetchone()
    if (not row or row["username"].casefold() != actor.username.casefold()
            or row["role"] != actor.role or row["token_version"] != actor.token_version):
        raise ActorChanged()


def bound_actor(value, actor) -> CapturedAddActor:
    """A ticket's original deadline may only be shortened by the entry cookie."""
    if not isinstance(actor, CapturedAddActor):
        raise ActorChanged()
    captured = _actor(value)
    _actor(asdict(actor))
    if not _same_identity(captured, actor) or captured.expires_at < actor.expires_at:
        raise ActorChanged()
    return actor


def original_actor(value, entry_actor) -> CapturedAddActor:
    """Both the marker's original expiry AND the entry cookie must be live."""
    if not isinstance(entry_actor, CapturedAddActor):
        raise ActorChanged()
    captured = _actor(value)
    _actor(asdict(entry_actor))
    if not _same_identity(captured, entry_actor):
        raise ActorChanged()
    # Carry the earlier deadline into every later await and the actual write
    # lock. Checking the entry cookie only at form parsing would let a longer
    # lived old marker outlive a shorter lived current cookie during lookup.
    # This never renews a marker; all derived markers can only expire earlier.
    return replace(captured, expires_at=min(captured.expires_at, entry_actor.expires_at))


def strict_isbn(value) -> str:
    if not isinstance(value, str) or len(value) > 32 or not re.fullmatch(
            r"(?:[0-9](?:[ -]*[0-9]){8}[ -]*[0-9Xx]|[0-9](?:[ -]*[0-9]){12})", value):
        raise AddMediaError("invalid_input", 422)
    compact = value.replace(" ", "").replace("-", "").upper()
    # Validate the ORIGINAL ISBN-10 before the deliberately permissive conversion.
    if len(compact) == 10:
        if not isbn.validate_isbn10(compact):
            raise AddMediaError("invalid_input", 422)
        return isbn.isbn10_to_isbn13(compact)
    if not isbn.validate_isbn13(compact):
        raise AddMediaError("invalid_input", 422)
    return compact


def strict_barcode(value) -> str:
    if not isinstance(value, str) or len(value) > 32 or not re.fullmatch(
            r"[0-9](?:[ -]*[0-9]){11,12}", value):
        raise AddMediaError("invalid_input", 422)
    compact = value.replace(" ", "").replace("-", "")
    if ((len(compact) == 12 and upc.validate_upc(compact))
            or (len(compact) == 13 and not compact.startswith(("978", "979"))
                and upc.validate_ean13(compact))):
        return compact
    raise AddMediaError("invalid_input", 422)


def intent_from_dict(value) -> CapturedAddIntent:
    """Closed, complete intent; no aliases, silent defaults or guessed identifiers."""
    if not isinstance(value, dict) or set(value) != _INTENT_KEYS:
        raise AddMediaError("intent_changed")
    if any(not isinstance(v, str) or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in v)
           for v in value.values()):
        raise AddMediaError("invalid_input", 422)
    intent = CapturedAddIntent(**value)
    if (intent.mode not in {"add", "wishlist"} or intent.media_type not in MEDIA_TYPES
            or intent.method not in {"title", "isbn", "barcode", "catalog_number", "manual"}
            or intent.music_provider not in {"auto", "musicbrainz", "discogs"}):
        raise AddMediaError("invalid_input", 422)
    music = intent.media_type in MUSIC_MEDIA_TYPES
    maximum = 100 if intent.method == "catalog_number" else (32 if intent.method in {"isbn", "barcode"} else 200)
    if (len(intent.query) > maximum or len(intent.artist) > 200 or len(intent.title_hint) > 200
            or len(intent.platform) > 100 or len(intent.country) > 80
            or (not music and (intent.music_provider != "auto" or intent.artist or intent.title_hint or intent.country))
            or (intent.media_type != "video_game" and intent.platform)):
        raise AddMediaError("invalid_input", 422)
    if music and intent.music_provider != "discogs" and intent.country and not re.fullmatch(r"[A-Za-z]{2}", intent.country):
        raise AddMediaError("invalid_input", 422)
    if intent.method == "isbn":
        if intent.media_type not in BOOK_MEDIA_TYPES:
            raise AddMediaError("invalid_input", 422)
        strict_isbn(intent.query)
    elif intent.method == "barcode":
        strict_barcode(intent.query)
    elif intent.method == "catalog_number":
        if not music or not intent.query.strip():
            raise AddMediaError("invalid_input", 422)
    elif intent.method == "title" and not (intent.query.strip() or (music and intent.artist.strip())):
        raise AddMediaError("invalid_input", 422)
    return intent


def intent_dict(intent):
    if not isinstance(intent, CapturedAddIntent):
        raise AddMediaError("intent_changed")
    value = asdict(intent)
    intent_from_dict(value)
    return value


def verify_host_ticket(token, *, actor, purpose, limit=music_review.MAX_TICKET):
    """The Add host's fixed uses of the unchanged native review signer."""
    if not isinstance(token, str) or not token.isascii() or not token or len(token) > limit:
        raise AddMediaError()
    if not isinstance(actor, CapturedAddActor):
        raise ActorChanged()
    _actor(asdict(actor))
    try:
        review = music_review.verify(token, user_id=actor.id, purpose=purpose)
    except music_review.ReviewError:
        raise AddMediaError() from None
    if (set(review) != _REVIEW_KEYS or review["item"] != 0 or review["fingerprint"] != "new"
            or not _live_expiry(review["exp"])):
        raise AddMediaError()
    return review["candidate"]


def sign_context(actor, intent=None) -> str:
    if not isinstance(actor, CapturedAddActor):
        raise ActorChanged()
    _actor(asdict(actor))
    candidate = {"actor": asdict(actor), "intent": intent_dict(intent) if intent is not None else None}
    token = music_review.sign(candidate, user_id=actor.id, item_id=0, fingerprint="new", purpose=CONTEXT_PURPOSE)
    if len(token) > MAX_CONTEXT or not token.isascii():
        raise AddMediaError("invalid_input", 422)
    return token


def verify_context(token, entry_actor):
    if not isinstance(token, str) or not token.isascii() or not token or len(token) > MAX_CONTEXT:
        raise AddMediaError()
    try:
        # This bounded subject is only INPUT to the unchanged native verifier.
        # It grants no authority: authenticate first, then check the signed
        # actor/subject agreement and finally bind that actor to the entry.
        subject = jwt.decode(token, options={"verify_signature": False}).get("sub")
        if not isinstance(subject, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", subject) or int(subject) > 2**63 - 1:
            raise ValueError
        review = music_review.verify(token, user_id=subject, purpose=CONTEXT_PURPOSE)
    except (jwt.InvalidTokenError, music_review.ReviewError, ValueError, TypeError, OverflowError):
        raise AddMediaError() from None
    if (set(review) != _REVIEW_KEYS or review["item"] != 0 or review["fingerprint"] != "new"
            or not _live_expiry(review["exp"])):
        raise AddMediaError()
    candidate = review["candidate"]
    if set(candidate) != {"actor", "intent"}:
        raise AddMediaError()
    value = candidate["actor"]
    try:
        # Malformed signed context is stale; a well-formed original session
        # deadline that has elapsed is separately ActorChanged below.
        if (not isinstance(value, dict) or set(value) != _ACTOR_KEYS
                or type(value["id"]) is not int or not 0 < value["id"] <= 2**63 - 1
                or review["sub"] != str(value["id"])
                or not isinstance(value["username"], str) or not value["username"]
                or not isinstance(value["role"], str) or value["role"] not in {"editor", "admin"}
                or type(value["token_version"]) is not int or value["token_version"] < 1
                or type(value["expires_at"]) not in (int, float) or not math.isfinite(value["expires_at"])
                or value["expires_at"] <= 0):
            raise AddMediaError()
        intent = intent_from_dict(candidate["intent"]) if candidate["intent"] is not None else None
    except (AddMediaError, OverflowError):
        raise AddMediaError() from None
    captured = original_actor(value, entry_actor)
    return captured, intent
