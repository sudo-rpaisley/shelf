import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, FileResponse

from app.auth import require_role
from app.config import DATABASE_PATH, DATA_DIR
from app.crypto import SENSITIVE_KEYS, encrypt_value, get_encryption_key
from app.currency import CURRENCIES, invalidate_cache as invalidate_currency_cache
from app import features
from app.database import get_db, get_setting, set_setting
from app.nav import HIDEABLE_KEYS, invalidate_cache as invalidate_nav_cache
from app.services import audiobookshelf, audnexus
from app.services.national import SEARCH_LANGS

router = APIRouter(prefix="/api/settings", dependencies=[Depends(require_role("admin"))])

_INTEGRATION_KEYS = (
    "abs_url",
    "abs_public_url",
    "abs_token",
    "komga_url",
    "komga_api_key",
    "romm_url",
    "romm_api_token",
    "romm_token",
    "isbndb_api_key",
    "tmdb_api_key",
    "hardcover_token",
    "google_books_api_key",
    "librarything_token",
    "igdb_client_id",
    "igdb_client_secret",
    "discogs_token",
)


def _upsert_setting(db, key: str, value: str, cleared: bool = False):
    """Write one setting. Sensitive keys are write-only: the page renders
    masked fields that post empty, so a blank submission keeps the stored
    value unless the matching clear checkbox was ticked."""
    if key in SENSITIVE_KEYS:
        if cleared:
            value = ""
        elif not value:
            return
        else:
            value = encrypt_value(value, get_encryption_key())
    set_setting(db, key, value)


@router.post("")
async def update_settings(request: Request):
    """Save integration settings.

    Only keys present in the submitted form are written — each integration
    section is its own form, and sections must not blank each other's values
    (previously handled by echoing every credential as a hidden input).
    """
    form = await request.form()
    romm_aliases = ("romm_token", "romm_api_token")
    romm_values = {(form.get(key) or "").strip() for key in romm_aliases if key in form}
    romm_nonempty = romm_values - {""}
    if len(romm_nonempty) > 1:
        raise HTTPException(400, "Conflicting RomM token inputs")
    romm_clear = any(form.get(f"clear_{key}") == "on" for key in romm_aliases)
    # Validated before the transaction opens: a bad browser URL must leave the
    # whole submission untouched, not write the other fields and reject one.
    if "abs_public_url" in form:
        public_url = (form.get("abs_public_url") or "").strip().rstrip("/")
        if public_url and audiobookshelf.validate_url(public_url):
            return RedirectResponse(
                url="/settings?abs_public_url_error=invalid", status_code=303
            )
    with get_db() as db:
        for key in _INTEGRATION_KEYS:
            if key not in form or key in romm_aliases:
                continue
            value = (form.get(key) or "").strip()
            if key in ("abs_url", "abs_public_url", "komga_url", "romm_url"):
                value = value.rstrip("/")
            _upsert_setting(db, key, value, cleared=form.get(f"clear_{key}") == "on")
        # Both public compatibility forms select the SAME credential. Updating
        # one must not leave a conflicting retained alias behind; a blank form
        # still preserves both, and either explicit clear removes both values.
        if romm_nonempty or romm_clear:
            selected = next(iter(romm_nonempty), "")
            for key in romm_aliases:
                _upsert_setting(db, key, selected, cleared=romm_clear)
    if "librarything_token" in form:
        from app.services.librarything import reset_cache
        reset_cache()
    invalidate_nav_cache()  # hardcover_token gates the Discover tab
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/google-books/test")
async def test_google_books(request: Request):
    """Test a submitted or configured Google Books API key."""
    from app.services import googlebooks

    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "message": "Invalid request"}
    if not isinstance(body, dict):
        return {"ok": False, "message": "Invalid request"}

    raw_api_key = body.get("api_key")
    if raw_api_key is not None and not isinstance(raw_api_key, str):
        return {"ok": False, "message": "Invalid request"}
    api_key = (raw_api_key or "").strip()
    if not api_key:
        with get_db() as db:
            api_key = get_setting(db, "google_books_api_key")
    if not api_key:
        return {"ok": False, "message": "No API key configured"}
    return await googlebooks.test_connection(api_key)


