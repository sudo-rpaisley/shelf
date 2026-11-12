import json
import logging
import math
import sqlite3
import unicodedata
from urllib.parse import urlparse

import httpx

from app import config
from app.config import BOOK_MEDIA_TYPES
from app.database import get_db
from app.services import authors as authors_svc
from app.services import cover_sync_policy, media_groups, metadata_policy
from app.services.item_write import IdentifierInTrash, insert_item
from app.services.item_write import ItemValueError, update_item_fields
from app.services import isbn as isbn_svc

logger = logging.getLogger(__name__)

_UNKNOWN_CREATORS = frozenset({
    "unknown", "unknown author", "anonymous", "various", "various authors",
    "various artists", "n a", "none", "null",
})


def validate_url(url: str) -> str | None:
    """Validate an Audiobookshelf URL's scheme and hostname.

    Returns an error message, or None when the URL is usable. Lives here rather
    than in a router because both the sync router (the API endpoint) and the
    settings router (the browser-facing endpoint) validate the same shape.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return "URL must use http:// or https://"
        if not parsed.hostname:
            return "Invalid URL"
    except Exception:
        return "Invalid URL"
    return None


def get_excluded_libraries() -> set[str]:
    """ABS library IDs the user has opted out of syncing (JSON setting)."""
    with get_db() as db:
        row = db.execute(
            "SELECT value FROM settings WHERE key = 'abs_excluded_libraries'"
        ).fetchone()
    if not row or not row["value"]:
        return set()
    try:
        return set(json.loads(row["value"]))
    except (json.JSONDecodeError, TypeError):
        return set()


def _normalise_publish_year(value):
    """Match SQLite's usual INTEGER coercion so an unchanged year stays unchanged."""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _collection(response: httpx.Response, key: str) -> tuple[dict, list]:
    """An omitted collection is a malformed response, not an empty library."""
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
        raise ValueError("Malformed Audiobookshelf collection")
    return payload, payload[key]


def _listing_is_partial(payload: dict, count: int, requested_limit: int | None = None) -> bool:
    """This client reads one page; never claim a known truncated page is complete."""
    totals = []
    for name in ("total", "totalItems", "totalResults", "totalLibraries"):
        if name in payload:
            value = payload[name]
            if type(value) is not int or value < 0:
                raise ValueError("Malformed Audiobookshelf total")
            totals.append(value)
    partial = any(value != count for value in totals)
    complete_hint = bool(totals) and not partial
    for name in ("page", "limit", "totalPages", "numPages"):
        if name in payload and (type(payload[name]) is not int or payload[name] < 0):
            raise ValueError("Malformed Audiobookshelf pagination")
    partial |= payload.get("page", 0) > 0
    partial |= any(payload.get(name, 0) > 1 for name in ("totalPages", "numPages"))
    for name in ("hasMore", "hasNextPage", "isLast"):
        if name in payload:
            if type(payload[name]) is not bool:
                raise ValueError("Malformed Audiobookshelf pagination")
            more = not payload[name] if name == "isLast" else payload[name]
            partial |= more
            complete_hint |= not more
    if "nextPage" in payload and payload["nextPage"] is not None:
        if type(payload["nextPage"]) is not int or payload["nextPage"] < 0:
            raise ValueError("Malformed Audiobookshelf pagination")
        partial = True
    limit = payload.get("limit") or requested_limit
    return partial or bool(limit and count >= limit and not complete_hint)


def _incomplete(stats: dict) -> None:
    stats["errors"] += 1
    stats["complete"] = False


def _valid_item(item) -> bool:
    """Validate shapes used below so one bad record cannot abort healthy siblings."""
    if not isinstance(item, dict) or not isinstance(item.get("media"), dict):
        return False
    media = item["media"]
    metadata = media.get("metadata")
    if not isinstance(metadata, dict):
        return False
    if item.get("id") is not None and not isinstance(item["id"], str):
        return False
    for name in ("title", "authorName", "author", "narratorName", "seriesName", "publisher", "description"):
        if metadata.get(name) is not None and not isinstance(metadata[name], str):
            return False
    year = metadata.get("publishedYear")
    if year is not None and type(year) not in (str, int):
        return False
    if year not in (None, ""):
        normalised_year = _normalise_publish_year(year)
        if type(normalised_year) is int and not -(2**63) <= normalised_year < 2**63:
            return False
    for name in ("numAudioFiles", "duration"):
        value = media.get(name)
        if value is not None and (type(value) not in (int, float) or value < 0
                                  or value > 2**63 - 1 or not math.isfinite(value)):
            return False
        if name == "numAudioFiles" and value is not None and type(value) is not int:
            return False
    return media.get("ebookFormat") is None or isinstance(media["ebookFormat"], str)


