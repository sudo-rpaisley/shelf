#!/usr/bin/env python3
"""Tripwire lint: every read of `items` or `item_copies` in app/ must go
through its TEMP view (app/database.py::get_db()), not the physical table.

T1 added `deleted_at` to `items`/`item_copies`; the earlier soft-delete-seam
plan's own T2 made get_db() create a per-connection `CREATE TEMP VIEW IF NOT
EXISTS items_live AS SELECT * FROM items WHERE deleted_at IS NULL` and T4-T6
repointed the ~172 existing direct reads in app/ onto that view, so the
items census is 0 and this script has guarded it there ever since — a new
direct read of `items` in app/ reds it.

This plan's own T1 added the second view, `copies_live` — a copy is live
only if it AND its item are untrashed (see the CREATE in get_db()). Its T2
taught the same script the second relation, item_copies -> copies_live, as a
parallel set of functions (`find_copy_violations`, `copy_allowlist_mismatches`)
that share the items functions' normaliser and span-matcher rather than
duplicating them. T3-T5 repointed every non-exempt read, driving the copies
census to 0, and this task (T6) is what makes main() fail on it — a new
direct read of item_copies in app/ now reds the build exactly as a new direct
read of items already does.

Integrated fork exceptions below name exact physical identity, authorization,
lifecycle and migration statements. They do not grant file/function access:
normal catalogue reads still use the actor-scoped views. Dynamic SQL entries
retain source placeholders, not their runtime expansions.

Only app/**/*.py is scanned. Tests legitimately read the physical tables (a
later plan needs them to) and are out of scope.

G53: prose in app/ that needs to talk about this should say "the items
table"/"items_live" or "the item_copies table"/"copies_live", never write
the literal `FROM items` / `JOIN items` / `FROM item_copies` / `JOIN
item_copies` construct in a comment or docstring — this guard strips
`#`-comment *lines* but not inline prose inside a docstring, and a comment
quoting either construct must not trip it.

Run directly (exit 1 on items violations, items mismatches, copies
violations, or copies allowlist mismatches) or via
tests/test_items_live_lint.py.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Strip a string literal's prefix together with its opening quote, before
# the bare-quote strip below. Skipping this turns `f"FROM items i "` into
# `fFROM items i ` and `\bFROM` no longer matches, because `f` and `F` are
# both word characters with no boundary between them. Three statements have
# exactly this shape today: app/routers/items.py:901, app/routers/pages.py:70,
# app/routers/series.py:83. (An equivalent fix is a prefix-aware matcher —
# `(?<!\w)(?:[rubf]{1,2})?(FROM|JOIN)\s+items\b` — this file picks the
# prefix-strip approach instead.)
_STRING_PREFIX_QUOTE = re.compile(r'\b[rRbBuUfF]{1,2}"')

# The forbidden construct. Excluded, each pinned in
# tests/test_items_live_lint.py even though only the first exists in app/
# today:
#   - preceded by "DELETE " (`DELETE FROM items ...`) — a write, stays on
#     the physical table.
#   - preceded by "yield " (`yield from items`).
#   - followed by " import" (`from items import x`).
# `\b` does not split on `_`, so items_live / item_copies / item_tags /
# item_links never match.
_ITEMS_READ = re.compile(
    r"(?<!DELETE )(?<!yield )\b(FROM|JOIN)\s+items\b(?! import)", re.I
)

VIOLATION_MSG = (
    "{path}:{line}: reads the items table directly — read through "
    "items_live (writes and the allowlisted lookups in "
    "scripts/check_items_live.py stay on items; prose quoting the "
    "FROM/JOIN construct inside a docstring trips this too, see G53)"
)

# The item_copies twin of _ITEMS_READ. No `yield`/`import` exclusion is
# needed here — nothing in app/ does `yield from item_copies` or
# `from item_copies import x` today, unlike the items case (see the
# `_ITEMS_READ` comment above) — but the DELETE exclusion still applies:
# app/services/item_copies.py has three `DELETE FROM item_copies ...`
# statements (writes, stay on the physical table). `\b` does not split on
# `_`, so `copies_live` never matches.
_COPIES_READ = re.compile(r"(?<!DELETE )\b(FROM|JOIN)\s+item_copies\b", re.I)

COPIES_VIOLATION_MSG = (
    "{path}:{line}: reads the item_copies table directly — read through "
    "copies_live (writes and the allowlisted lookups in "
    "scripts/check_items_live.py stay on item_copies; prose quoting the "
    "FROM/JOIN construct inside a docstring trips this too, see G53)"
)

#: repo-relative path -> {statement substring: how many reads it may suppress}.
#: A substring identifies a legitimate direct read of the physical `items`
#: table, and is normalised the same way the scanner reads source (quotes
#: stripped, whitespace collapsed to single spaces). Mirrors
#: RAW_UPDATE_ALLOWLIST in tests/test_item_write.py — each entry was read at
#: its call site and carries a one-line reason. Allowlisted by
#: repository-relative path, never by basename (G88).
#:
#: An entry must SPAN the read it excuses (see `_spanning`), so it has to
#: quote enough of its statement to contain the FROM/JOIN clause outright.
#: Keep each one long enough that a *different* statement cannot reproduce
#: it and allowlist itself: the count beside it is the structural half of
#: that guard, and `allowlist_mismatches()` reds when a new read rides along
#: inside an existing entry's text.
ALLOWLIST: dict[str, dict[str, int]] = {
    "app/database.py": {
        # The items_live view CREATE in get_db() itself — the seam reads the
        # physical table by definition. Without this entry the lint reds on
        # the statement that defines it.
        "SELECT * FROM items WHERE deleted_at IS NULL": 1,
        # The copies_live view CREATE in get_db() — it joins the items
        # relation so that trashing an item hides its copies with no write
        # to them, so the seam's own definition reads the physical table.
        "SELECT c.* FROM item_copies c JOIN items i ON i.id = c.item_id "
        "WHERE c.deleted_at IS NULL AND i.deleted_at IS NULL": 1,
        # Published fork migrations 20/21: each complete collision subquery
        # mirrors UNIQUE(upc, media_type), before request views exist.
        "SELECT 1 FROM items o WHERE o.upc = '0' || items.upc "
        "AND o.media_type = items.media_type": 1,
        "SELECT 1 FROM items o WHERE o.upc = items.isbn "
        "AND o.media_type = items.media_type": 1,
        # Immutable upstream26 source evidence, NOT the integrated node-domain
        # seed. The bridge executes the adapted statement below instead.
        "SELECT i.id, 1, i.location_id, 1 FROM items i "
        "WHERE i.owned = 1 AND i.location_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM item_copies c WHERE c.item_id = i.id)": 1,
        # Immutable upstream36 / bridge79: one-time shared acquisition seed,
        # not an HTTP personal-wishlist read or an ensure-time replay.
        "SELECT (SELECT id FROM lists WHERE slug = 'wishlist'), id "
        "FROM items WHERE owned = 0": 1,
        # Published fork42/55/61: primary series, private-state snapshot for
        # existing users, and item-library binding. Admission/pending history
        # authorizes these one-time seeds; scoped views do not yet exist.
        "SELECT id, TRIM(series_name), series_position, 1 FROM items "
        "WHERE series_name IS NOT NULL AND TRIM(series_name) != '' "
        "ON CONFLICT(item_id, series_name) DO UPDATE SET "
        "position = excluded.position, is_primary = 1": 1,
        "SELECT u.id, i.id, i.reading_status, i.date_started, i.date_finished, "
        "CASE WHEN i.owned = 0 THEN 1 ELSE 0 END, 0 FROM users u CROSS JOIN items i "
        "WHERE i.reading_status IS NOT NULL OR i.date_started IS NOT NULL "
        "OR i.date_finished IS NOT NULL OR i.owned = 0": 1,
        "INSERT OR IGNORE INTO library_items (item_id, library_id) "
        "SELECT id, 1 FROM items": 1,
        # Adapted upstream26 / bridge69: ONLY explicit legacy->node binding,
        # eligible owned physical media, no existing copy history (even Trash),
        # and no replay after the holdings-foundation marker.
        "INSERT INTO item_copies (item_id, copy_number, location_id, is_primary) "
        "SELECT i.id, 1, n.id, 1 FROM items i "
        "LEFT JOIN location_nodes n ON n.legacy_location_id = i.location_id "
        "WHERE i.owned = 1 AND i.media_type IN "
        "('book','kids_book','vinyl','cassette','cd','music_other', "
        "'dvd','video_game','comic','manga','magazine') "
        "AND NOT EXISTS (SELECT 1 FROM item_copies c WHERE c.item_id = i.id) "
        "AND NOT EXISTS (SELECT 1 FROM settings "
        "WHERE key = 'holdings_foundation_backfill_v1' AND value = '1')": 1,
        # Bridge107: three physical inline-identity arms in ONE seed, all
        # required to retain hidden/trashed provider attachments. Count=3 is
        # the ABS/RomM/Komga UNION, not a budget for future matching statements.
        "INSERT INTO digital_holdings (item_id, provider, external_id, library_id) "
        "SELECT item_id, provider, external_id, library_id FROM ( "
        "SELECT id AS item_id, 'audiobookshelf' AS provider, abs_id AS external_id, "
        "abs_library_id AS library_id FROM items "
        "UNION ALL SELECT id, 'romm', romm_id, romm_platform_id FROM items "
        "UNION ALL SELECT id, 'komga', komga_id, komga_library_id FROM items "
        ") legacy WHERE external_id IS NOT NULL AND TRIM(external_id) != '' "
        "AND NOT EXISTS (SELECT 1 FROM digital_holdings d WHERE d.item_id = legacy.item_id "
        "AND d.provider = legacy.provider AND d.external_id = legacy.external_id) "
        "AND NOT EXISTS (SELECT 1 FROM settings "
        "WHERE key = 'holdings_foundation_backfill_v1' AND value = '1')": 3,
        # Bridge108/109: positively validated provider evidence only, before
        # views; no title/ISBN adoption or plural-identity collapse. Komga has
        # TWO reads in this exact statement (inline arm + kind/series join).
        "INSERT INTO romm_records (romm_id, item_id, platform_id, created_at, updated_at) "
        "SELECT external_id, item_id, MIN(CASE WHEN TRIM(library_id) != '' THEN library_id END), "
        "MIN(created_at), MAX(updated_at) FROM ( "
        "SELECT romm_id AS external_id, id AS item_id, romm_platform_id AS library_id, "
        "created_at, updated_at FROM items "
        "UNION ALL SELECT external_id, item_id, library_id, created_at, updated_at "
        "FROM digital_holdings WHERE provider = 'romm' "
        ") evidence WHERE external_id IS NOT NULL AND TRIM(external_id) != '' "
        "GROUP BY external_id, item_id": 1,
        "INSERT INTO komga_records "
        "(komga_id, item_id, library_id, series_id, kind, created_at, updated_at) "
        "SELECT e.external_id, e.item_id, MIN(CASE WHEN TRIM(e.library_id) != '' THEN e.library_id END), "
        "CASE WHEN e.external_id = i.komga_id THEN i.komga_series_id END, "
        "CASE WHEN i.media_type IN ('manga', 'digital_manga') THEN 'manga' ELSE 'comic' END, "
        "MIN(e.created_at), MAX(e.updated_at) FROM ( "
        "SELECT komga_id AS external_id, id AS item_id, komga_library_id AS library_id, "
        "created_at, updated_at FROM items "
        "UNION ALL SELECT external_id, item_id, library_id, created_at, updated_at "
        "FROM digital_holdings WHERE provider = 'komga' "
        ") e JOIN items i ON i.id = e.item_id "
        "WHERE e.external_id IS NOT NULL AND TRIM(e.external_id) != '' "
        "GROUP BY e.external_id, e.item_id": 2,
        # Read-only lineage admission must inspect ALL physical connector
        # evidence, including Trash, before initialization installs views.
        # Include the call boundary: a bare SELECT * prefix would also excuse
        # any adjacent SELECT * ... WHERE read in this same file.
        "db.execute(SELECT * FROM items)": 1,
        # gc_orphaned_series_meta: a soft-deleted item keeps its series
        # alive so a restore finds it intact.
        "SELECT 1 FROM items WHERE series_name = ? COLLATE NOCASE": 1,
    },
    "app/services/item_write.py": {
        # Trusted actorless Trash maintenance retains ownership validation and
        # reindexes exactly the rows actually written. Both physical branches
        # explicitly require current_actor() is None; HTTP uses items_live and
        # all-target require_item_role before mutation. No lifecycle/copy seed.
        "SELECT 1 FROM items WHERE id IN ({marks}) AND owned = 1": 1,
        "SELECT id FROM items WHERE id IN ({marks})": 1,
        "SELECT id, isbn, upc, media_type FROM items WHERE {where}": 1,
        # Non-disclosing UNIQUE-slot census, including hidden live/Trash
        # identities. Every hit is checked with has_item_role(viewer) before
        # the funnel may expose/restore a twin; {field} is source text.
        "SELECT id FROM items WHERE {field} = ? AND media_type = ? AND id != ?": 1,
        # _trashed_twin — the insert funnel's collision-with-Trash lookup.
        # It must read the physical table because the UNIQUE constraints do:
        # a trashed row still holds its (isbn, media_type) / (upc,
        # media_type) slot, so items_live would hide exactly the row whose
        # slot the INSERT is about to collide with (G107).
        # `LIMIT 1` is load-bearing *here*, not only in SQLite: without it
        # this text is a prefix of refuse_trash_collision's statements below
        # and would silently span those reads too, which is the exact
        # ride-along G105 exists to catch.
        "SELECT id, title FROM items "
        "WHERE isbn = ? AND media_type = ? AND deleted_at IS NOT NULL "
        "LIMIT 1": 1,
        "SELECT id, title FROM items "
        "WHERE upc = ? AND media_type = ? AND deleted_at IS NOT NULL "
        "LIMIT 1": 1,
        # refuse_trash_collision — the update funnel's mirror of the same
        # rule. `AND id != ?` excludes the row being edited, so re-saving an
        # item's own unchanged identifiers is never a collision. Physical for
        # the same reason as the two above.
        "SELECT id, title FROM items WHERE isbn = ? AND media_type = ? "
        "AND deleted_at IS NOT NULL AND id != ?": 1,
        "SELECT id, title FROM items WHERE upc = ? AND media_type = ? "
        "AND deleted_at IS NOT NULL AND id != ?": 1,
        # trashed_title — names the trashed row blocking an edit, for the
        # edit page's refusal arm. Physical by definition: a live row has no
        # title to report here.
        "SELECT title FROM items WHERE id = ? AND deleted_at IS NOT NULL": 1,
    },
    "app/services/author_index.py": {
        # reindex_items: the author index covers trashed rows too (soft
        # delete writes nothing to children), so the existence check before
        # linking finds the rows the view hides (G107).
        "SELECT id FROM items WHERE id IN ({marks})": 1,
        # rebuild_all: the boot build indexes every physical row, trashed
        # ones included, for the same reason.
        "SELECT id, authors FROM items WHERE authors IS NOT NULL AND authors <> ''": 1,
    },
    "app/services/trash.py": {
        # _count: TWO item reads in this exact combined retention count
        # (item half + copy-parent join). Scoped callers bind editor ACLs;
        # the unscoped total is only the global-admin nag/marker, never a list.
        "SELECT (SELECT COUNT(*) FROM items i WHERE {item_sql} AND ({scope})) + "
        "(SELECT COUNT(*) FROM item_copies c JOIN items i "
        "ON i.id = c.item_id AND i.deleted_at IS NULL "
        "WHERE {copy_sql} AND ({scope})) AS n": 2,
        # Empty expired: admin-only, locked, same retention predicate plus
        # editor scope; individually trashed copies count only for live items.
        "SELECT i.id FROM items i WHERE {item_sql} AND ({scope}) ORDER BY i.id": 1,
        "SELECT c.id FROM item_copies c JOIN items i ON i.id = c.item_id "
        "AND i.deleted_at IS NULL WHERE {copy_sql} AND ({scope}) ORDER BY c.id": 1,
        # Restore-state distinction must see copy AND parent Trash. The bound
        # editor predicate prevents disclosing an inaccessible parent's state.
        "SELECT c.deleted_at AS copy_deleted, i.deleted_at AS item_deleted "
        "FROM item_copies c JOIN items i ON i.id = c.item_id "
        "WHERE c.id = ? AND ({scope})": 1,
        # Admin purge starts from Trash; _atomic_purge locks and checks parent
        # visibility before this guard. It retains scan security snapshots.
        "SELECT id FROM items WHERE id = ? AND deleted_at IS NOT NULL": 1,
        # Trash listing: actor-private projection/join plus explicit editor
        # scope, not a raw household-state projection. Source placeholders
        # are deliberate; do not substitute their runtime SQL here.
        "SELECT {projection}, ({item_cond}) AS expired, "
        "EXISTS(SELECT 1 FROM checkouts ck WHERE ck.item_id = i.id "
        "AND ck.checked_in IS NULL) AS has_open_loan "
        "FROM items i {state_join} WHERE i.deleted_at IS NOT NULL AND ({scope}) "
        "ORDER BY i.deleted_at DESC": 1,
        # Copy Trash keeps its NODE placement and its physical parent's
        # live/trashed state. Full node join + editor scope, never flat IDs.
        "SELECT c.*, i.title AS item_title, (i.deleted_at IS NULL) AS item_live, "
        "(i.deleted_at IS NULL AND ({copy_cond})) AS expired, l.name AS location_name "
        "FROM item_copies c JOIN items i ON i.id = c.item_id "
        "LEFT JOIN location_nodes l ON l.id = c.location_id "
        "WHERE c.deleted_at IS NOT NULL AND ({scope}) "
        "ORDER BY i.id, c.copy_number, c.id": 1,
    },
    "app/services/audiobookshelf.py": {
        # Exact inline + plural-holding attachment census, including hidden
        # and Trash. Conflicting parents refuse; no live/lowest-id winner.
        "SELECT id FROM items WHERE abs_id = ? UNION "
        "SELECT item_id FROM digital_holdings WHERE provider = 'audiobookshelf' AND external_id = ?": 1,
        # The one resolved attachment is checked with require_item_role for
        # a live write; Trash is skipped without restore or metadata/cover work.
        "SELECT * FROM items WHERE id = ?": 1,
    },
    "app/services/komga_records.py": {
        # Exact inline/sidecar/plural attachment identity, including hidden
        # and Trash. Disagreement refuses before any candidate mutation.
        "SELECT id FROM items WHERE komga_id = ? UNION "
        "SELECT item_id FROM komga_records WHERE komga_id = ? UNION "
        "SELECT item_id FROM digital_holdings WHERE provider = 'komga' AND external_id = ?": 1,
        # Resolved attachment: live writes authorize its library; Trash is
        # retained and skipped, never adopted into a fresh candidate's library.
        "SELECT * FROM items WHERE id = ?": 1,
        # _existing_record. The JOIN is part of the read (G107): joining the
        # view is what hid a trashed item's record, and komga_id is the
        # record table's PRIMARY KEY — so a hidden record meant a fresh
        # insert, a PK violation, and the whole block rolled back.
        "SELECT kr.*, i.source, i.media_type, i.deleted_at FROM komga_records kr "
        "JOIN items i ON i.id = kr.item_id WHERE kr.komga_id = ?": 1,
    },
    "app/services/romm_records.py": {
        # Same all-attachment identity census as Komga, not a browse query.
        "SELECT id FROM items WHERE romm_id = ? UNION "
        "SELECT item_id FROM romm_records WHERE romm_id = ? UNION "
        "SELECT item_id FROM digital_holdings WHERE provider = 'romm' AND external_id = ?": 1,
        # Live writes authorize the exact parent; Trash remains untouched.
        "SELECT * FROM items WHERE id = ?": 1,
        # _existing_record — same reasoning as Komga's, and RomM has no
        # IntegrityError handler at all, so the rollback was unconditional.
        "SELECT rr.*, i.source, i.deleted_at FROM romm_records rr "
        "JOIN items i ON i.id = rr.item_id WHERE rr.romm_id = ?": 1,
    },
    "app/routers/hardcover.py": {
        # _check_hc_identifiers: physical exact-ID collision probes across
        # hidden live/Trash. Every returned parent must pass editor authority
        # (explicit captured user for workers); hidden hits become generic
        # IdentifierUnavailable. ONE source read, not two runtime field values.
        "SELECT * FROM items WHERE {field} = ?": 1,
    },
    "app/services/hardcover.py": {
        # Actor-free scheduler/account sync must bind a positively resolved
        # owner, that owner's explicit library predicate AND private state,
        # never inherit a broad service view or household status. The captured
        # identity/library set is rechecked under the lock before private writes.
        "SELECT i.id, i.hardcover_book_id, i.media_type, li.library_id, s.reading_status "
        "FROM items i JOIN library_items li ON li.item_id = i.id "
        "LEFT JOIN user_item_state s ON s.item_id = i.id AND s.user_id = ? "
        "WHERE i.deleted_at IS NULL AND i.hardcover_book_id IS NOT NULL "
        "AND i.media_type IN ({','.join('?' * len(family))}) AND ({condition}) "
        "{selection} ORDER BY i.id": 1,
    },
    "app/services/barcode_resolve.py": {
        # resolve() (moved from items_scan_modes._find_item_by_barcode) —
        # the existing-item scan modes must find a row in Trash so they can report it and offer Restore instead of
        # "not in your collection". Ordered live-first: an ISBN held by a
        # live row and a trashed one (different media types) answers live.
        # Each read binds library scope AND the actor's private projection;
        # a hidden match cannot become a scan result or a restore offer.
        "SELECT {projection}, l.name as location_name FROM items i "
        "LEFT JOIN locations l ON i.location_id = l.id "
        "{state_join} WHERE i.isbn = ? AND ({scope}) "
        "ORDER BY i.deleted_at IS NOT NULL, i.id": 1,
        "SELECT {projection}, l.name as location_name FROM items i "
        "LEFT JOIN locations l ON i.location_id = l.id "
        "{state_join} WHERE i.upc = ? AND ({scope}) "
        "ORDER BY i.deleted_at IS NOT NULL, i.id": 1,
        # resolve()'s copy-barcode read — finds the row the view hides, not a
        # UNIQUE prediction: a trashed copy, or a copy of a trashed item, must
        # still resolve so the scan reports it. Physical on both tables (G107).
        "SELECT {projection}, l.name AS location_name, c.id AS copy_id, "
        "c.copy_number, c.location_id AS copy_location_id, "
        "cl.name AS copy_location_name, "
        "COALESCE(i.deleted_at, c.deleted_at) AS trash_stamp, "
        "(i.deleted_at IS NULL AND c.deleted_at IS NOT NULL) AS copy_trashed "
        "FROM item_copies c JOIN items i ON i.id = c.item_id "
        "LEFT JOIN locations l ON i.location_id = l.id "
        "LEFT JOIN location_nodes cl ON c.location_id = cl.id "
        "{state_join} WHERE c.copy_barcode = ? AND ({scope}) "
        "ORDER BY i.deleted_at IS NOT NULL, c.deleted_at IS NOT NULL, c.id": 1,
    },
    "app/routers/items_csv.py": {
        # CSV dedup — same reason as _find_item_by_barcode above. `isbn IN
        # (?, ?)` can match two different rows (the ISBN-13 and ISBN-10 forms
        # can each be held by a different item), and the title/author
        # fallback below is covered by no UNIQUE constraint at all, so both
        # reads order a live row first and only act on a trashed hit when no
        # live row matches (claude-R4, "live wins").
        "SELECT id, deleted_at FROM items WHERE media_type = ? AND isbn IN (?, ?) "
        "ORDER BY deleted_at IS NOT NULL, id LIMIT 1": 1,
        "SELECT id, deleted_at FROM items WHERE TRIM(title) = TRIM(?) COLLATE NOCASE AND "
        "TRIM(COALESCE(authors, '')) = TRIM(?) COLLATE NOCASE AND media_type = ? AND "
        "(isbn IS NULL OR isbn = '') ORDER BY deleted_at IS NOT NULL, id LIMIT 1": 1,
        # CSV dedup on UPC — predicts UNIQUE(upc, media_type)
        # (idx_items_upc_type), which a trashed row still holds (G107).
        "SELECT id, deleted_at FROM items WHERE upc = ? AND media_type = ? "
        "ORDER BY deleted_at IS NOT NULL, id LIMIT 1": 1,
        # CSV carries accessible Trash only where the actor has library edit
        # access; live rows still need viewer access. Personal columns come
        # from the actor's projection/join, not household legacy state.
        "SELECT {projection}, l.name as location_name, {lists.WISHLISTED_SQL} AS wishlisted "
        "FROM items i {private_join} LEFT JOIN locations l ON i.location_id = l.id "
        "WHERE ({condition}) AND (i.deleted_at IS NULL OR ({trash_access})) ORDER BY i.title": 1,
    },
    "app/services/csv_import.py": {
        # match_audiobook's ASIN rung — finds the row the view hides, not a
        # UNIQUE prediction (items.asin is deliberately not unique): a
        # trashed audiobook holding the ASIN must be found so the route
        # restores it rather than creating a twin (G107).
        "SELECT id, deleted_at FROM items WHERE asin = ? AND media_type = 'audiobook' "
        "ORDER BY deleted_at IS NOT NULL, id LIMIT 1": 1,
        # match_audiobook's title rung — same reason as the CSV title
        # fallback in items_csv.py: no UNIQUE constraint behind it, so it
        # orders a live row first ("live wins") and acts on a trashed hit
        # only when no live row matches.
        "SELECT id, deleted_at FROM items WHERE TRIM(title) = TRIM(?) COLLATE NOCASE AND "
        "TRIM(COALESCE(authors, '')) = TRIM(?) COLLATE NOCASE AND "
        "media_type = 'audiobook' AND asin IS NULL "
        "ORDER BY deleted_at IS NOT NULL, id LIMIT 1": 1,
    },
    "app/services/archive.py": {
        # Portable catalogue export intentionally includes accessible Trash.
        # _export_condition enforces viewer/live vs editor/Trash policy;
        # item_projection overlays ONLY the current actor's private state.
        "SELECT {projection}, locations.name AS location_name, "
        "{lists.WISHLISTED_SQL} AS wishlisted "
        "FROM items i {private_join} LEFT JOIN locations ON locations.id = i.location_id "
        "WHERE ({condition}) ORDER BY i.id": 1,
        # Copy export retains Trash and exact NODE-domain provenance, under
        # the same library/lifecycle predicate as its item export.
        "SELECT c.*, n.name AS location FROM item_copies c "
        "JOIN items i ON i.id = c.item_id "
        "LEFT JOIN location_nodes n ON n.id = c.location_id "
        "WHERE ({condition}) ORDER BY c.item_id, c.copy_number, c.id": 1,
        # THREE identical bounds: plan_archive, apply_plan's all-selected
        # authorization preflight, and its row loop's pre-import bound. Each
        # must include high-ID Trash; no IDs or aggregate are exported.
        "SELECT COALESCE(MAX(id), 0) AS m FROM items": 3,
        # Live dedupe must detect hidden identities too, not treat a scoped
        # view miss as permission to create/adopt. _classify (and the legacy
        # alias check) authorizes every hit before returning a verdict; selected
        # mutations receive a separate all-target editor preflight.
        "SELECT id FROM items WHERE deleted_at IS NULL AND isbn IN (?, ?) AND media_type = ? "
        "AND id <= ? ORDER BY id LIMIT 1": 1,
        "SELECT id FROM items WHERE deleted_at IS NULL AND (isbn IS NULL OR isbn = '') "
        "AND media_type = ? AND title = ? COLLATE NOCASE "
        "AND COALESCE(authors, '') = ? COLLATE NOCASE AND id <= ?": 1,
        # Exact collision-only census includes hidden live/Trash UNIQUE slots.
        # _authorize_archive_item turns unauthorized hits into generic refusal.
        "SELECT id FROM items WHERE {field} = ? AND media_type = ? AND id <= ?": 1,
        # _trashed_twin_lookup — finds the row the view hides, so an archive
        # record can be classified `restore` (or left alone in Trash) before
        # anything is written (G85). NOT a UNIQUE-prediction read: the
        # title/author base is covered by no constraint at all, and all three
        # exist to find a trashed row, not to foresee a collision (G107).
        "SELECT id, cover_path FROM items WHERE isbn IN (?, ?) AND media_type = ? "
        "AND deleted_at IS NOT NULL AND id <= ? ORDER BY id LIMIT 1": 1,
        "SELECT id, cover_path FROM items WHERE upc = ? AND media_type = ? "
        "AND deleted_at IS NOT NULL AND id <= ? ORDER BY id LIMIT 1": 1,
        "SELECT id, cover_path FROM items WHERE (isbn IS NULL OR isbn = '') "
        "AND media_type = ? AND title = ? COLLATE NOCASE "
        "AND COALESCE(authors, '') = ? COLLATE NOCASE "
        "AND deleted_at IS NOT NULL AND id <= ? ORDER BY id LIMIT 1": 1,
    },
    "app/services/catalogue_security.py": {
        # The authority primitive itself: native physical identity/liveness
        # must be available for include_deleted checks. It returns a row ONLY
        # after canonical-principal viewer + requested-role checks; unmapped,
        # hidden and missing all deny. This is not a request read shortcut.
        "SELECT * FROM items WHERE id = ?": 1,
    },
    "app/services/libraries.py": {
        # Assignment/move identity guard includes Trash. A mapped existing
        # parent and destination BOTH require editor authority; new_item only
        # admits the insert funnel's already-authorized, not-yet-mapped row.
        "SELECT 1 FROM items WHERE id = ?": 1,
        # Explicit orphan-recovery boundary only: persisted native admin,
        # no bearer capability, same resolved actor and captured session version.
        # Ordinary has_item_role/items_live still deny unmapped items. Capability
        # and listing are exact unmapped selections including Trash; recovery's
        # locked existence check changes only the security mapping.
        "SELECT 1 FROM items i WHERE i.id = ? "
        "AND NOT EXISTS (SELECT 1 FROM library_items li WHERE li.item_id = i.id)": 1,
        "SELECT i.id, i.title, i.media_type, i.deleted_at FROM items i "
        "WHERE NOT EXISTS (SELECT 1 FROM library_items li WHERE li.item_id = i.id) "
        "ORDER BY i.title COLLATE NOCASE, i.id": 1,
        "SELECT i.id FROM items i WHERE i.id = ?": 1,
    },
    "app/services/user_state.py": {
        # ID existence only, never legacy status/dates/notes. get_state calls
        # _authorize first (same actor + item viewer access, no deleted HTTP
        # target). Values then come exclusively from user_item_state(user_id).
        "SELECT id FROM items WHERE id = ?": 1,
    },
    "app/services/tags.py": {
        # Vocabulary admission, not usage counts: unused names remain public;
        # used names require an explicitly library-authorized physical parent.
        # This retains authorized Trash zero-count suggestions without leaking
        # hidden-only or unmapped tag names. Ordinary counts still use live views.
        "NOT EXISTS (SELECT 1 FROM item_tags any_use WHERE any_use.tag_id = t.id) "
        "OR EXISTS (SELECT 1 FROM item_tags permitted_use "
        "JOIN items tag_item ON tag_item.id = permitted_use.item_id "
        "WHERE permitted_use.tag_id = t.id AND ({visible}))": 1,
    },
    "app/services/scan_history.py": {
        # Retained history intersects its immutable library/actor snapshot
        # with the STILL-BOUND parent's current ACL, including Trash. Using a
        # live view here would erase the evidence rather than authorize it.
        "((EXISTS (SELECT 1 FROM libraries history_library "
        "JOIN library_memberships history_member ON history_member.library_id = history_library.id "
        "WHERE history_library.id = {scan_alias}.security_library_id "
        "AND history_member.user_id = ? AND history_member.role IN ('viewer', 'editor'))) "
        "OR ({scan_alias}.security_library_id IS NULL AND {scan_alias}.security_user_id = ?)) "
        "AND ({scan_alias}.item_id IS NULL OR EXISTS (SELECT 1 FROM items physical "
        "WHERE physical.id = {scan_alias}.item_id AND ({current_scope})))": 1,
    },
    "app/services/holdings.py": {
        # Boolean-only exact provider-identity collision check across hidden
        # and Trash attachments. The destination is authorized under the lock;
        # the blocker is never adopted, reparented, restored or disclosed.
        "SELECT 1 FROM items WHERE {inline} = ? AND id != ?": 1,
    },
    "app/services/komga_sync.py": {
        # Non-disclosing link eligibility: refuse if a chosen visible item's
        # provider ID is also attached to ANY other physical/hidden/Trash row.
        "SELECT id AS item_id FROM items WHERE komga_id = ? UNION "
        "SELECT item_id FROM komga_records WHERE komga_id = ? UNION "
        "SELECT item_id FROM digital_holdings WHERE provider = 'komga' AND external_id = ?": 1,
    },
    "app/services/romm_sync.py": {
        # Same non-disclosing all-attachment link eligibility as Komga.
        "SELECT id AS item_id FROM items WHERE romm_id = ? UNION "
        "SELECT item_id FROM romm_records WHERE romm_id = ? UNION "
        "SELECT item_id FROM digital_holdings WHERE provider = 'romm' AND external_id = ?": 1,
    },
    "app/services/item_merge.py": {
        # Structural preflight under the whole-merge savepoint, AFTER both
        # parents pass live editor authorization. Compare original legacy
        # series identity/position with child memberships; conflicts refuse.
        "SELECT series_name, series_position FROM items WHERE id IN (?, ?) "
        "AND series_name IS NOT NULL AND TRIM(series_name) != ''": 1,
        # Same authorized identity preflight: reject moving ANY source copy
        # history, including Trash, into a nonphysical keeper before reparent.
        "SELECT media_type FROM items WHERE id = ?": 1,
    },
    "app/services/locations.py": {
        # Exact legacy-alias detach effect set, including Trash and copies on
        # precise descendant NODES. Every parent must authorize under the
        # write lock BEFORE mutation; a scoped view would silently omit one.
        "WITH RECURSIVE nodes(id) AS ( "
        "SELECT id FROM location_nodes WHERE legacy_location_id = ? UNION "
        "SELECT n.id FROM location_nodes n JOIN nodes p ON n.parent_id = p.id "
        ") SELECT id FROM items WHERE location_id = ? UNION "
        "SELECT c.item_id AS id FROM item_copies c JOIN nodes n ON n.id = c.location_id "
        "ORDER BY id": 1,
    },
    "app/routers/items.py": {
        # Merge all-target preflight under BEGIN IMMEDIATE. Physical existence
        # separates an unknown native no-op from a hidden/Trash existing target
        # that must refuse via require_item_role, not disappear from the batch.
        "SELECT 1 FROM items WHERE id = ?": 1,
    },
    "app/routers/items_common.py": {
        # Retained kids_book/book ISBN identity census, including Trash and
        # hidden twins. Every hit authorizes before choosing a row or restoring;
        # stored media IDs are not canonicalized/merged by this lookup.
        "SELECT id, media_type, deleted_at FROM items WHERE isbn = ? "
        "AND media_type IN ({','.join('?' for _ in types)}) "
        "ORDER BY deleted_at IS NOT NULL, (media_type = ?) DESC, id": 1,
        # Cover-recovered ISBN: non-disclosing physical collision probe before
        # the authorized target update, including actor-free queued jobs. Never
        # claim an identifier held by hidden or trashed catalogue history.
        "SELECT id FROM items WHERE isbn = ? AND id != ?": 1,
    },
    "app/routers/intake.py": {
        # Exact legacy/current ISBN identity, including hidden and Trash.
        # Both probes authorize EVERY hit with editor/include_deleted before
        # classifying a duplicate or restoring; batch preflight precedes writes.
        "SELECT id, deleted_at FROM items WHERE isbn = ? "
        "AND media_type IN ({','.join('?' for _ in types)})": 1,
        "SELECT id, deleted_at, media_type FROM items WHERE isbn = ? "
        "AND media_type IN ({','.join('?' for _ in types)})": 1,
    },
    "app/routers/item_barcode_edit.py": {
        # Legacy alias identity preflight under the write lock. All physical
        # book/kids_book twins, even Trash, must pass viewer access; ambiguity
        # and hidden rows refuse generically rather than being conflated.
        "SELECT id, media_type, deleted_at FROM items "
        "WHERE isbn = ? AND media_type IN ('book', 'kids_book') ORDER BY id": 1,
    },
    "app/routers/series.py": {
        # Shared-name effects include hidden AND trashed legacy/membership
        # parents. Authorize ALL under the lock and refuse any trashed member,
        # rather than silently committing a partial cross-library rename.
        "SELECT i.id, i.deleted_at FROM items i WHERE TRIM(i.series_name) = ? COLLATE NOCASE "
        "OR EXISTS (SELECT 1 FROM item_series s WHERE s.item_id = i.id "
        "AND s.series_name = ? COLLATE NOCASE) ORDER BY i.id": 1,
    },
    "app/routers/continue_home.py": {
        # Non-disclosing order census: hidden/Trash next members are a hard
        # STOP, not skipped to suggest later visible members. Only the actor's
        # private state is joined; liveness + per-member viewer ACL precede
        # rendering any candidate. PHYSICAL_SERIES_MEMBERSHIPS stays literal.
        "SELECT i.id, i.title, i.authors, i.media_type, i.cover_path, i.owned, "
        "i.series_name, i.series_position, i.publish_year, i.deleted_at, s.position, "
        "uis.reading_status, uis.progress_value, uis.progress_total, uis.progress_unit "
        "FROM {PHYSICAL_SERIES_MEMBERSHIPS} s JOIN items i ON i.id = s.item_id "
        "LEFT JOIN user_item_state uis ON uis.item_id = i.id AND uis.user_id = ? "
        "WHERE s.series_name = ? COLLATE NOCASE AND i.media_type IN ({placeholders}) "
        "ORDER BY s.position IS NULL, s.position, i.publish_year IS NULL, "
        "i.publish_year, i.title COLLATE NOCASE, i.id": 1,
    },
    "app/routers/related.py": {
        # Global-admin maintenance preflight must also detect unmapped live
        # parents. Every physical target requires editor authority before rebuild.
        "SELECT id FROM items WHERE deleted_at IS NULL ORDER BY id": 1,
        # Whole-component manual unlink effect census, then ALL returned
        # parents authorize before any edge removal. TWO reads (anchor +
        # traversal) in this ONE statement; a scoped traversal would miss a
        # hidden edge and falsely report a successful partial detach.
        "WITH RECURSIVE connected(id) AS ( "
        "SELECT id FROM items WHERE id = ? AND deleted_at IS NULL UNION "
        "SELECT i.id FROM item_links il JOIN connected c "
        "ON il.item_a_id = c.id OR il.item_b_id = c.id "
        "JOIN items i ON i.id = CASE WHEN il.item_a_id = c.id "
        "THEN il.item_b_id ELSE il.item_a_id END WHERE i.deleted_at IS NULL "
        ") SELECT CASE WHEN il.item_a_id = ? THEN il.item_b_id ELSE il.item_a_id END AS id "
        "FROM item_links il WHERE il.link_type = 'related' "
        "AND (il.item_a_id = ? OR il.item_b_id = ?) "
        "AND il.item_a_id IN (SELECT id FROM connected) "
        "AND il.item_b_id IN (SELECT id FROM connected) ORDER BY id": 2,
    },
    "app/routers/upcoming.py": {
        # Shared watch/dismissal effects across ALL live series parents. Used
        # for all-target editor preflight (and can_edit), never to display IDs
        # or hidden titles. A view would erase unauthorized effects.
        "SELECT id FROM items WHERE deleted_at IS NULL "
        "AND series_name = ? COLLATE NOCASE ORDER BY id": 1,
    },
    "app/routers/share.py": {
        # Explicit public capability, NOT the visiting actor's catalogue view.
        # Stored token fixes one library; every candidate mapping is checked
        # before the cap/output. Minimal public fields only, no personal data.
        "SELECT i.id, i.title, i.authors, i.cover_path, i.media_type, i.publish_year, "
        "i.series_name, i.series_position FROM items i "
        "WHERE i.deleted_at IS NULL AND ({_scope_where(scope)}) "
        "ORDER BY i.title COLLATE NOCASE, i.id": 1,
        # Capability creation is global-admin-only; the destination library
        # and each included parent authorize under the same write lock.
        "SELECT i.id FROM items i WHERE i.deleted_at IS NULL "
        "AND ({_scope_where(scope)}) ORDER BY i.id": 1,
    },
}

#: The item_copies twin of ALLOWLIST — same shape, same G88/G53 rules, a
#: separate dict because find_violations()/allowlist_mismatches() keep their
#: current meaning (items only, per this task's spec) and must not fold
#: item_copies reads in. Each entry names its current source spelling and
#: justified read count — by repository-relative path, never by basename
#: (G88). A service's entry must never excuse the same SQL in a router.
COPIES_ALLOWLIST: dict[str, dict[str, int]] = {
    "app/services/csv_import.py": {
        # Precise primary placement for an already authorized parent Trash row.
        # export_locations rechecks editor/include_deleted on this exact item;
        # deleted copies remain excluded. Normal live export uses copies_live.
        "SELECT item_id, location_id FROM item_copies WHERE item_id = ? "
        "AND is_primary = 1 AND deleted_at IS NULL": 1,
    },
    "app/services/copy_labels.py": {
        # next_number — a trashed copy still holds its copy_barcode UNIQUE
        # slot, so the next generated number must see it (G107's first
        # class: predicting the constraint).
        "SELECT copy_barcode FROM item_copies WHERE copy_barcode LIKE ?": 1,
    },
    "app/services/barcode_resolve.py": {
        # resolve()'s copy-barcode read — finds the row the view hides, not a
        # UNIQUE prediction: a trashed copy, or a copy of a trashed item, must
        # still resolve so the scan reports it. Same statement as its ALLOWLIST entry.
        # Full statement includes actor projection/scope and the NODE join;
        # accepting the old flat-copy join would conflate unrelated numeric IDs.
        "SELECT {projection}, l.name AS location_name, c.id AS copy_id, "
        "c.copy_number, c.location_id AS copy_location_id, "
        "cl.name AS copy_location_name, "
        "COALESCE(i.deleted_at, c.deleted_at) AS trash_stamp, "
        "(i.deleted_at IS NULL AND c.deleted_at IS NOT NULL) AS copy_trashed "
        "FROM item_copies c JOIN items i ON i.id = c.item_id "
        "LEFT JOIN locations l ON i.location_id = l.id "
        "LEFT JOIN location_nodes cl ON c.location_id = cl.id "
        "{state_join} WHERE c.copy_barcode = ? AND ({scope}) "
        "ORDER BY i.deleted_at IS NOT NULL, c.deleted_at IS NOT NULL, c.id": 1,
    },
    "app/database.py": {
        # The copies_live view CREATE in get_db() itself — the seam reads
        # the physical table by definition, same reasoning as the
        # items_live entry above.
        "SELECT c.* FROM item_copies c JOIN items i ON i.id = c.item_id": 1,
        # Immutable upstream26 source evidence only; bridge69 executes the
        # explicit NODE adaptation below, never the old flat-ID seed.
        "SELECT i.id, 1, i.location_id, 1 FROM items i "
        "WHERE i.owned = 1 AND i.location_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM item_copies c WHERE c.item_id = i.id)": 1,
        # Adapted one-time seed: eligible media, explicit legacy->node map,
        # physical-history NOT EXISTS (Trash blocks recreation), marker guard.
        "INSERT INTO item_copies (item_id, copy_number, location_id, is_primary) "
        "SELECT i.id, 1, n.id, 1 FROM items i "
        "LEFT JOIN location_nodes n ON n.legacy_location_id = i.location_id "
        "WHERE i.owned = 1 AND i.media_type IN "
        "('book','kids_book','vinyl','cassette','cd','music_other', "
        "'dvd','video_game','comic','manga','magazine') "
        "AND NOT EXISTS (SELECT 1 FROM item_copies c WHERE c.item_id = i.id) "
        "AND NOT EXISTS (SELECT 1 FROM settings "
        "WHERE key = 'holdings_foundation_backfill_v1' AND value = '1')": 1,
        # Read-only lineage admission: duplicate primaries (including Trash)
        # must refuse before DDL/index creation, not be silently repaired.
        "SELECT 1 FROM item_copies WHERE is_primary = 1 "
        "GROUP BY item_id HAVING COUNT(*) > 1 LIMIT 1": 1,
    },
    "app/services/item_copies.py": {
        # sync_primary_location: ALL copy history, including Trash, suppresses
        # initial creation. Parent editor authority is checked under the lock;
        # metadata/location edits may not manufacture a replacement copy.
        "SELECT 1 FROM item_copies WHERE item_id = ? LIMIT 1": 1,
        # add_copy — the numbering half only. A trashed copy still holds its
        # (item_id, copy_number) slot, so numbering must see what the UNIQUE
        # constraint sees. The "does this item have a copy" half is a
        # separate statement and reads the view.
        "SELECT COALESCE(MAX(copy_number), 0) AS n "
        "FROM item_copies WHERE item_id = ?": 1,
        # restore_copy — NOT the predict-a-UNIQUE-violation class the three
        # entries above are. This read exists to find the row `copies_live`
        # is deliberately hiding: a restore has to start from the trashed
        # copy, which by definition no view will return. Reading the view
        # here would make the function a silent no-op.
        # The physical parent is explicitly authorized editor/include_deleted
        # before restore, then checked live before any mutation.
        "SELECT * FROM item_copies "
        "WHERE id = ? AND deleted_at IS NOT NULL": 1,
        # purge_copy — the same class as restore_copy's read: a purge starts
        # from the trashed copy no view will return.
        # Physical parent must pass admin/include_deleted under the write lock.
        "SELECT item_id FROM item_copies WHERE id = ? AND deleted_at IS NOT NULL": 1,
    },
    "app/services/item_merge.py": {
        # Whole-merge physical eligibility preflight AFTER both live parents
        # authorize: Trash copy history cannot be moved to a digital keeper.
        "SELECT 1 FROM item_copies WHERE item_id = ? LIMIT 1": 1,
        # _reparent_copies — numbers the losing item's copies above the
        # keeper's own highest copy_number so nothing collides.
        "SELECT COALESCE(MAX(copy_number), 0) AS n FROM item_copies "
        "WHERE item_id = ?": 1,
        # _reparent_copies — the row-selection read. NOT the
        # predict-a-UNIQUE-violation class the entry above (and most of this
        # allowlist) is: this one exists so a trashed copy of the losing item
        # is not left invisibly parented to it and destroyed by the caller's
        # cascading `DELETE FROM items`. `copies_live` would hide exactly
        # that row, and a trashed copy is restorable, so the loss would be
        # silent and irreversible — see `reparent_children`'s docstring,
        # which already forbids losing anything restorable (G107).
        "SELECT id FROM item_copies WHERE item_id = ? "
        "ORDER BY copy_number, id": 1,
    },
    "app/routers/item_copies.py": {
        # _barcode_conflict — copy_barcode is UNIQUE collection-wide, so the
        # check includes hidden/Trash, but returns ONLY a boolean. No raw
        # item join or foreign title is justified by a constraint prediction.
        "SELECT 1 FROM item_copies WHERE copy_barcode = ? AND id IS NOT ? LIMIT 1": 1,
    },
    "app/services/trash.py": {
        # Exact retention statement, ONE copy read. See the item-side entry
        # for its two item reads and scoped-vs-admin-total policy.
        "SELECT (SELECT COUNT(*) FROM items i WHERE {item_sql} AND ({scope})) + "
        "(SELECT COUNT(*) FROM item_copies c JOIN items i "
        "ON i.id = c.item_id AND i.deleted_at IS NULL "
        "WHERE {copy_sql} AND ({scope})) AS n": 1,
        # Admin-only locked expired selection and editor-scoped restore-state
        # classification both need physical Trash, never a broad read bypass.
        "SELECT c.id FROM item_copies c JOIN items i ON i.id = c.item_id "
        "AND i.deleted_at IS NULL WHERE {copy_sql} AND ({scope}) ORDER BY c.id": 1,
        "SELECT c.deleted_at AS copy_deleted, i.deleted_at AS item_deleted "
        "FROM item_copies c JOIN items i ON i.id = c.item_id "
        "WHERE c.id = ? AND ({scope})": 1,
        # listing()'s copy half — same reasoning as the items ALLOWLIST
        # entry above (a copy group must see a trashed copy no view returns,
        # and its item's live/trashed state for the heading link); same full
        # span for the same reason.
        # The location is a NODE, never a flat location with a coincident ID.
        "SELECT c.*, i.title AS item_title, (i.deleted_at IS NULL) AS item_live, "
        "(i.deleted_at IS NULL AND ({copy_cond})) AS expired, l.name AS location_name "
        "FROM item_copies c JOIN items i ON i.id = c.item_id "
        "LEFT JOIN location_nodes l ON l.id = c.location_id "
        "WHERE c.deleted_at IS NOT NULL AND ({scope}) "
        "ORDER BY i.id, c.copy_number, c.id": 1,
    },
    "app/services/archive.py": {
        # Archive import — same copy_barcode UNIQUE conflict check as
        # _barcode_conflict above, on the import path instead of the API.
        "SELECT item_id FROM item_copies WHERE copy_barcode = ?": 1,
        # _copies_by_item — the portable archive carries Trash — design
        # plan-soft-delete-export. A trashed item's trashed copies must be
        # seen so they are exported too (they are already demoted, so no
        # further transform is needed).
        # _export_condition applies explicit library/lifecycle authorization;
        # the node-domain provenance is necessary for a faithful round trip.
        "SELECT c.*, n.name AS location FROM item_copies c "
        "JOIN items i ON i.id = c.item_id "
        "LEFT JOIN location_nodes n ON n.id = c.location_id "
        "WHERE ({condition}) ORDER BY c.item_id, c.copy_number, c.id": 1,
    },
    "app/routers/trash.py": {
        # Parent identity only; _item_authorized immediately applies canonical
        # actor + requested role/include_deleted before restore/purge/state.
        "SELECT item_id FROM item_copies WHERE id = ?": 1,
    },
    "app/services/audits.py": {
        # ALL submitted parents authorize editor/include_deleted under the lock
        # before any missing mark. Hidden/Trash existing copies must refuse
        # atomically, not vanish through the live view as unknown no-ops. Audit
        # history visibility separately intersects enduring library/actor ACLs.
        "SELECT item_id FROM item_copies WHERE id = ?": 1,
    },
    "app/services/locations.py": {
        # Moving a NODE affects all descendant copy parents, including Trash.
        # Every one is authorized before any alias/node/projection mutation.
        "WITH RECURSIVE nodes(id) AS (SELECT ? UNION SELECT n.id FROM location_nodes n "
        "JOIN nodes p ON n.parent_id = p.id) "
        "SELECT DISTINCT c.item_id FROM item_copies c JOIN nodes n ON c.location_id = n.id": 1,
        # Refuse deletion if ANY physical copy retains this NODE, even Trash;
        # boolean only, no cross-library identifying data is returned.
        "SELECT 1 FROM item_copies WHERE location_id = ?": 1,
        # Legacy alias detach: physical effect census + all-target authority,
        # with explicit descendant NODE IDs, never coalesced flat/copy IDs.
        "WITH RECURSIVE nodes(id) AS ( "
        "SELECT id FROM location_nodes WHERE legacy_location_id = ? UNION "
        "SELECT n.id FROM location_nodes n JOIN nodes p ON n.parent_id = p.id "
        ") SELECT id FROM items WHERE location_id = ? UNION "
        "SELECT c.item_id AS id FROM item_copies c JOIN nodes n ON n.id = c.location_id "
        "ORDER BY id": 1,
    },
}


def _normalise_file(path: Path) -> tuple[str, list[tuple[int, int]]]:
    """Return (joined_buffer, offsets) for one file.

    Comment-only lines are dropped first (G53). Each string literal's
    prefix is stripped together with its opening quote (see
    `_STRING_PREFIX_QUOTE`), then any remaining `"` characters are stripped,
    whitespace is collapsed to single spaces, and the surviving lines are
    joined with a single separating space — so a statement split across
    adjacent string literals or an f-string fragment that opens on `FROM`
    reads as one piece of text. `offsets` maps each surviving line's start
    position in the joined buffer to its 1-based source line number.
    """
    lines = path.read_text().splitlines()
    buf_parts: list[str] = []
    offsets: list[tuple[int, int]] = []
    pos = 0
    for i, raw_line in enumerate(lines, 1):
        if raw_line.lstrip().startswith("#"):
            continue
        unprefixed = _STRING_PREFIX_QUOTE.sub("", raw_line)
        collapsed = re.sub(r"\s+", " ", unprefixed.replace('"', "")).strip()
        if not collapsed:
            continue
        offsets.append((pos, i))
        buf_parts.append(collapsed)
        pos += len(collapsed) + 1  # +1 for the joining space
    return " ".join(buf_parts), offsets


def _line_for(offsets: list[tuple[int, int]], offset: int) -> int:
    line_no = offsets[0][1] if offsets else 1
    for start, ln in offsets:
        if start > offset:
            break
        line_no = ln
    return line_no


def _spanning(buf: str, sub: str, match: re.Match) -> bool:
    """True when some occurrence of `sub` in `buf` CONTAINS the whole match.

    This is the suppression rule, and it is deliberately not a proximity
    test. An earlier revision checked whether an allowlisted substring
    appeared anywhere in a ±200/300-character window around the match, which
    excused any new direct read that happened to land beside an allowlisted
    statement — two reads in the same short function, or a literal a few
    lines from the view's own CREATE, went unreported. Requiring the entry
    to span the match ties each exemption to the one statement it names.
    """
    idx = buf.find(sub)
    while idx != -1:
        if idx <= match.start() and match.end() <= idx + len(sub):
            return True
        idx = buf.find(sub, idx + 1)
    return False


def _find_violations(
    root: Path, pattern: re.Pattern, allowlist: dict[str, dict[str, int]], message: str
) -> list[str]:
    """Shared scan behind both find_violations() and find_copy_violations().

    One `_spanning` call, one normaliser (`_normalise_file`) — both
    relations scan the same normalised text and suppress the same way, they
    differ only in which pattern and which allowlist they carry (G88).
    """
    violations = []
    for path in sorted((root / "app").rglob("*.py")):
        rel = str(path.relative_to(root))
        buf, offsets = _normalise_file(path)
        allowed = allowlist.get(rel, {})
        for match in pattern.finditer(buf):
            if any(_spanning(buf, sub, match) for sub in allowed):
                continue
            line = _line_for(offsets, match.start())
            violations.append(message.format(path=rel, line=line))
    return violations


def find_violations(root: Path = ROOT) -> list[str]:
    return _find_violations(root, _ITEMS_READ, ALLOWLIST, VIOLATION_MSG)


def find_copy_violations(root: Path = ROOT) -> list[str]:
    """The item_copies twin of find_violations(). Deliberately not folded
    into it. Gates main() from this task onward — see the module docstring
    and main() below."""
    return _find_violations(root, _COPIES_READ, COPIES_ALLOWLIST, COPIES_VIOLATION_MSG)


def _allowlist_mismatches(
    root: Path, pattern: re.Pattern, allowlist: dict[str, dict[str, int]]
) -> list[str]:
    """Shared scan behind both allowlist_mismatches() and
    copy_allowlist_mismatches().

    Every allowlist entry must span exactly the number of reads declared
    beside it. A count of 0 is the stale case — an entry the code no longer
    produces, sitting there silently over-permissive (mirrors
    test_raw_update_allowlist_has_no_stale_entries in
    tests/test_item_write.py). A count ABOVE the declared one is the
    ride-along case: a new direct read written inside text an existing entry
    already covers, which would otherwise be exempted without anyone
    deciding it should be. Both are mismatches and both must fail.
    """
    buf_by_path: dict[str, str] = {}
    for path in sorted((root / "app").rglob("*.py")):
        rel = str(path.relative_to(root))
        buf_by_path[rel] = _normalise_file(path)[0]

    mismatches = []
    for rel, expected in allowlist.items():
        buf = buf_by_path.get(rel, "")
        matches = list(pattern.finditer(buf))
        for sub, want in expected.items():
            got = sum(1 for m in matches if _spanning(buf, sub, m))
            if got != want:
                mismatches.append(
                    f"{rel}: {sub!r} spans {got} read(s), expected {want}"
                )
    return mismatches


def allowlist_mismatches(root: Path = ROOT) -> list[str]:
    return _allowlist_mismatches(root, _ITEMS_READ, ALLOWLIST)


def copy_allowlist_mismatches(root: Path = ROOT) -> list[str]:
    """The item_copies twin of allowlist_mismatches(). Gates main() from
    this task onward, same as find_copy_violations() above — the copies
    allowlist must stay accurate and the copies census must stay at 0."""
    return _allowlist_mismatches(root, _COPIES_READ, COPIES_ALLOWLIST)


def main() -> int:
    violations = find_violations()
    mismatches = allowlist_mismatches()
    copy_violations = find_copy_violations()
    copy_mismatches = copy_allowlist_mismatches()

    if violations:
        print(f"items_live lint: {len(violations)} violation(s)\n")
        for v in violations:
            print(f"  {v}")
    if mismatches:
        print(f"items_live lint: {len(mismatches)} allowlist mismatch(es)\n")
        for m in mismatches:
            print(f"  {m}")
    if copy_violations:
        print(
            f"copies_live lint: {len(copy_violations)} violation(s) — reads the "
            "item_copies table directly instead of copies_live (the "
            "exact constraint, authorization, migration and lifecycle spans "
            "in COPIES_ALLOWLIST are the only reads excused)\n"
        )
        for v in copy_violations:
            print(f"  {v}")
    if copy_mismatches:
        print(f"copies_live lint: {len(copy_mismatches)} allowlist mismatch(es)\n")
        for m in copy_mismatches:
            print(f"  {m}")

    if violations or mismatches or copy_violations or copy_mismatches:
        return 1
    print("items_live lint: every read goes through the view.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