@router.post("/discogs/test")
async def test_discogs_token(discogs_token: str = Form("")):
    """Test a typed Discogs token, falling back to the configured token."""
    import httpx

    from app.config import HTTP_TIMEOUT
    from app.services import discogs

    token = discogs_token.strip()
    if not token:
        with get_db() as db:
            token = get_setting(db, "discogs_token")
    if not token:
        code = "missing"
    else:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            result = await discogs.test_connection(token, client)
        code = {
            "found": "ok",
            "rejected": "rejected",
            "rate_limited": "rate_limited",
            "transport_failed": "unavailable",
            "no_match": "invalid",
            "no_credential": "missing",
        }.get(result.outcome, "invalid")
    return RedirectResponse(url=f"/settings?discogs_test={code}", status_code=303)


@router.post("/vision")
async def update_vision_settings(
    vision_provider: str = Form(""),
    anthropic_api_key: str = Form(""),
    anthropic_vision_model: str = Form(""),
    openai_base_url: str = Form(""),
    openai_api_key: str = Form(""),
    openai_vision_model: str = Form(""),
    openai_ingest_long_edge: str = Form(""),
    ollama_url: str = Form(""),
    ollama_model: str = Form(""),
    ollama_ingest_long_edge: str = Form(""),
    clear_anthropic_api_key: str = Form(""),
    clear_openai_api_key: str = Form(""),
):
    """Save photo-intake vision settings.

    Separate handler (like /lending) so this partial form doesn't blank
    the other integration credentials.
    """
    if vision_provider not in ("", "anthropic", "openai", "ollama"):
        return {"ok": False, "message": "Unknown vision provider"}
    ollama_long_edge = ollama_ingest_long_edge.strip()
    if ollama_long_edge and not ollama_long_edge.isdigit():
        return {"ok": False, "message": "Ollama image size must be a whole number of pixels"}
    openai_long_edge = openai_ingest_long_edge.strip()
    if openai_long_edge and not openai_long_edge.isdigit():
        return {"ok": False, "message": "OpenAI image size must be a whole number of pixels"}
    # Each API key clears independently via its own checkbox.
    clears = {"anthropic_api_key": clear_anthropic_api_key == "on",
              "openai_api_key": clear_openai_api_key == "on"}
    with get_db() as db:
        for key, value in [
            ("vision_provider", vision_provider),
            ("anthropic_api_key", anthropic_api_key.strip()),
            ("anthropic_vision_model", anthropic_vision_model.strip()),
            ("openai_base_url", openai_base_url.strip().rstrip("/")),
            ("openai_api_key", openai_api_key.strip()),
            ("openai_vision_model", openai_vision_model.strip()),
            ("openai_ingest_long_edge", openai_long_edge),
            ("ollama_url", ollama_url.strip().rstrip("/")),
            ("ollama_model", ollama_model.strip()),
            ("ollama_ingest_long_edge", ollama_long_edge),
        ]:
            _upsert_setting(db, key, value, cleared=clears.get(key, False))
    invalidate_nav_cache()  # the vision provider and its key gate the Intake tab
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/lending")
async def update_lending_settings(
    lending_overdue_days: str = Form("28"),
    notify_url: str = Form(""),
    notify_format: str = Form("ntfy"),
    clear_notify_url: str = Form(""),
):
    """Save lending/reminder settings.

    Separate from POST /api/settings on purpose: that handler writes its
    entire fixed key list from the form, so posting a partial form there
    would blank the integration credentials.
    """
    from app.services.notify import FORMATS

    days = lending_overdue_days.strip() or "28"
    if not days.isdigit():
        return {"ok": False, "message": "Overdue days must be a whole number"}
    fmt = (notify_format or "").strip()
    if fmt not in FORMATS:
        return {"ok": False, "message": "Unknown notification format"}

    with get_db() as db:
        for key, value in [
            ("lending_overdue_days", days),
            ("notify_url", notify_url.strip()),
            ("notify_format", fmt),
        ]:
            _upsert_setting(db, key, value, cleared=clear_notify_url == "on" and key == "notify_url")
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/trash")
async def update_trash_settings(trash_retention_days: str = Form("180")):
    """Save the Trash retention window.

    Separate from POST /api/settings on purpose, same as /lending: a partial
    form posted there would blank the integration credentials.
    """
    from app.services import trash

    raw = trash_retention_days.strip() or "180"
    # int() on an ASCII-only string: str.isdigit() also accepts "²" and other
    # Unicode digits, which get_retention_days then reads back as the default.
    try:
        days = int(raw) if raw.isascii() else -1
    except ValueError:
        days = -1
    if not 0 <= days <= trash.MAX_TRASH_RETENTION_DAYS:
        return {"ok": False, "message": "Retention days must be a whole number"}

    with get_db() as db:
        _upsert_setting(db, "trash_retention_days", str(days))
        trash.settle_marker(db)
    trash.invalidate()
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/price-alerts")
async def update_price_alert_settings(
    price_alert_threshold_pct: str = Form("15"),
    price_alert_nightly_cap: str = Form("100"),
):
    """Save the price-alert drop threshold and nightly lookup cap.

    Separate from POST /api/settings for the same reason as /trash: a partial
    form posted there would blank the integration credentials.
    """
    from app.services import price_alerts

    def _whole(raw: str) -> int:
        # ASCII-only int(): str.isdigit() also accepts "²" (see /trash).
        raw = raw.strip()
        try:
            return int(raw) if raw.isascii() else -1
        except ValueError:
            return -1

    threshold = _whole(price_alert_threshold_pct)
    cap = _whole(price_alert_nightly_cap)
    if not 1 <= threshold <= price_alerts.MAX_THRESHOLD_PCT:
        return {"ok": False, "message": f"Drop threshold must be a whole number from 1 to {price_alerts.MAX_THRESHOLD_PCT}"}
    if not 1 <= cap <= price_alerts.MAX_NIGHTLY_CAP:
        return {"ok": False, "message": f"Books per night must be a whole number from 1 to {price_alerts.MAX_NIGHTLY_CAP}"}

    with get_db() as db:
        _upsert_setting(db, "price_alert_threshold_pct", str(threshold))
        _upsert_setting(db, "price_alert_nightly_cap", str(cap))
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/buy-links")
async def update_buy_links_settings(
    buy_links_bookshop_id: str = Form(""),
    buy_links_bookshop_host: str = Form(""),
    buy_links_amazon_tag: str = Form(""),
    buy_links_amazon_host: str = Form(""),
):
    """Save the owner's own affiliate tags for the buy links.

    Separate from POST /api/settings for the same reason as /price-alerts. A
    blank tag is valid and turns that store off. Nothing is written unless all
    four values pass, so a refusal leaves every saved value as it was.
    """
    from app.services import buy_links

    bookshop_id = buy_links_bookshop_id.strip()
    amazon_tag = buy_links_amazon_tag.strip()
    bookshop_host = buy_links_bookshop_host.strip() or buy_links.DEFAULT_BOOKSHOP_HOST
    amazon_host = buy_links_amazon_host.strip() or buy_links.DEFAULT_AMAZON_HOST
    if bookshop_id and not buy_links.BOOKSHOP_ID_RE.fullmatch(bookshop_id):
        return {"ok": False, "message": "Bookshop.org affiliate id is digits only, up to 12"}
    if amazon_tag and not buy_links.AMAZON_TAG_RE.fullmatch(amazon_tag):
        return {"ok": False, "message": "Amazon tag may use letters, digits and hyphens, up to 40 characters"}
    if bookshop_host not in buy_links.BOOKSHOP_HOSTS:
        return {"ok": False, "message": "Unknown Bookshop.org site"}
    if amazon_host not in buy_links.AMAZON_HOSTS:
        return {"ok": False, "message": "Unknown Amazon marketplace"}

    with get_db() as db:
        _upsert_setting(db, "buy_links_bookshop_id", bookshop_id)
        _upsert_setting(db, "buy_links_bookshop_host", bookshop_host)
        _upsert_setting(db, "buy_links_amazon_tag", amazon_tag)
        _upsert_setting(db, "buy_links_amazon_host", amazon_host)
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/release-radar")
async def update_release_radar_settings(release_radar_cap: str = Form("50")):
    """Save the release-radar daily series cap.

    Separate from POST /api/settings for the same reason as /price-alerts: a
    partial form posted there would blank the integration credentials.
    """
    from app.services import release_radar

    # ASCII-only int(): str.isdigit() also accepts "²" (see /trash).
    raw = release_radar_cap.strip()
    try:
        cap = int(raw) if raw.isascii() else -1
    except ValueError:
        cap = -1
    if not 1 <= cap <= release_radar.MAX_CAP:
        return {"ok": False, "message": f"Series per day must be a whole number from 1 to {release_radar.MAX_CAP}"}

    with get_db() as db:
        _upsert_setting(db, "release_radar_cap", str(cap))
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/labels")
async def update_labels_settings(copy_barcode_prefix: str = Form("")):
    """Save the copy-label prefix.

    Admin by the router dependency and deliberately not feature-gated, like
    the price-alert settings: an admin may set the prefix up before turning
    Collector tools on. Separate from POST /api/settings for the same reason
    as /trash — a partial form posted there would blank the credentials.
    """
    from app.services import copy_labels

    value = copy_labels.normalise_prefix(copy_barcode_prefix)
    if value is None:
        return RedirectResponse(url="/settings?labels_prefix_error=invalid", status_code=303)
    with get_db() as db:
        _upsert_setting(db, copy_labels.PREFIX_KEY, value)
    return RedirectResponse(url="/settings", status_code=303)