def _live_identity_conflict(db, existing, updates):
    """Check the final identity AFTER three-way planning; Trash stays the funnel's job."""
    media_type = updates.get("media_type", existing["media_type"])
    for name in ("isbn", "upc"):
        value = updates.get(name, existing[name])
        if value is None or (name == "isbn" and not value):
            continue
        row = db.execute(
            f"SELECT id FROM items_live WHERE {name} = ? AND media_type = ? AND id != ? ORDER BY id LIMIT 1",
            (value, media_type, existing["id"]),
        ).fetchone()
        if row is not None:
            return row["id"], name, value
    return None


def _cover_fill_allowed(db, item_id: int, source_key: str) -> bool:
    # Cover removal deliberately leaves the old {id}.jpg on disk and resets
    # dismissal to zero. That retained file is evidence of a removed cover,
    # not permission for an automatic sync to put it back.
    preference = cover_sync_policy.decision(db, item_id)
    if preference is True:
        return False
    allowed = metadata_policy.cover_fill_allowed(
        db, item_id, source_key, retained_file=(config.COVERS_DIR / f"{item_id}.jpg").exists(),
        explicit_allow=preference is False,
    )
    if not allowed and preference is None:
        row = db.execute("SELECT cover_path, cover_review_dismissed FROM items_live WHERE id = ?", (item_id,)).fetchone()
        if row is not None and not row["cover_path"] and not row["cover_review_dismissed"]:
            cover_sync_policy.block(db, item_id)
    return allowed


