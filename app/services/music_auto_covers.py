"""Fill missing music artwork only; never identify or merge a pressing.

Saved release references take priority. Artist/title fallback is cosmetic album
art, not edition evidence. All network work is outside the publication lock.
"""

import asyncio
import logging
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from app.config import COVERS_DIR, MUSIC_MEDIA_TYPES, SECRET_ENV_VARS
from app.database import get_db, get_setting
from app.services import cover_sync_policy, discogs, music_artwork, music_matching, musicbrainz, provider_result


logger = logging.getLogger(__name__)
MAX_FRONT_ATTEMPTS = 2
MAX_DISCOGS_DETAILS = 3
LOOKUP_TIMEOUT = 90


class _ChangedArtwork(Exception):
    pass


def _mbid(value):
    return music_matching.release_id(value) if isinstance(value, str) and len(value) == 36 else None


def _state(db, item_id):
    row = db.execute(
        "SELECT id, title, authors, media_type, upc, publish_year, cover_path, "
        "cover_review_dismissed, updated_at FROM items_live WHERE id = ?", (item_id,),
    ).fetchone()
    if not row or row["media_type"] not in MUSIC_MEDIA_TYPES or row["cover_path"] or row["cover_review_dismissed"]:
        return None
    settings = dict(db.execute("SELECT key, value FROM settings WHERE key IN ('feature.music', 'discogs_token')"))
    decision = cover_sync_policy.decision(db, item_id)
    if settings.get("feature.music") == "0" or decision is True:
        return None
    item = music_artwork.context(db, row)
    if not (_mbid(item.get("musicbrainz_release_id")) or _mbid(item.get("musicbrainz_release_group_id"))
            or discogs._public_release_id(item.get("discogs_release_id"))
            or (item.get("title") and (item.get("authors") or item.get("artist_credit")))):
        return None
    # Raw configuration material is compared in memory only, never persisted,
    # hashed into provenance, logged or sent to image hosts. No decryption here:
    # this same check also runs under the final writer lock.
    env_name = SECRET_ENV_VARS.get("discogs_token")
    revision = (tuple(sorted(item.items())), settings.get("feature.music", ""), settings.get("discogs_token", ""),
                os.environ.get(env_name, "") if env_name else "", decision)
    return item, revision


def enqueue_missing(item_id):
    """Optional postcommit handoff; a queue failure must not undo a saved item."""
    try:
        with get_db() as db:
            db.execute("BEGIN")
            state = _state(db, item_id)
        if state is None:
            return False
        from app.services import cover_queue
        cover_queue.enqueue(item_id)
        return True
    except Exception:
        logger.warning("Could not queue missing music artwork for item %d", item_id, exc_info=True)
        return False


def _current(item_id, expected):
    with get_db() as db:
        db.execute("BEGIN")
        state = _state(db, item_id)
    if state is None or state[1] != expected:
        raise _ChangedArtwork


def _music_url(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == "https" and parsed.hostname is not None
                and parsed.username is None and parsed.password is None and parsed.port in (None, 443)
                and not parsed.fragment and (parsed.hostname in {"coverartarchive.org", "i.discogs.com"}
                                            or parsed.hostname.endswith(".us.archive.org")))
    except ValueError:
        return False


def _fronts(result):
    if not isinstance(result, provider_result.ProviderResult) or not result.found or not isinstance(result.payload, list):
        return []
    return [image for image in result.payload[:24] if isinstance(image, dict)
            and image.get("front") is True and _music_url(image.get("url"))][:MAX_FRONT_ATTEMPTS]


def _discogs_fronts(result, identity):
    if not result.found or not isinstance(result.payload, dict):
        return []
    if discogs._public_release_id(result.payload.get("discogs_release_id")) != identity:
        return []
    images = result.payload.get("images")
    if not isinstance(images, list):
        return []
    return [image for image in images[:12] if isinstance(image, dict)
            and image.get("label") == "Primary" and _music_url(image.get("url"))][:MAX_FRONT_ATTEMPTS]


def _same_album(item, release):
    title = music_matching._text(item.get("title"))
    artist = music_matching._text(item.get("authors") or item.get("artist_credit"))
    return bool(title and artist and title == music_matching._text(release.get("title"))
                and artist == music_matching._text(release.get("artist_credit")))