@router.post("/nav")
async def update_nav_settings(request: Request):
    """Save which nav tabs are visible.

    Each hideable tab gets a checkbox that posts "on" when checked (visible).
    Only keys in HIDEABLE_KEYS are ever considered — a forged 'browse' or
    'settings' checkbox, or any unknown key, is silently dropped, so those
    tabs can never end up in the stored hidden list.
    """
    form = await request.form()
    checked = {k for k in form.keys() if k in HIDEABLE_KEYS}
    hidden = sorted(HIDEABLE_KEYS - checked)
    with get_db() as db:
        _upsert_setting(db, "nav_hidden_tabs", json.dumps(hidden))
    invalidate_nav_cache()
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/features/{key}")
async def set_feature(key: str, enabled: str = Form(...)):
    """Turn a feature on ("1") or off ("0") from Settings → Features.

    Probes are not re-run here: the warning is the page's `data-confirm`
    step, and the stored flag is the only effect. Nothing is deleted.
    """
    if key not in features.FEATURES or enabled not in ("0", "1"):
        raise HTTPException(status_code=404 if key not in features.FEATURES else 400)
    with get_db() as db:
        features.set_feature_enabled(db, key, enabled == "1")
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/profile")
async def set_profile(profile: str = Form(...)):
    """Apply Minimal, Standard or Everything from Settings → Features.

    Validates and applies; the page's `data-confirm` decides whether to ask
    first, as for a single feature. Not under `/features/`, where the dynamic
    `/features/{key}` above would take it (G119).
    """
    if profile not in features.PROFILES:
        raise HTTPException(status_code=400)
    with get_db() as db:
        features.apply_profile(db, profile)
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/features/{key}/enable")
async def enable_feature(key: str):
    """Turn a feature on from its disabled page.

    Lands on the feature's own entry path from the registry. No request value
    (a `next` field, the Referer) is ever read for the target: echoing one
    would be an open redirect on an admin POST.
    """
    if key not in features.FEATURES:
        raise HTTPException(status_code=404)
    with get_db() as db:
        features.set_feature_enabled(db, key, True)
    return RedirectResponse(url=features.FEATURES[key].entry_path, status_code=303)


