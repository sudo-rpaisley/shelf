"""Explicit ASIN review over existing items/provenance; no new schema or sync job.

Reviews reuse music_review's signed, expiring user/item/purpose-bound tickets.
Identity is always the stored ASIN; it is never adopted from a preview. Backfill
is strictly fill-only, including durable blank overrides from any saved source.
All writes run on the caller's BEGIN IMMEDIATE connection, never across HTTP.
"""

import hashlib
import hmac
import json
import math
import re
import time
from pathlib import Path

from app import auth, config, features, i18n
from app.services import audible_catalog, cover_sync_policy, item_write, music_review


ReviewError = music_review.ReviewError


class PermissionChanged(ReviewError):
    def __init__(self, message, status_code):
        self.status_code = status_code
        super().__init__(message)


REVIEW_PURPOSE = "shelf-audiobook-review-v1"
IMAGE_PURPOSE = "shelf-audiobook-image-v1"
PREVIEW_FORM_BYTES = 4096
REVIEW_FORM_BYTES = 130000
FIELD_LABELS = {"title": i18n.N_("Title"), "subtitle": i18n.N_("Subtitle"), "authors": i18n.N_("Authors"),
                "narrator": i18n.N_("Narrator"), "duration_mins": i18n.N_("Duration (minutes)"),
                "description": i18n.N_("Description"), "publisher": i18n.N_("Publisher"),
                "publish_year": i18n.N_("Publication year"), "language": i18n.N_("Language"),
                "series_name": i18n.N_("Series"), "series_position": i18n.N_("Series position")}
MAX_BASELINES = 256
_now = time.time


def form_limit(path):
    """Integration hook: enforce this BEFORE native-form CSRF parsing as well."""
    # Bound malformed ids too: CSRF parsing precedes FastAPI's Path validation.
    match = re.fullmatch(r"/api/audiobooks/[^/]+/metadata/(preview|apply)", path.rstrip("/"))
    return (PREVIEW_FORM_BYTES if match[1] == "preview" else REVIEW_FORM_BYTES) if match else None


def item(db, item_id):
    row = db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone()
    if not row or row["media_type"] != "audiobook":
        raise ReviewError(i18n.gettext("Audiobook not found."))
    return dict(row)


def _baselines(db, item_id):
    fields = (*FIELD_LABELS, "cover_path")
    rows = db.execute(
        "SELECT source_key, field_name, observed_at, "
        "CASE WHEN length(value_json) <= 131072 THEN value_json END AS value_json "
        "FROM item_metadata_provenance WHERE item_id = ? "
        f"AND field_name IN ({','.join('?' for _ in fields)}) ORDER BY source_key, field_name LIMIT ?",
        (item_id, *fields, MAX_BASELINES + 1),
    ).fetchall()
    if len(rows) > MAX_BASELINES or any(row["value_json"] is None for row in rows):
        raise ReviewError(i18n.gettext("This audiobook has too much saved provenance to review safely. Edit it directly instead."))
    return [dict(row) for row in rows]


def _snapshot(raw):
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) and "observed" in value else None
    except (ValueError, TypeError, RecursionError):
        return None


