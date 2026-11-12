"""Conservative, read-only format-link proposals from the current catalogue.

Titles are compared whole, preserving Unicode and subtitles. A title is never
evidence on its own: a named creator must overlap, or a validated work/edition
identifier must agree. Prose books and music releases are separate families;
graphic adaptations, films, games and periodicals are deliberately not guessed.
Nothing here creates links, merges items, refreshes providers or installs schema.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from itertools import combinations
from uuid import UUID

from app import i18n
from app.config import MUSIC_MEDIA_TYPES
from app.services import authors, format_groups
from app.services.isbn import canonical_isbn_pair


MAX_SUGGESTIONS = 200
MAX_PAIR_CHECKS = 6000
_PROSE_TYPES = frozenset({"book", "ebook", "audiobook"})
_UNKNOWN_CREATORS = frozenset({
    "unknown", "unknown author", "unknown artist", "various", "various artists",
    "various authors", "anonymous", "n a", "na", "none", "not available",
})
_COLUMNS = """i.id, i.title, i.subtitle, i.authors, i.media_type, i.isbn,
    i.isbn10, i.hardcover_book_id, m.artist_credit,
    m.musicbrainz_release_group_id"""
_FINGERPRINT_FIELDS = (
    "id", "title", "subtitle", "authors", "media_type", "isbn", "isbn10",
    "hardcover_book_id", "artist_credit", "musicbrainz_release_group_id",
)


def _whole(value) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def creator_keys(value: str | None, *, include_roles: bool = False) -> frozenset[str]:
    """Named, whole-creator identities, never initials/surname-token guessing.

    A shared translator, narrator or cover illustrator is not evidence that two
    books contain the same work. Unknown/anonymous placeholders are not people.
    """
    return frozenset(
        entry.name_key for entry in authors.parse(value)
        if (include_roles or entry.role is None) and entry.name_key not in _UNKNOWN_CREATORS
        and any(char.isalnum() for char in entry.name_key)
    )


def _family(media_type: str) -> str | None:
    if media_type in _PROSE_TYPES:
        return "book"
    if media_type in MUSIC_MEDIA_TYPES:
        return "music"
    return None


def _hardcover_id(value) -> str | None:
    text = str(value or "").strip()
    return text if re.fullmatch(r"[1-9][0-9]*", text) else None


def _release_group(value) -> str | None:
    try:
        group = UUID(str(value or "").strip())
    except ValueError:
        return None
    return str(group) if group.int else None


def _isbns(row) -> frozenset[str]:
    found = set()
    for column in ("isbn", "isbn10"):
        value = str(row[column] or "").strip()
        # canonical_isbn_pair's legacy normalizer drops arbitrary letters.
        # Here an unrelated barcode/URL containing digits must not become an ISBN.
        if value and re.fullmatch(r"[0-9Xx -]+", value):
            pair = canonical_isbn_pair(value)
            if pair:
                found.add(pair[0])
    return frozenset(found)


def _prepare(row) -> dict:
    item = dict(row)
    item["family"] = _family(item["media_type"])
    title = _whole(item["title"])
    item["title_key"] = ((title, _whole(item["subtitle"]))
                         if any(c.isalnum() for c in title) else None)
    item["creator_keys"] = creator_keys(item["authors"])
    if item["family"] == "music":
        item["creator_keys"] |= creator_keys(item["artist_credit"])
    item["hardcover_key"] = _hardcover_id(item["hardcover_book_id"])
    item["isbn_keys"] = _isbns(item)
    item["release_group_key"] = _release_group(item["musicbrainz_release_group_id"])
    return item


def _candidate(a: dict, b: dict) -> dict | None:
    if not a["family"] or a["family"] != b["family"] or a["id"] == b["id"]:
        return None
    a, b = sorted((a, b), key=lambda item: item["id"])
    evidence = []
    if a["family"] == "book":
        # Conflicting known work identities outweigh an ambiguous shared title.
        if a["hardcover_key"] and b["hardcover_key"]:
            if a["hardcover_key"] != b["hardcover_key"]:
                return None
            evidence.append({"kind": "hardcover_book_id", "value": a["hardcover_key"],
                             "label": i18n.gettext("Same Hardcover book ID")})
        for isbn in sorted(a["isbn_keys"] & b["isbn_keys"]):
            evidence.append({"kind": "isbn", "value": isbn,
                             "label": i18n.gettext("Same validated ISBN")})
    else:
        if a["release_group_key"] and b["release_group_key"]:
            if a["release_group_key"] != b["release_group_key"]:
                return None
            evidence.append({"kind": "musicbrainz_release_group", "value": a["release_group_key"],
                             "label": i18n.gettext("Same MusicBrainz release group")})

    overlap = sorted(a["creator_keys"] & b["creator_keys"])
    if a["title_key"] and a["title_key"] == b["title_key"] and overlap:
        evidence.append({"kind": "title_creator", "value": ", ".join(overlap),
                         "label": i18n.gettext("Whole title and subtitle agree; shared named creator")})
    if not evidence:
        return None

    # Include raw relevant metadata, not updated_at: changing notes does not
    # invalidate a review, but changing a displayed title or a work ID does.
    snapshot = {
        "version": 1,
        "items": [{field: item[field] for field in _FINGERPRINT_FIELDS} for item in (a, b)],
        "evidence": [{"kind": part["kind"], "value": part["value"]} for part in evidence],
    }
    fingerprint = hashlib.sha256(json.dumps(
        snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()

    def display(item):
        return {field: item[field] for field in (
            "id", "title", "subtitle", "authors", "media_type", "artist_credit",
        )}

    return {
        "item_a_id": a["id"], "item_b_id": b["id"],
        "item_a": display(a), "item_b": display(b), "family": a["family"],
        "evidence": evidence, "evidence_key": fingerprint,
        "explanation": "; ".join(f"{part['label']}: {part['value']}" for part in evidence),
    }


def _valid_pair(a, b) -> bool:
    return all(isinstance(value, int) and not isinstance(value, bool)
               and 0 < value <= 2**63 - 1 for value in (a, b)) and a != b


def evidence_for_pair(db, a: int, b: int) -> dict | None:
    """Current metadata evidence, not graph eligibility (also used on retries)."""
    if not _valid_pair(a, b):
        return None
    a, b = sorted((a, b))
    rows = db.execute(
        f"SELECT {_COLUMNS} FROM items_live i LEFT JOIN music_releases m ON m.item_id = i.id "
        "WHERE i.id IN (?, ?) ORDER BY i.id", (a, b),
    ).fetchall()
    if len(rows) != 2:
        return None
    return _candidate(*(_prepare(row) for row in rows))


def candidate_for_pair(db, a: int, b: int) -> dict | None:
    """Current eligible evidence, ignoring dismissals; None for linked/dead pairs.

    Call under the caller's write lock when acting on this result. All direct
    edges block proposals, so a related/adaptation edge can never be converted.
    """
    proposal = evidence_for_pair(db, a, b)
    if proposal is None:
        return None
    a, b = proposal["item_a_id"], proposal["item_b_id"]
    if db.execute(
        "SELECT 1 FROM item_links WHERE (item_a_id = ? AND item_b_id = ?) "
        "OR (item_a_id = ? AND item_b_id = ?)", (a, b, b, a),
    ).fetchone():
        return None
    if b in format_groups.format_ids(db, a):
        return None
    return proposal


def _graph(db):
    """Use the shared format-only components, never all-edge related_ids."""
    component = {item_id: members[0] for members in format_groups.format_components(db)
                 for item_id in members}
    direct = {
        tuple(sorted((edge["item_a_id"], edge["item_b_id"]))) for edge in db.execute(
        "SELECT e.item_a_id, e.item_b_id FROM item_links e "
        "JOIN items_live a ON a.id = e.item_a_id JOIN items_live b ON b.id = e.item_b_id "
        "WHERE e.link_type != 'format' "
        "ORDER BY e.item_a_id, e.item_b_id"
        )
    }
    return direct, lambda item_id: component.get(item_id, item_id)


def _bucket_keys(item):
    family = item["family"]
    if family == "book":
        if item["hardcover_key"]:
            yield (0, family, item["hardcover_key"])
        for isbn in sorted(item["isbn_keys"]):
            yield (1, family, isbn)
    elif item["release_group_key"]:
        yield (0, family, item["release_group_key"])
    if item["title_key"]:
        # The scan is intentionally conservative and bounded even for malformed
        # provider credits with hundreds of comma-separated contributors.
        for creator in sorted(item["creator_keys"])[:32]:
            yield (2, family, *item["title_key"], creator)


def review(db, limit: int = 100) -> dict:
    """Bounded deterministic proposals and currently effective dismissals.

    Catalogue/edge reads are linear; pairs are considered only within evidence
    buckets, with a hard visit budget even for one enormous ambiguous bucket.
    Counts are deliberately bounded scan counts, not alleged catalogue totals.
    """
    limit = max(0, min(int(limit), MAX_SUGGESTIONS))
    if not limit:
        return {"suggestions": [], "dismissed": [], "scan_limited": False}
    types = sorted(_PROSE_TYPES | MUSIC_MEDIA_TYPES)
    placeholders = ",".join("?" for _ in types)
    items = {
        row["id"]: _prepare(row) for row in db.execute(
            f"SELECT {_COLUMNS} FROM items_live i LEFT JOIN music_releases m ON m.item_id = i.id "
            f"WHERE i.media_type IN ({placeholders}) ORDER BY i.id", types,
        )
    }
    direct, root = _graph(db)
    dismissals = {
        (row["item_a_id"], row["item_b_id"]): row["evidence_key"]
        for row in db.execute(
            "SELECT d.item_a_id, d.item_b_id, d.evidence_key FROM connection_dismissals d "
            "JOIN items_live a ON a.id = d.item_a_id JOIN items_live b ON b.id = d.item_b_id "
            "ORDER BY d.item_a_id, d.item_b_id"
        )
    }
    buckets = {}
    for item in items.values():
        for key in _bucket_keys(item):
            buckets.setdefault(key, []).append(item["id"])
    seen = set()
    proposals, dismissed = [], []
    visits = 0
    limited = False
    for key in sorted(buckets):
        for a, b in combinations(buckets[key], 2):
            visits += 1
            if visits > MAX_PAIR_CHECKS:
                limited = True
                break
            if (a, b) in seen:
                continue
            seen.add((a, b))
            if (a, b) in direct or root(a) == root(b):
                continue
            proposal = _candidate(items[a], items[b])
            if proposal is None:
                continue
            if dismissals.get((a, b)) == proposal["evidence_key"]:
                if len(dismissed) < limit:
                    dismissed.append(proposal)
                else:
                    limited = True
            elif len(proposals) < limit:
                proposals.append(proposal)
            else:
                limited = True
            if len(proposals) == limit and len(dismissed) == limit:
                limited = True
                break
        if visits > MAX_PAIR_CHECKS or (len(proposals) == limit and len(dismissed) == limit):
            break
    return {"suggestions": proposals, "dismissed": dismissed, "scan_limited": limited}


def suggestions(db, limit: int = 100) -> list[dict]:
    """Public read-only proposal list, capped at 200; dismissals match evidence."""
    return review(db, limit)["suggestions"]