@router.post("/display")
async def update_display_settings(request: Request):
    """Save display currency, metadata language and Browse grouping.

    Only writes when the posted code is known — an unknown code is silently
    dropped, same posture as /nav's key filtering. Currency is display
    only: Shelf never converts amounts between currencies. The search
    language steers which language's editions Open Library search prefers.
    """
    form = await request.form()
    code = (form.get("currency") or "").strip().upper()
    if code in CURRENCIES:
        with get_db() as db:
            _upsert_setting(db, "currency", code)
        invalidate_currency_cache()

    search_lang = (form.get("metadata_search_lang") or "").strip()
    if search_lang in SEARCH_LANGS:
        with get_db() as db:
            _upsert_setting(db, "metadata_search_lang", search_lang)

    if form.get("browse_group_digital_comics_present") == "1":
        group_digital_comics = "1" if form.get("browse_group_digital_comics") == "on" else "0"
        with get_db() as db:
            _upsert_setting(db, "browse_group_digital_comics", group_digital_comics)
    # Audible marketplace for Audnexus lookups. Empty or unknown is refused
    # here, not in the markup (G104).
    audible_region = (form.get("audible_region") or "").strip()
    if audible_region in audnexus.REGIONS:
        with get_db() as db:
            _upsert_setting(db, "audible_region", audible_region)

    return RedirectResponse(url="/settings", status_code=303)