async def _album_sources(item, client, token, *, skip_group=None, skip_discogs=None):
    title, artist = item.get("title") or "", item.get("authors") or item.get("artist_credit") or ""
    if not title.strip() or not artist.strip():
        return
    # Any format: another edition can supply album artwork without claiming its
    # pressing identity. Search summaries alone never attach provider bindings.
    answer = await music_matching.search_catalogue(client, title=title[:200], artist=artist[:200],
                                                   discogs_token=token)
    if not answer.found:
        return
    matches = [row for row in answer.payload if row.get("provider") == "musicbrainz" and _same_album(item, row)]
    groups = {_mbid(row.get("musicbrainz_release_group_id")) for row in matches}
    if len(groups) > 1:
        return  # Conflicting album evidence: leave this item for human review.
    if groups and None not in groups and next(iter(groups)) != skip_group:
        group = next(iter(groups))
        yield _fronts(await musicbrainz.cover_art(group, client, group=True))
    verified, masters, uncertain = [], set(), False
    candidates = [row for row in answer.payload if row.get("provider") == "discogs"]
    checked = set()
    for row in candidates:
        identity = discogs._public_release_id(row.get("discogs_release_id"))
        if not identity or identity == skip_discogs or identity in checked:
            continue
        if len(checked) >= MAX_DISCOGS_DETAILS:
            break
        checked.add(identity)
        result = await discogs.lookup_public_release(identity, client, token=token)
        if (not result.found or not isinstance(result.payload, dict)
                or discogs._public_release_id(result.payload.get("discogs_release_id")) != identity
                or not _same_album(item, result.payload)):
            continue
        master = discogs._public_release_id(result.payload.get("discogs_master_id"))
        if not master:
            uncertain = True
            continue
        masters.add(master)
        verified.extend(_discogs_fronts(result, identity))
    if len(masters) == 1 and not uncertain:
        yield verified[:MAX_FRONT_ATTEMPTS]


def _discard_staged(path, item_id):
    # Only our unique downloader's exact relative naming domain is removable.
    name = Path(path).name
    if path == f"covers/{name}" and re.fullmatch(rf"{item_id}-[0-9a-f]{{32}}\.jpg", name):
        (COVERS_DIR / name).unlink(missing_ok=True)


def _publish(item_id, path, expected):
    may_be_referenced = False
    try:
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = _state(db, item_id)
            if current is None or current[1] != expected:
                raise _ChangedArtwork
            # Retain the unique staged file if a write/commit acknowledgement is
            # ambiguous. An orphan is safer than deleting possibly committed art.
            may_be_referenced = True
            changed = db.execute(
                "UPDATE items SET cover_path = ?, cover_edition_isbn = NULL, cover_edition_language = NULL, "
                "updated_at = datetime('now') WHERE id = ? AND deleted_at IS NULL AND COALESCE(cover_path, '') = ''",
                (path, item_id),
            ).rowcount
            if changed != 1:
                may_be_referenced = False
                raise _ChangedArtwork
        return path
    finally:
        if not may_be_referenced:
            _discard_staged(path, item_id)


async def resolve(item_id, client, *, expected_release_id=None):
    """Return newly stored artwork or None; no identity/release/history writes."""
    with get_db() as db:
        db.execute("BEGIN")
        state = _state(db, item_id)
        if expected_release_id is not None and (state is None or not _mbid(expected_release_id)
                or _mbid(state[0].get("musicbrainz_release_id")) != _mbid(expected_release_id)):
            return None
        token = (get_setting(db, "discogs_token") or "") if state is not None else ""
    if state is None:
        return None
    item, expected = state
    seen = set()

    async def attempt(images):
        for image in images[:MAX_FRONT_ATTEMPTS]:
            url = image["url"]
            if url in seen:
                continue
            _current(item_id, expected)
            seen.add(url)
            path = await music_artwork.download(item_id, url)
            if path:
                return _publish(item_id, path, expected)
        return None

    try:
        async with asyncio.timeout(LOOKUP_TIMEOUT):
            mbid = _mbid(item.get("musicbrainz_release_id"))
            if mbid:
                path = await attempt(_fronts(await musicbrainz.cover_art(mbid, client)))
                if path:
                    return path
            identity = discogs._public_release_id(item.get("discogs_release_id"))
            if identity:
                result = await discogs.lookup_public_release(identity, client, token=token)
                path = await attempt(_discogs_fronts(result, identity))
                if path:
                    return path
            group = _mbid(item.get("musicbrainz_release_group_id"))
            if group:
                path = await attempt(_fronts(await musicbrainz.cover_art(group, client, group=True)))
                if path:
                    return path
            async for images in _album_sources(item, client, token, skip_group=group, skip_discogs=identity):
                path = await attempt(images)
                if path:
                    return path
    except (_ChangedArtwork, TimeoutError):
        return None
    return None