def _empty(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _overridden(current, baseline):
    return (baseline is None or bool(baseline.get("overridden"))
            or any(current != baseline[key] for key in ("local", "applied") if key in baseline))


def retained_artwork(item_id, *, staged_path=None):
    # Remove cover historically kept the file. A missing pointer is not proof
    # of an untouched blank; orphan cleanup must not undo that local decision.
    staged_name = Path(staged_path).name if staged_path else None
    return any(path.is_file() and path.name != staged_name for pattern in (f"{item_id}.jpg", f"{item_id}-*.jpg")
               for path in config.COVERS_DIR.glob(pattern))


def fingerprint(db, item_id, *, staged_path=None):
    current = item(db, item_id)
    payload = ["audiobook-review-v1", current, _baselines(db, item_id),
               cover_sync_policy.decision(db, item_id), retained_artwork(item_id, staged_path=staged_path)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def sign(candidate, *, user_id, item_id, fingerprint, image=False):
    return music_review.sign(candidate, user_id=user_id, item_id=item_id, fingerprint=fingerprint,
                             purpose=IMAGE_PURPOSE if image else REVIEW_PURPOSE)


def _valid_field(name, value):
    if name in audible_catalog.FIELD_LIMITS:
        return (isinstance(value, str) and bool(value.strip()) and len(value) <= audible_catalog.FIELD_LIMITS[name]
                and not any(ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF for c in value))
    if name in {"publish_year", "duration_mins"}:
        return type(value) is int and 1 <= value <= (9999 if name == "publish_year" else 1000000)
    return name == "series_position" and type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1000000


def verify(ticket, *, user_id, item_id, image=False):
    review = music_review.verify(ticket, user_id=user_id, purpose=IMAGE_PURPOSE if image else REVIEW_PURPOSE)
    candidate = review["candidate"]
    if (review["item"] != item_id or item_id <= 0 or not isinstance(review["fingerprint"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", review["fingerprint"])):
        raise ReviewError(i18n.gettext("That preview belongs to a different audiobook. Preview it again."))
    try:
        asin, market = audible_catalog.identity(candidate.get("asin"), candidate.get("marketplace"))
        images = candidate.get("images")
        if (candidate.get("kind") != "audible" or asin != candidate["asin"] or not isinstance(images, list)
                or len(images) > 1 or any(not audible_catalog.artwork_url(url) for url in images)):
            raise ValueError
        if not image:
            fields = candidate.get("fields")
            if (candidate.get("source_url") != audible_catalog.product_url(asin, market) or not isinstance(fields, dict)
                    or not fields.get("title") or any(key not in FIELD_LABELS or not _valid_field(key, value) for key, value in fields.items())):
                raise ValueError
    except (ValueError, TypeError, KeyError):
        raise ReviewError(i18n.gettext("That catalogue preview is not safe to apply. Preview it again.")) from None
    return review


def check_current(db, item_id, review, *, staged_path=None):
    current = item(db, item_id)
    asin, _ = audible_catalog.identity(current["asin"], review["candidate"]["marketplace"])
    if asin != review["candidate"]["asin"] or not hmac.compare_digest(fingerprint(db, item_id, staged_path=staged_path), review["fingerprint"]):
        raise ReviewError(i18n.gettext("This audiobook or its metadata decisions changed after the preview. Preview it again before applying anything."))
    return current


def authorize_apply(db, actor):
    """Revalidate request authority on the SAME write-locked connection.

    Route dependencies and request.state.user are pre-HTTP snapshots. Do not
    use get_current_user (another connection), a cached feature flag, or a
    signed preview as authority to install or disclose a catalogue review. The
    actor includes the verified entry session's expiry. Future item/
    household capability checks must join this guard under the same lock.
    """
    if not db.in_transaction:
        raise RuntimeError("Audiobook apply authorization requires the write lock")
    current = db.execute("SELECT username, role, token_version FROM users WHERE id = ?", (actor["id"],)).fetchone()
    if (current is None or current["token_version"] != actor.get("token_version")
            or type(actor.get("expires_at")) is not int or actor["expires_at"] <= _now()
            or not isinstance(actor.get("username"), str)
            or current["username"].casefold() != actor["username"].casefold()):
        raise PermissionChanged(i18n.gettext("Your session changed while this review was open. Nothing was saved; sign in and review again."), 401)
    if auth.ROLE_LEVELS.get(current["role"], 0) < auth.ROLE_LEVELS["editor"]:
        raise PermissionChanged(i18n.gettext("You no longer have permission to apply this review. Nothing was saved."), 403)
    flag = db.execute("SELECT value FROM settings WHERE key = ?", (features.setting_key("connected_library"),)).fetchone()
    if flag is not None and flag["value"] == "0":
        raise PermissionChanged(i18n.gettext("Connected library was turned off while this review was open. Nothing was saved."), 403)


def authorize_read(db, item_id, actor, previous):
    """One post-HTTP authority + item snapshot under the caller's write lock."""
    authorize_apply(db, actor)
    try:
        return check_current(db, item_id, previous)
    except (ReviewError, ValueError):
        # No error template may reveal an edited/trashed item or carry a stale
        # signed candidate. The caller returns a plain, non-disclosing refusal.
        raise PermissionChanged(i18n.gettext("This audiobook changed during lookup. Preview it again."), 404) from None


def backfill_fields(db, item_id, candidate):
    current = item(db, item_id)
    protected = {row["field_name"] for row in _baselines(db, item_id)
                 if _overridden(current[row["field_name"]], _snapshot(row["value_json"]))}
    result = {name for name in candidate["fields"] if _empty(current[name]) and name not in protected}
    # Never put one series' ordinal onto another series, even in sparse data.
    if ("series_position" in result and "series_name" not in result
            and current["series_name"] != candidate["fields"].get("series_name")):
        result.remove("series_position")
    # The mirror case is also ambiguous: an orphan local ordinal is NOT
    # evidence that the provider's series name belongs to that ordinal.
    if "series_name" in result and not _empty(current["series_position"]):
        result.remove("series_name")
    return result


def backfill_artwork_allowed(db, item_id, *, staged_path=None):
    current = item(db, item_id)
    return (_empty(current["cover_path"]) and not current["cover_review_dismissed"]
            and not cover_sync_policy.blocked(db, item_id) and not retained_artwork(item_id, staged_path=staged_path)
            and not any(row["field_name"] == "cover_path" and _overridden(current["cover_path"], _snapshot(row["value_json"]))
                        for row in _baselines(db, item_id)))


def choices(form, candidate):
    allowed = {"ticket", "fields", "image", "mode", "_csrf"}
    if set(form) - allowed or any(len(form.getlist(key)) != 1 for key in form if key != "fields") or len(form.getlist("ticket")) != 1:
        raise ReviewError(i18n.gettext("Submit only the audiobook review choices, not source values."))
    mode, image = form.get("mode", "selected"), form.get("image", "-1")
    chosen = form.getlist("fields")
    if (mode not in {"selected", "backfill"} or image not in {"-1", "0"}
            or len(chosen) != len(set(chosen)) or len(chosen) > len(FIELD_LABELS)
            or any(name not in candidate["fields"] for name in chosen) or (image == "0" and not candidate["images"])):
        raise ReviewError(i18n.gettext("Choose only the fields and artwork shown in the preview."))
    return mode, set(chosen), image == "0"


def apply(db, item_id, review, *, mode, chosen, path, actor):
    """Recheck under the caller's write lock, then apply one atomic review."""
    authorize_apply(db, actor)
    current = check_current(db, item_id, review, staged_path=path)
    candidate = review["candidate"]
    if mode == "backfill":
        chosen = backfill_fields(db, item_id, candidate)
        if path and not backfill_artwork_allowed(db, item_id, staged_path=path):
            raise ReviewError(i18n.gettext("Backfill keeps existing or manually removed artwork. Choose Keep current artwork and try again."))
    fields = candidate["fields"]
    values = {name: fields[name] for name in chosen if current[name] != fields[name]}
    if path:
        values.update(cover_path=path, cover_edition_isbn=None, cover_edition_language=None)
    if values:
        item_write.update_item_fields(db, item_id, values)
    if path:
        cover_sync_policy.block(db, item_id)

    # Reuse the provenance representation without granting automatic refresh
    # ownership. In selective mode an unchecked blank means Keep local too.
    key = f"audible:{candidate['marketplace']}:{candidate['asin']}"
    baselines = _baselines(db, item_id)
    previous = {row["field_name"]: _snapshot(row["value_json"]) for row in baselines if row["source_key"] == key}
    stored = item(db, item_id)
    # An explicit Keep local/Apply choice must also survive the next connected
    # source refresh when its old applied value happens to equal the chosen
    # value (including NULL). Preserve observations and their timestamps; this
    # is a local decision, not a new observation from those other providers.
    decided = fields.keys() if mode == "selected" else chosen
    for row in baselines:
        if row["source_key"] == key or row["field_name"] not in decided:
            continue
        baseline = _snapshot(row["value_json"])
        if baseline is not None:
            baseline.update(local=stored[row["field_name"]], overridden=True)
            db.execute("UPDATE item_metadata_provenance SET value_json = ? WHERE item_id = ? AND source_key = ? AND field_name = ?",
                       (json.dumps(baseline, ensure_ascii=False), item_id, row["source_key"], row["field_name"]))
    protected = {row["field_name"] for row in baselines
                 if _overridden(current[row["field_name"]], _snapshot(row["value_json"]))}
    for name, observed in fields.items():
        overridden = name not in chosen and (mode == "selected" or name in protected
                                             or _overridden(current[name], previous.get(name, {})))
        snapshot = {"observed": observed, "local": stored[name], "overridden": overridden}
        if name in chosen:
            snapshot["applied"] = stored[name]
        db.execute("INSERT INTO item_metadata_provenance (item_id, source_key, field_name, value_json) VALUES (?, ?, ?, ?) "
                   "ON CONFLICT(item_id, source_key, field_name) DO UPDATE SET value_json = excluded.value_json, observed_at = datetime('now')",
                   (item_id, key, name, json.dumps(snapshot, ensure_ascii=False, allow_nan=False)))
    return len(chosen), bool(path)