@router.post("/notify-test")
async def notify_test(request: Request):
    """Send a test notification to the URL provided in the request body."""
    from app.services.notify import send_notification

    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "message": "Invalid request"}
    if not isinstance(body, dict):
        return {"ok": False, "message": "Invalid request"}

    raw_url = body.get("url")
    raw_fmt = body.get("format")
    if (
        (raw_url is not None and not isinstance(raw_url, str))
        or (raw_fmt is not None and not isinstance(raw_fmt, str))
    ):
        return {"ok": False, "message": "Invalid request"}
    url = (raw_url or "").strip()
    fmt = raw_fmt or "ntfy"
    if not url:
        # Masked field posts empty — test the stored URL instead
        from app.database import get_setting
        with get_db() as db:
            url = get_setting(db, "notify_url")
    if not url:
        return {"ok": False, "message": "No URL provided"}

    ok = await send_notification(url, "Shelf test notification", "Loan reminders are working!", fmt)
    return {"ok": ok, "message": "Test notification sent" if ok else "Send failed — check the URL and server logs"}


def _vacuum_backup():
    backup_path = DATA_DIR / "shelf_backup.db"
    backup_path.unlink(missing_ok=True)
    with get_db() as db:
        db.execute("VACUUM INTO ?", (str(backup_path),))
    return backup_path


@router.get("/backup")
async def download_backup():
    """Download a plain backup of the SQLite database."""
    backup_path = _vacuum_backup()
    filename = f"shelf_backup_{datetime.now():%Y%m%d_%H%M}.db"
    return FileResponse(str(backup_path), filename=filename, media_type="application/octet-stream")


@router.post("/backup")
async def download_backup_encrypted(passphrase: str = Form("")):
    """Download a backup, AES-GCM-encrypted when a passphrase is provided —
    an off-site copy then exposes neither user hashes nor item data."""
    from app.crypto import encrypt_backup

    backup_path = _vacuum_backup()
    if not passphrase:
        filename = f"shelf_backup_{datetime.now():%Y%m%d_%H%M}.db"
        return FileResponse(str(backup_path), filename=filename, media_type="application/octet-stream")

    encrypted = encrypt_backup(backup_path.read_bytes(), passphrase)
    enc_path = DATA_DIR / "shelf_backup.db.enc"
    enc_path.write_bytes(encrypted)
    backup_path.unlink(missing_ok=True)  # don't leave the plaintext copy behind
    filename = f"shelf_backup_{datetime.now():%Y%m%d_%H%M}.db.enc"
    return FileResponse(str(enc_path), filename=filename, media_type="application/octet-stream")


def _validate_restore_schema(conn):
    """Read-only hostile-schema checks, before the central lineage gate."""
    for kind in ("trigger", "view"):
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = ? LIMIT 1", (kind,)).fetchone():
            raise ValueError(f"Database contains {kind}s — not allowed")
    for row in conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table'"):
        if re.match(r"\s*CREATE\s+VIRTUAL\s+TABLE\b", row[0] or "", re.I):
            raise ValueError("Database contains virtual tables — not allowed")
    if any(row[2] == "virtual" for row in conn.execute("PRAGMA table_list")):
        raise ValueError("Database contains virtual tables — not allowed")
    if len(conn.execute("PRAGMA database_list").fetchall()) != 1:
        raise ValueError("Database has attached databases — not allowed")
    required = {
        "items": {"id", "title", "media_type"},
        "users": {"id", "username", "password", "role"},
        "settings": {"key", "value"},
    }
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if missing := required.keys() - tables:
        raise ValueError(f"Not a valid Shelf database (missing tables: {', '.join(sorted(missing))})")
    for table, columns in required.items():
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if missing := columns - existing:
            raise ValueError(f"Incompatible schema: '{table}' table is missing columns: {', '.join(sorted(missing))}")
    if [row[0] for row in conn.execute("PRAGMA integrity_check")] != ["ok"]:
        raise ValueError("Invalid database file — integrity check failed")