async def sync(abs_url: str, abs_token: str, on_progress=None) -> dict:
    """Sync items from Audiobookshelf. Returns summary stats.

    on_progress: optional async callback(current, total, title, status) for progress updates.
    """
    # `in_trash` is counted lazily, deliberately not seeded here. This loop
    # increments by literal name — it has no "is this action known" check the
    # way the Komga and RomM loops do — so a lazy key is safe, and it keeps
    # the stats shape byte-identical until something is actually in Trash,
    # which is every sync until the delete sites are flipped. The template
    # renders the count only when it is non-zero.
    stats = {"added": 0, "updated": 0, "unchanged": 0, "skipped": 0, "errors": 0}

    headers = {"Authorization": f"Bearer {abs_token}"}
    excluded = get_excluded_libraries()

    async with httpx.AsyncClient(timeout=30) as client:
        # Get libraries
        resp = await client.get(f"{abs_url}/api/libraries", headers=headers)
        if resp.status_code != 200:
            return {"error": f"Failed to connect: HTTP {resp.status_code}"}

        try:
            payload, remote_libraries = _collection(resp, "libraries")
        except (ValueError, RecursionError):
            _incomplete(stats)
            return stats
        try:
            if _listing_is_partial(payload, len(remote_libraries)):
                _incomplete(stats)
        except ValueError:
            _incomplete(stats)
        libraries = []
        for lib in remote_libraries:
            if (not isinstance(lib, dict) or not isinstance(lib.get("id"), str)
                    or not lib["id"].strip()):
                _incomplete(stats)
                continue
            if lib["id"] in excluded:
                continue
            if (not isinstance(lib.get("name", ""), str)
                    or lib.get("mediaType", "book") not in ("book", "podcast")):
                _incomplete(stats)
                continue
            libraries.append(lib)

        # First pass: fetch all library items to get total count. A single
        # large/slow library should not prevent healthy libraries from syncing.
        lib_items = []
        for lib in libraries:
            lib_id = lib["id"]
            try:
                resp = await client.get(
                    f"{abs_url}/api/libraries/{lib_id}/items",
                    headers=headers,
                    params={"limit": 10000},
                )
            except httpx.TimeoutException:
                logger.warning(
                    "Timed out fetching Audiobookshelf library %s; skipping it for this sync",
                    lib_id,
                )
                stats["errors"] += 1
                continue
            except httpx.HTTPError:
                stats["errors"] += 1
                continue
            if resp.status_code != 200:
                stats["errors"] += 1
                continue
            try:
                payload, results = _collection(resp, "results")
            except (ValueError, RecursionError):
                _incomplete(stats)
                continue
            try:
                if _listing_is_partial(payload, len(results), 10000):
                    _incomplete(stats)
            except ValueError:
                _incomplete(stats)
            lib_items.append((lib, results))

        total = sum(len(results) for _, results in lib_items)
        current = 0

        for lib, results in lib_items:
            lib_id = lib["id"]
            lib_type = lib.get("mediaType", "book")

            # Determine default media type from library name
            lib_name = lib.get("name", "").lower()
            if "ebook" in lib_name or "epub" in lib_name:
                lib_default_type = "ebook"
            elif lib_type == "podcast":
                lib_default_type = "ebook"
            else:
                lib_default_type = "audiobook"

            for item in results:
                current += 1
                if not _valid_item(item):
                    _incomplete(stats)
                    if on_progress:
                        await on_progress(current, total, "Malformed Audiobookshelf item", "error")
                    continue
                abs_id = item.get("id")
                media = item.get("media", {})

                # Determine media type per item:
                # - Has audio files/duration → audiobook
                # - Has ebookFormat and no audio → ebook
                # - Otherwise use library-level default
                num_audio = media.get("numAudioFiles") or 0
                duration = media.get("duration") or 0
                ebook_format = media.get("ebookFormat")

                if num_audio > 0 or duration > 0:
                    media_type = "audiobook"
                elif ebook_format and num_audio == 0:
                    media_type = "ebook"
                else:
                    media_type = lib_default_type
                metadata = media.get("metadata", {})

                title = metadata.get("title")
                if not title or not title.strip():
                    _incomplete(stats)
                    stats["skipped"] += 1
                    if on_progress:
                        item_label = abs_id or "unknown item"
                        await on_progress(
                            current, total,
                            f"Missing title — Audiobookshelf item {item_label}",
                            "skipped",
                        )
                    continue
                if not abs_id or not abs_id.strip():
                    _incomplete(stats)
                    stats["skipped"] += 1
                    if on_progress:
                        await on_progress(
                            current, total,
                            f"Missing Audiobookshelf item ID — {title}",
                            "skipped",
                        )
                    continue

                authors = metadata.get("authorName") or metadata.get("author")
                narrator = metadata.get("narratorName")
                # A provider value. Pre-cleaned per #54: the raw value
                # survives only in the warning below. The ASIN is a different
                # identifier and never stands in for the ISBN (G72).
                raw_isbn = metadata.get("isbn")
                isbn_pair = isbn_svc.canonical_isbn_pair(raw_isbn) if raw_isbn else None
                if raw_isbn and isbn_pair is None:
                    logger.warning(
                        "%s: %r is not a valid ISBN — stored without one",
                        f"Audiobookshelf item {abs_id} ({title})", raw_isbn,
                    )
                isbn = isbn_pair[0] if isbn_pair else None
                raw_asin = metadata.get("asin")
                asin = isbn_svc.clean_asin(raw_asin)
                if raw_asin and asin is None:
                    logger.warning(
                        "%s: %r is not a valid ASIN — stored without one",
                        f"Audiobookshelf item {abs_id} ({title})", raw_asin,
                    )
                series_name = metadata.get("seriesName")
                publisher = metadata.get("publisher")
                pub_year = _normalise_publish_year(metadata.get("publishedYear"))
                description = metadata.get("description")

                duration_secs = media.get("duration")
                duration_mins = int(duration_secs / 60) if duration_secs else None
                source_key = f"audiobookshelf:{abs_id}"
                provider_fields = {
                    "title": title,
                    "authors": authors,
                    "narrator": narrator,
                    "isbn": isbn,
                    "asin": asin,
                    "series_name": series_name,
                    "publisher": publisher,
                    "publish_year": pub_year,
                    "description": description,
                    "duration_mins": duration_mins,
                    "media_type": media_type,
                }

                with get_db() as db:
                    metadata_policy.begin_write(db)
                    # Physical, and LIVE FIRST (claude-R4 / codex-R1). A
                    # trashed row must be seen here — the view would hide it,
                    # the ISBN-less item would miss, and the next sync would
                    # insert a duplicate. But `abs_id` is a plain index, not
                    # unique, so a trashed row must never win over a live one
                    # sharing the id: order live rows first.
                    existing = db.execute(
                        """SELECT * FROM items WHERE abs_id = ?
                           ORDER BY deleted_at IS NOT NULL, id LIMIT 1""",
                        (abs_id,),
                    ).fetchone()
                    if existing is not None and existing["deleted_at"] is not None:
                        # A machine re-syncing does not resurrect what a
                        # person deleted. Skipped BEFORE any write (G85), and
                        # counted — never a swallowed exception (G47).
                        stats["in_trash"] = stats.get("in_trash", 0) + 1
                        db.rollback()
                        if on_progress:
                            await on_progress(current, total, title, "in_trash")
                        continue

                    # Shelf permits the same ISBN across formats, but not twice
                    # within one media type. ABS IDs alone are therefore not
                    # enough to decide whether an insert is safe: a manually
                    # catalogued audiobook may already occupy the ISBN slot,
                    # or ABS itself may expose duplicate records for it.
                    isbn_match = None
                    if not existing and isbn:
                        isbn_match = db.execute(
                            """SELECT id, abs_id FROM items_live
                               WHERE isbn = ? AND media_type = ?
                               ORDER BY id LIMIT 1""",
                            (isbn, media_type),
                        ).fetchone()

                    if isbn_match:
                        if isbn_match["abs_id"]:
                            # SQLite-backed logging opens a second connection
                            # (G3), so release this item's lock before warning.
                            db.rollback()
                            logger.warning(
                                "Skipping ABS item %s (%s): ISBN %s already belongs "
                                "to Shelf item %s for media type %s",
                                abs_id, title, isbn, isbn_match["id"], media_type,
                            )
                            stats["skipped"] += 1
                            if on_progress:
                                await on_progress(
                                    current, total,
                                    f"ISBN conflict with Shelf item {isbn_match['id']} — {title} ({isbn})",
                                    "skipped",
                                )
                            continue

                        # Adopt an existing manually-added same-format item
                        # instead of attempting a duplicate insert. From this
                        # point on provenance still treats its existing
                        # nonempty metadata as manual, not provider-owned.
                        existing = db.execute(
                            "SELECT * FROM items_live WHERE id = ?",
                            (isbn_match["id"],),
                        ).fetchone()

                    # A bad value here is one ABS record, not the whole sync
                    # (#54) — insert_item()/update_item_fields() raise
                    # ItemValueError before touching SQLite, so nothing is
                    # written for this item and the loop moves on.
                    if existing:
                        desired = metadata_policy.metadata_updates(
                            db, existing["id"], source_key, provider_fields,
                        )
                        # Keep #54's repair of an old ASIN/malformed value
                        # misfiled as ISBN. This is identifier sanitation, not
                        # permission to overwrite valid local metadata.
                        if (existing["isbn"]
                                and isbn_svc.canonical_isbn_pair(existing["isbn"]) is None):
                            desired["isbn"] = isbn
                        conflict = _live_identity_conflict(db, existing, desired)
                        if conflict is not None:
                            conflict_id, field, value = conflict
                            db.rollback()
                            logger.warning(
                                "Skipping ABS item %s (%s): %s %s already belongs to Shelf item %s",
                                abs_id, title, field.upper(), value, conflict_id,
                            )
                            stats["skipped"] += 1
                            if on_progress:
                                await on_progress(
                                    current, total,
                                    f"{field.upper()} conflict with Shelf item {conflict_id} — {title} ({value})",
                                    "skipped",
                                )
                            continue
                        applied_fields = set(desired)
                        for name, value in {"abs_id": abs_id, "abs_library_id": lib_id}.items():
                            if existing[name] != value:
                                desired[name] = value
                        changed = bool(desired)

                        try:
                            if changed:
                                update_item_fields(db, existing["id"], desired)
                                status = "updated"
                            else:
                                status = "unchanged"
                            metadata_policy.record_observation(
                                db, existing["id"], source_key, provider_fields,
                                applied_fields=applied_fields,
                            )
                            stats[status] += 1
                        except IdentifierInTrash:
                            # The update funnel refuses an ISBN a trashed row
                            # holds — counted as the insert arm counts it, not
                            # as an error on every sync until Trash is emptied.
                            stats["in_trash"] = stats.get("in_trash", 0) + 1
                            db.rollback()
                            if on_progress:
                                await on_progress(current, total, title, "in_trash")
                            continue
                        except ItemValueError as e:
                            stats["errors"] += 1
                            db.rollback()
                            if on_progress:
                                await on_progress(current, total, f"{title}: {e}", "error")
                            continue
                        except sqlite3.IntegrityError:
                            db.rollback()
                            stats["skipped"] += 1
                            if on_progress:
                                await on_progress(current, total, f"Identifier conflict — {title}", "skipped")
                            continue

                        item_id = existing["id"]
                        fetch_cover = _cover_fill_allowed(db, item_id, source_key)
                    else:
                        try:
                            item_id = insert_item(
                                db,
                                title=title,
                                authors=authors,
                                isbn=isbn,
                                # The provider's value, pre-cleaned: an
                                # invalid ASIN is dropped, not refused (G72).
                                asin=asin,
                                media_type=media_type,
                                publisher=publisher,
                                publish_year=pub_year,
                                description=description,
                                series_name=series_name,
                                narrator=narrator,
                                duration_mins=duration_mins,
                                abs_id=abs_id,
                                abs_library_id=lib_id,
                                source="audiobookshelf",
                                restore_trashed=False,
                            )
                        except IdentifierInTrash:
                            # A trashed ISBN twin with a different abs_id.
                            # Must precede the ItemValueError arm below — it
                            # is a subclass, and that arm counts an error.
                            stats["in_trash"] = stats.get("in_trash", 0) + 1
                            db.rollback()
                            if on_progress:
                                await on_progress(current, total, title, "in_trash")
                            continue
                        except ItemValueError as e:
                            stats["errors"] += 1
                            db.rollback()
                            if on_progress:
                                await on_progress(current, total, f"{title}: {e}", "error")
                            continue
                        except sqlite3.IntegrityError:
                            db.rollback()
                            stats["skipped"] += 1
                            if on_progress:
                                await on_progress(current, total, f"Identifier conflict — {title}", "skipped")
                            continue
                        metadata_policy.record_initial(db, item_id, source_key, provider_fields)
                        stats["added"] += 1
                        fetch_cover = _cover_fill_allowed(db, item_id, source_key)
                        status = "added"

                if on_progress:
                    await on_progress(current, total, title, status)

                # Covers are fill-only, even when descriptive metadata changed.
                if not fetch_cover:
                    continue
                try:
                    cover_resp = await client.get(
                        f"{abs_url}/api/items/{abs_id}/cover",
                        headers=headers,
                        params={"width": 400},
                    )
                    if cover_resp.status_code == 200 and len(cover_resp.content) > 1000:
                        with get_db() as db:
                            metadata_policy.begin_write(db)
                            cover_row = db.execute(
                                "SELECT id, cover_path, cover_review_dismissed FROM items_live WHERE id = ?", (item_id,),
                            ).fetchone()
                            # A person may have picked a cover (or trashed the
                            # item) while the HTTP request was in flight. Check
                            # again under the lock BEFORE touching the file.
                            if cover_row is not None and _cover_fill_allowed(db, item_id, source_key):
                                cover_dest = config.COVERS_DIR / f"{item_id}.jpg"
                                config.COVERS_DIR.mkdir(parents=True, exist_ok=True)
                                cover_dest.write_bytes(cover_resp.content)
                                db.execute(
                                    "UPDATE items SET cover_path = ?, cover_edition_isbn = NULL, cover_edition_language = NULL WHERE id = ?",
                                    (f"covers/{item_id}.jpg", item_id),
                                )
                                metadata_policy.record_cover_applied(db, item_id, source_key)
                except Exception:
                    logger.debug("Failed to download cover for ABS item %s", abs_id, exc_info=True)

    # Infer format links only within the book family, never adaptations.
    _auto_link_items()

    return stats