def _restore_destination_keys():
    """Capture existing active keys without auth's relocation/generation path.

    get_secret_key may write a key file AND prune a live settings row on a cold
    resolve. Neither is an acceptable side effect of a possibly refused restore.
    """
    from app import auth, crypto

    encryption_key = crypto.get_existing_encryption_key()
    signing_key = (auth._cached_secret_key or auth.SECRET_KEY
                   or crypto._read_keyfile(auth.SIGNING_KEY_FILE))
    if not signing_key:
        raise ValueError("Restore requires an existing destination signing key")
    return encryption_key, signing_key


def _prepare_restore(conn, *, encryption_key, signing_key):
    """Migrate ONLY the uploaded staging database, never the serving database.

    The public central connection-taking gate owns lineage and migrations.
    Crypto uses explicit, already-established destination keys; the uploaded
    signing-key row is ONLY a legacy decryption candidate and is then removed.
    """
    from app.crypto import normalize_sensitive_settings
    from app.database import inspect_schema_lineage, validate_and_migrate_staged_connection
    from app.services import author_index

    conn.execute("PRAGMA trusted_schema=OFF")
    _validate_restore_schema(conn)
    conn.row_factory = sqlite3.Row
    inspect_schema_lineage(conn)  # no history deletion/relabeling or upstream blessing
    source_key_row = conn.execute("SELECT value FROM settings WHERE key = 'secret_key'").fetchone()
    source_key = source_key_row[0] if source_key_row else None
    if source_key is not None and not isinstance(source_key, str):
        raise ValueError("Invalid legacy signing-key setting")
    conn.execute("PRAGMA journal_mode=DELETE")  # staging has no other connections/WAL
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA secure_delete=ON")
    validate_and_migrate_staged_connection(conn)
    normalization = normalize_sensitive_settings(
        conn, encryption_key=encryption_key, signing_key=signing_key,
        legacy_signing_keys=(source_key,) if source_key else (),
    )
    conn.execute("DELETE FROM settings WHERE key = 'secret_key'")
    author_index.rebuild_all(conn)  # boot-derived work also finishes before replacement
    conn.commit()
    conn.execute("VACUUM")  # no freed source-key/plaintext pages ride into the live copy
    inspect_schema_lineage(conn)
    return normalization


def _token_high_water(conn):
    from app.auth import TOKEN_VERSION_HIGH_WATER

    return conn.execute(
        "SELECT MAX(COALESCE((SELECT MAX(token_version) FROM users), 0), "
        "COALESCE((SELECT CAST(value AS INTEGER) FROM settings WHERE key = ?), 0))",
        (TOKEN_VERSION_HIGH_WATER,),
    ).fetchone()[0]


def _invalidate_restored_tokens(conn, live_max):
    from app.auth import TOKEN_VERSION_HIGH_WATER

    high_water = max(live_max, _token_high_water(conn)) + 1
    conn.execute(
        "UPDATE users SET token_version = ?, updated_at = datetime('now')", (high_water,)
    )
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (TOKEN_VERSION_HIGH_WATER, str(high_water)),
    )
    # API tokens are independent of the cookie token_version. A restored hash
    # must never resurrect a previously revoked bearer capability.
    conn.execute("DELETE FROM api_tokens")
    conn.commit()


def _fsync_restore_file(path):
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _install_restored_database(staged, staged_path):
    """SQLite's online backup is an atomic page replacement, including WAL.

    Do NOT os.replace shelf.db: open connections retain its old WAL and can
    replay it over the replacement. Keep a unique, durable pre-restore snapshot;
    SQLite rolls its destination transaction back if backup fails. No active
    user/session/settings row is changed until the fully prepared copy lands.
    """
    if staged.execute("PRAGMA page_count").fetchone()[0] <= 1:
        raise ValueError("Not a fully prepared Shelf database")
    fd, name = tempfile.mkstemp(prefix="shelf_pre_restore_", suffix=".db", dir=DATA_DIR)
    os.close(fd)
    from pathlib import Path

    recovery_path = Path(name)
    # Retain even a failed attempt's recovery copy. Do not log a refusal here:
    # SQLiteHandler would itself mutate the active database's log_entries.
    with closing(sqlite3.connect(str(DATABASE_PATH))) as live:
        with closing(sqlite3.connect(str(recovery_path))) as recovery:
            live.backup(recovery)
            _invalidate_restored_tokens(staged, _token_high_water(recovery))
        _fsync_restore_file(recovery_path)
        _fsync_restore_file(staged_path)
        directory = os.open(str(DATA_DIR), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        live.execute("PRAGMA synchronous=FULL")
        staged_version = _token_high_water(staged)
        with closing(sqlite3.connect(str(DATABASE_PATH))) as observer:
            checked = False

            def check_locked_snapshot(status, remaining, total):
                nonlocal checked
                if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise ValueError("Database is busy — retry the restore")
                if not checked and status == sqlite3.SQLITE_OK:
                    # pages=1 and the prepared central schema's >1 pages ensure
                    # this runs BEFORE SQLITE_DONE/commit. SQLite holds the
                    # destination write transaction across all backup steps.
                    # This separate reader still sees its committed pre-copy
                    # snapshot, never the partially copied schema/pages.
                    if _token_high_water(observer) >= staged_version:
                        raise ValueError("Database sessions changed during restore — retry the restore")
                    checked = True

            # A callback refusal aborts/rolls back SQLite's destination backup
            # transaction. No cookie issued between the recovery snapshot and
            # this write lock can collide with the staged session generation.
            staged.backup(live, pages=1, progress=check_locked_snapshot)


@router.post("/restore")
async def restore_backup(request: Request):
    """Restore database from uploaded .db file. Requires container restart."""
    form = await request.form()
    db_file = form.get("file")
    if not db_file or not hasattr(db_file, "read"):
        return {"ok": False, "message": "No file uploaded"}

    max_db_size = 500 * 1024 * 1024  # 500 MB
    content = await db_file.read(max_db_size + 1)
    if len(content) > max_db_size:
        return {"ok": False, "message": "File too large (max 500 MB)"}

    from app.crypto import is_encrypted_backup, decrypt_backup
    if is_encrypted_backup(content):
        passphrase = (form.get("passphrase") or "").strip()
        if not passphrase:
            return {"ok": False, "message": "This backup is encrypted — enter its passphrase"}
        try:
            content = decrypt_backup(content, passphrase)
        except ValueError as e:
            return {"ok": False, "message": str(e)}

    from pathlib import Path
    from app.database import SchemaLineageError

    try:
        encryption_key, signing_key = _restore_destination_keys()
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}

    fd, name = tempfile.mkstemp(prefix="shelf_restore_", suffix=".db", dir=DATA_DIR)
    tmp_path = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        with closing(sqlite3.connect(str(tmp_path))) as staged:
            normalization = _prepare_restore(staged, encryption_key=encryption_key, signing_key=signing_key)
            _install_restored_database(staged, tmp_path)
    except (SchemaLineageError, ValueError) as exc:
        return {"ok": False, "message": str(exc)}
    except Exception:
        return {"ok": False, "message": "Invalid database or restore failed — the active database was not replaced"}
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(tmp_path) + suffix).unlink(missing_ok=True)

    # The external encryption/signing keys are deliberately never replaced or
    # generated here. Credentials from a different install need re-entry.
    invalidate_nav_cache()  # the restored DB carries its own settings
    features.invalidate_cache()  # ...including its own feature.* rows
    invalidate_currency_cache()

    return {
        "ok": True,
        "message": "Database restored. All sessions invalidated. Restart the container to apply.",
        "credentials_require_reentry": normalization["undecryptable"],
    }