def get_playback_url(abs_url: str, abs_id: str, public_url: str | None = None) -> str:
    """Construct the browser-facing Audiobookshelf item URL.

    ``abs_url`` stays the API/sync endpoint. ``public_url`` is the optional
    ``abs_public_url`` setting, for deployments where Shelf reaches
    Audiobookshelf over an internal Docker/LAN address while the user's browser
    reaches it through a reverse proxy. Blank falls back to ``abs_url``, which
    is the pre-existing behaviour. Both are read by the caller — this stays a
    pure string builder because one caller maps it over every linked item.
    """
    base_url = (public_url or abs_url).rstrip("/")
    return f"{base_url}/item/{abs_id}"


def _normalize_title(title: str) -> str:
    """Exact Unicode-preserving key; do not discard subtitles or articles."""
    title = " ".join(unicodedata.normalize("NFKC", title or "").casefold().split())
    return title if any(char.isalnum() for char in title) else ""


def _authors_compatible(a: str | None, b: str | None) -> bool:
    """Require a real shared creator, never a missing name or substring hit."""
    if not a or not b:
        return False
    creators_a = {author.name_key for author in authors_svc.parse(a) if author.role is None}
    creators_b = {author.name_key for author in authors_svc.parse(b) if author.role is None}
    creators_a -= _UNKNOWN_CREATORS
    creators_b -= _UNKNOWN_CREATORS
    return bool(creators_a & creators_b)


def auto_link(origin_ids) -> int:
    """Link each origin item to the same work in another format.

    Candidates must be live rows of a different book-family ``media_type``.
    A shared valid ISBN matches first; otherwise a non-origin candidate needs
    meaningful exact-title equality and a real shared creator. Returns the
    number of links inserted. Opens its own connection, so call it only after the
    caller's write transaction has committed (G112).
    """
    origin_set = {int(i) for i in origin_ids}
    if not origin_set:
        return 0
    inserted = 0
    with get_db() as db:
        metadata_policy.begin_write(db)
        # Batched: an ABS library can hold more ids than SQLite binds at once.
        ordered = sorted(origin_set)
        origins = []
        for start in range(0, len(ordered), 500):
            batch = ordered[start:start + 500]
            origins += db.execute(
                "SELECT id, title, authors, isbn, media_type FROM items_live "
                f"WHERE id IN ({','.join('?' * len(batch))})",
                batch,
            ).fetchall()

        for origin in origins:
            if origin["media_type"] not in BOOK_MEDIA_TYPES:
                continue
            # A graphic adaptation is not an interchangeable prose edition,
            # even when it credits the same author and repeats the same title.
            compatible_types = ({"comic", "manga"} if origin["media_type"] in {"comic", "manga"}
                                else {"book", "ebook", "audiobook"})
            norm_title = _normalize_title(origin["title"])

            # Match by ISBN
            if origin["isbn"] and isbn_svc.canonical_isbn_pair(origin["isbn"]):
                matches = db.execute(
                    "SELECT id, media_type FROM items_live WHERE isbn = ? AND id != ? AND media_type != ?",
                    (origin["isbn"], origin["id"], origin["media_type"]),
                ).fetchall()
                matches = [i for i in matches if i["media_type"] in compatible_types]
            else:
                matches = []

            # No title-only fallback, including folded-empty non-Latin titles.
            if not matches and norm_title:
                all_items = db.execute(
                    "SELECT id, title, authors, media_type FROM items_live WHERE id != ?",
                    (origin["id"],),
                ).fetchall()
                matches = [
                    i for i in all_items
                    if i["id"] not in origin_set
                    and i["media_type"] in compatible_types
                    and _normalize_title(i["title"]) == norm_title
                    and i["media_type"] != origin["media_type"]
                    and _authors_compatible(origin["authors"], i["authors"])
                ]

            for match in matches:
                match_id = match["id"] if isinstance(match, dict) else match[0]
                inserted += int(media_groups.link_items(
                    db, origin["id"], match_id, link_type="format",
                ))
    return inserted


def _auto_link_items():
    """Create item_links between items that appear to be the same work in different formats.

    The origins are every live row with an ``abs_id``. Title-based candidates
    exclude those origins; strong ISBNs may also link two ABS book formats.
    """
    with get_db() as db:
        ids = [r[0] for r in db.execute("SELECT id FROM items_live WHERE abs_id IS NOT NULL").fetchall()]
    auto_link(ids)
