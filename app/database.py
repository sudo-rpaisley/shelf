import logging
import hashlib
import re
import sqlite3
from contextlib import contextmanager
from typing import Sequence

from app.config import DATABASE_PATH, COVERS_DIR

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    subtitle        TEXT,
    authors         TEXT,
    isbn            TEXT,
    isbn10          TEXT,
    media_type      TEXT NOT NULL DEFAULT 'book',
    cover_path      TEXT,
    publisher       TEXT,
    publish_year    INTEGER,
    page_count      INTEGER,
    description     TEXT,
    series_name     TEXT,
    series_position REAL,
    narrator        TEXT,
    duration_mins   INTEGER,
    location_id     INTEGER REFERENCES locations(id),
    abs_id          TEXT,
    source          TEXT NOT NULL DEFAULT 'manual',
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(isbn, media_type)
);

CREATE INDEX IF NOT EXISTS idx_items_isbn ON items(isbn);
CREATE INDEX IF NOT EXISTS idx_items_media_type ON items(media_type);
CREATE INDEX IF NOT EXISTS idx_items_title ON items(title COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_items_location ON items(location_id);
CREATE INDEX IF NOT EXISTS idx_items_abs_id ON items(abs_id);

CREATE TABLE IF NOT EXISTS locations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    sort_order INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS scan_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    isbn       TEXT,
    media_type TEXT,
    result     TEXT NOT NULL,
    item_id    INTEGER REFERENCES items(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_items_authors ON items(authors COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_items_publish_year ON items(publish_year);
CREATE INDEX IF NOT EXISTS idx_items_series ON items(series_name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


# Versioned migrations: (version, description, sql)
# Append new migrations to the end. Never modify or reorder existing entries.
_FORK_CORE_MIGRATIONS: Sequence[tuple[int, str, str]] = (
    (1,  "Add reading_status column",         "ALTER TABLE items ADD COLUMN reading_status TEXT DEFAULT NULL"),
    (2,  "Add date_started column",           "ALTER TABLE items ADD COLUMN date_started TEXT DEFAULT NULL"),
    (3,  "Add date_finished column",          "ALTER TABLE items ADD COLUMN date_finished TEXT DEFAULT NULL"),
    (4,  "Add estimated_value column",        "ALTER TABLE items ADD COLUMN estimated_value REAL DEFAULT NULL"),
    (5,  "Add value_updated_at column",       "ALTER TABLE items ADD COLUMN value_updated_at TEXT DEFAULT NULL"),
    (6,  "Add upc column",                    "ALTER TABLE items ADD COLUMN upc TEXT DEFAULT NULL"),
    (7,  "Add hardcover_book_id column",      "ALTER TABLE items ADD COLUMN hardcover_book_id INTEGER DEFAULT NULL"),
    (8,  "Add hardcover_edition_id column",   "ALTER TABLE items ADD COLUMN hardcover_edition_id INTEGER DEFAULT NULL"),
    (9,  "Add hardcover_user_book_id column", "ALTER TABLE items ADD COLUMN hardcover_user_book_id INTEGER DEFAULT NULL"),
    (10, "Add owned column",                  "ALTER TABLE items ADD COLUMN owned INTEGER NOT NULL DEFAULT 1"),
    (11, "Add platform column",               "ALTER TABLE items ADD COLUMN platform TEXT DEFAULT NULL"),
    (12, "Add scan_log mode column",          "ALTER TABLE scan_log ADD COLUMN mode TEXT DEFAULT 'add'"),
    (13, "Add users token_version column",    "ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 1"),
    (14, "Add abs_library_id column",         "ALTER TABLE items ADD COLUMN abs_library_id TEXT DEFAULT NULL"),
    (15, "Add manual_value column",           "ALTER TABLE items ADD COLUMN manual_value REAL DEFAULT NULL"),
    (16, "Add series_meta complete column",   "ALTER TABLE series_meta ADD COLUMN complete INTEGER DEFAULT NULL"),
    (17, "Add series_meta hc_total column",   "ALTER TABLE series_meta ADD COLUMN hc_total INTEGER DEFAULT NULL"),
    (18, "Add series_meta hc_missing column", "ALTER TABLE series_meta ADD COLUMN hc_missing INTEGER DEFAULT NULL"),
    (19, "Add series_meta hc_checked_at column", "ALTER TABLE series_meta ADD COLUMN hc_checked_at TEXT DEFAULT NULL"),
    # 20-21 re-file barcodes landed in the wrong column before #20 was fixed.
    # Both are plain UPDATEs rather than schema changes, and both are written
    # to be idempotent + collision-safe: _backfill_versions() replays every
    # migration on a pre-version-tracking database and only swallows
    # OperationalError, so an IntegrityError here would abort startup.
    (20, "Canonicalize UPC codes to EAN-13",
     """UPDATE items SET upc = '0' || upc
        WHERE upc IS NOT NULL AND length(upc) = 12
          AND NOT EXISTS (SELECT 1 FROM items o
                          WHERE o.upc = '0' || items.upc
                            AND o.media_type = items.media_type)"""),
    (21, "Re-file UPC barcodes stored in the isbn column",
     """UPDATE items SET upc = isbn, isbn = NULL, isbn10 = NULL
        WHERE upc IS NULL AND isbn IS NOT NULL AND length(isbn) = 13
          AND isbn NOT LIKE '978%' AND isbn NOT LIKE '979%'
          AND NOT EXISTS (SELECT 1 FROM items o
                          WHERE o.upc = items.isbn
                            AND o.media_type = items.media_type)"""),
    (22, "Add language column", "ALTER TABLE items ADD COLUMN language TEXT DEFAULT NULL"),
    (23, "Backfill language from ISBN registration group",
     """UPDATE items SET language = CASE
            WHEN substr(isbn, 1, 5) = '97910' THEN 'fr'
            WHEN substr(isbn, 1, 5) = '97884' THEN 'es'
            WHEN substr(isbn, 1, 5) = '97885' THEN 'pt'
            WHEN substr(isbn, 1, 5) = '97887' THEN 'da'
            WHEN substr(isbn, 1, 5) = '97888' THEN 'it'
            WHEN substr(isbn, 1, 5) = '97912' THEN 'it'
            WHEN substr(isbn, 1, 5) = '97890' THEN 'nl'
            WHEN substr(isbn, 1, 5) = '97891' THEN 'sv'
            WHEN substr(isbn, 1, 5) = '97911' THEN 'ko'
            WHEN substr(isbn, 1, 4) = '9780' THEN 'en'
            WHEN substr(isbn, 1, 4) = '9781' THEN 'en'
            WHEN substr(isbn, 1, 4) = '9798' THEN 'en'
            WHEN substr(isbn, 1, 4) = '9782' THEN 'fr'
            WHEN substr(isbn, 1, 4) = '9783' THEN 'de'
            WHEN substr(isbn, 1, 4) = '9784' THEN 'ja'
            WHEN substr(isbn, 1, 4) = '9785' THEN 'ru'
            WHEN substr(isbn, 1, 4) = '9787' THEN 'zh'
            ELSE NULL
        END
        WHERE language IS NULL AND isbn IS NOT NULL"""),
    (24, "Add komga_id column", "ALTER TABLE items ADD COLUMN komga_id TEXT DEFAULT NULL"),
    (25, "Add komga_library_id column", "ALTER TABLE items ADD COLUMN komga_library_id TEXT DEFAULT NULL"),
    (26, "Add komga_series_id column", "ALTER TABLE items ADD COLUMN komga_series_id TEXT DEFAULT NULL"),
    (27, "Index Komga item IDs", "CREATE INDEX IF NOT EXISTS idx_items_komga_id ON items(komga_id)"),
    (28, "Add romm_id column", "ALTER TABLE items ADD COLUMN romm_id TEXT DEFAULT NULL"),
    (29, "Add romm_platform_id column", "ALTER TABLE items ADD COLUMN romm_platform_id TEXT DEFAULT NULL"),
    (30, "Index RomM item IDs", "CREATE INDEX IF NOT EXISTS idx_items_romm_id ON items(romm_id)"),
    (31, "Add Discogs master ID", "ALTER TABLE music_releases ADD COLUMN discogs_master_id INTEGER DEFAULT NULL"),
    (32, "Add Discogs label", "ALTER TABLE music_releases ADD COLUMN discogs_label TEXT DEFAULT NULL"),
    (33, "Add Discogs catalogue number", "ALTER TABLE music_releases ADD COLUMN discogs_catalog_number TEXT DEFAULT NULL"),
    (34, "Add Discogs format summary", "ALTER TABLE music_releases ADD COLUMN discogs_format_summary TEXT DEFAULT NULL"),
    (35, "Add Discogs genres", "ALTER TABLE music_releases ADD COLUMN discogs_genres TEXT DEFAULT NULL"),
    (36, "Add Discogs styles", "ALTER TABLE music_releases ADD COLUMN discogs_styles TEXT DEFAULT NULL"),
    (37, "Add Discogs notes", "ALTER TABLE music_releases ADD COLUMN discogs_notes TEXT DEFAULT NULL"),
    (38, "Add Discogs cache timestamp", "ALTER TABLE music_releases ADD COLUMN discogs_updated_at TEXT DEFAULT NULL"),
    (39, "Add music identifier source", "ALTER TABLE music_identifiers ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'"),
    (40, "Add item-series membership table",
     """CREATE TABLE IF NOT EXISTS item_series (
        item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        series_name TEXT NOT NULL COLLATE NOCASE,
        position REAL,
        is_primary INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (item_id, series_name)
    )"""),
    (41, "Index item-series names",
     "CREATE INDEX IF NOT EXISTS idx_item_series_name ON item_series(series_name COLLATE NOCASE)"),
    (42, "Backfill primary item-series memberships",
     """INSERT INTO item_series (item_id, series_name, position, is_primary)
        SELECT id, TRIM(series_name), series_position, 1 FROM items
        WHERE series_name IS NOT NULL AND TRIM(series_name) != ''
        ON CONFLICT(item_id, series_name) DO UPDATE SET
            position = excluded.position, is_primary = 1"""),
    (43, "Add collections table",
     """CREATE TABLE IF NOT EXISTS collections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        description TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )"""),
    (44, "Add collection item membership table",
     """CREATE TABLE IF NOT EXISTS collection_items (
        collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
        item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (collection_id, item_id)
    )"""),
    (45, "Index collection item memberships",
     "CREATE INDEX IF NOT EXISTS idx_collection_items_item ON collection_items(item_id)"),
    (46, "Add confirmed legacy book barcode mappings",
     """CREATE TABLE IF NOT EXISTS legacy_book_mappings (
            barcode      TEXT PRIMARY KEY,
            isbn13       TEXT NOT NULL,
            confirmed_at TEXT NOT NULL DEFAULT (datetime('now')),
            CHECK(length(barcode) = 17 AND barcode NOT GLOB '*[^0-9]*'),
            CHECK(length(isbn13) = 13
                  AND isbn13 NOT GLOB '*[^0-9]*'
                  AND substr(isbn13, 1, 3) IN ('978', '979'))
        )"""),
)

# Immutable upstream operation identities, NOT a second runnable registry.
# Their numbers collide with published fork operations. Adaptations below use
# new identities and record both original and effective SQL fingerprints.
_UPSTREAM_MIGRATIONS = (
    (24, "Add confirmed legacy book barcode mappings",
     """CREATE TABLE IF NOT EXISTS legacy_book_mappings (
            barcode      TEXT PRIMARY KEY,
            isbn13       TEXT NOT NULL,
            confirmed_at TEXT NOT NULL DEFAULT (datetime('now')),
            CHECK(length(barcode) = 17 AND barcode NOT GLOB '*[^0-9]*'),
            CHECK(length(isbn13) = 13
                  AND isbn13 NOT GLOB '*[^0-9]*'
                  AND substr(isbn13, 1, 3) IN ('978', '979'))
        )"""),
    (25, "Add first-class physical copies",
     """CREATE TABLE IF NOT EXISTS item_copies (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id            INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            copy_number        INTEGER NOT NULL,
            location_id        INTEGER REFERENCES locations(id) ON DELETE SET NULL,
            condition          TEXT,
            notes              TEXT,
            acquired_date      TEXT,
            acquisition_source TEXT,
            acquisition_price  REAL CHECK(acquisition_price IS NULL OR acquisition_price >= 0),
            provenance         TEXT,
            copy_barcode       TEXT UNIQUE,
            is_primary         INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0, 1)),
            created_at         TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at         TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(item_id, copy_number)
        )"""),
    (26, "Backfill primary copies from legacy locations",
     """INSERT INTO item_copies (item_id, copy_number, location_id, is_primary)
        SELECT i.id, 1, i.location_id, 1 FROM items i
        WHERE i.owned = 1 AND i.location_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM item_copies c WHERE c.item_id = i.id)"""),
    (27, "Add parent relationship to locations",
     "ALTER TABLE locations ADD COLUMN parent_id INTEGER REFERENCES locations(id) ON DELETE RESTRICT"),
    (28, "Add node label to locations",
     "ALTER TABLE locations ADD COLUMN label TEXT DEFAULT NULL"),
    (29, "Backfill flat locations as hierarchy roots",
     "UPDATE locations SET label = name WHERE label IS NULL"),
    (30, "Index hierarchical location children",
     "CREATE INDEX IF NOT EXISTS idx_locations_parent ON locations(parent_id, sort_order)"),
    (31, "Add physical copy shelf position",
     "ALTER TABLE item_copies ADD COLUMN position_order INTEGER DEFAULT NULL"),
    (32, "Add durable cover-review dismissal",
     "ALTER TABLE items ADD COLUMN cover_review_dismissed INTEGER NOT NULL DEFAULT 0"),
    # 33 and 34 are numbered entries *as well as* MIGRATION_TABLES copies, so
    # that 35 and 36 find their tables on the upgrade path. The loop above runs
    # before executescript(MIGRATION_TABLES), and _is_benign_migration_error
    # answers "benign" for `no such table` whenever the table is named in
    # MIGRATION_TABLES — so a CREATE that lived only there would let the seeds
    # be recorded as applied without ever running. The next person will reach
    # for MIGRATION_TABLES alone; this is why that is not enough.
    (33, "Add named lists",
     """CREATE TABLE IF NOT EXISTS lists (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    slug       TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)"""),
    (34, "Add list membership",
     """CREATE TABLE IF NOT EXISTS list_items (
    list_id  INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    item_id  INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (list_id, item_id)
)"""),
    (35, "Seed the wishlist",
     "INSERT OR IGNORE INTO lists (slug, name) VALUES ('wishlist', 'Wishlist')"),
    (36, "Put every unowned item on the wishlist",
     """INSERT OR IGNORE INTO list_items (list_id, item_id)
        SELECT (SELECT id FROM lists WHERE slug = 'wishlist'), id
        FROM items WHERE owned = 0"""),
    # 37 and 38 are the soft-delete seam. Nothing sets either column yet:
    # deletes still hard-delete, and the Trash plan is what starts writing a
    # timestamp here. The column exists now so the read side (the items_live
    # view and its lint) can land on its own, against an unchanged suite.
    #
    # Both go in MIGRATIONS *only* — deliberately not in SCHEMA's CREATE TABLE
    # items. A fresh install runs SCHEMA and then, because schema_version is
    # empty, _backfill_versions executes every migration's SQL; a SCHEMA copy
    # of the column would make 37 raise "duplicate column name", which
    # _is_benign_migration_error forgives only for versions <= 21, so startup
    # would abort. test_cover_review_dismissed_defaults_to_zero_on_a_fresh_bootstrap
    # pins exactly this for migration 32, and none of the columns added to
    # items since v1 appears in SCHEMA's CREATE. item_copies is the same story
    # by the precedent of 31's position_order, which is likewise absent from
    # the MIGRATION_TABLES copy of the table.
    (37, "Add soft-delete timestamp to items",
     "ALTER TABLE items ADD COLUMN deleted_at TEXT DEFAULT NULL"),
    (38, "Add soft-delete timestamp to item copies",
     "ALTER TABLE item_copies ADD COLUMN deleted_at TEXT DEFAULT NULL"),
    # 39 goes in MIGRATION_TABLES *as well*, which is the opposite of what 37
    # and 38 just above deliberately do — and the difference is only where the
    # table is created, not a change of policy.
    #
    # `items` and `item_copies` exist before the loop runs (SCHEMA creates the
    # first, and the second is reached the same way on any database old enough
    # to have one), so on a fresh install their ALTERs actually *execute*, and
    # a copy of the column in the CREATE would make them raise "duplicate
    # column name" — which _is_benign_migration_error forgives only for
    # versions <= 21, so startup would abort.
    #
    # `tags` is created by executescript(MIGRATION_TABLES), which runs *after*
    # this loop. So on a fresh database this ALTER answers "no such table:
    # tags" and is skipped — benign, because _is_benign_migration_error checks
    # that MIGRATION_TABLES names the table. The fresh path therefore never
    # applies the column, and the CREATE below is the only thing that can give
    # it to a new install. Both places, or one of the two bootstrap routes ends
    # up without the column (G1).
    (39, "Add tags media_type scope column",
     "ALTER TABLE tags ADD COLUMN media_type TEXT DEFAULT NULL"),
    # 40 is a new table, so it follows 33/34 rather than 37/38: the CREATE is a
    # numbered entry *and* lives in MIGRATION_TABLES. The numbered copy runs
    # first on an upgrade; the MIGRATION_TABLES copy runs on every boot and is
    # the only one a fresh install ever executes. A CREATE that lived in one
    # place only would leave one of the two bootstrap routes without the table
    # (G1; test_schema_parity pins the pair). The index is in MIGRATION_TABLES
    # alone, since a numbered entry is a single statement.
    (40, "Add price history",
     "CREATE TABLE IF NOT EXISTS price_history ("
     " id INTEGER PRIMARY KEY AUTOINCREMENT,"
     " item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,"
     " price REAL,"
     " source TEXT NOT NULL DEFAULT 'isbndb',"
     " observed_at TEXT NOT NULL DEFAULT (datetime('now')))"),
    # A feature with no flag row reads as on, and price_alerts is new in the
    # same release, so an upgrade would switch it on for every install — even
    # one that applied Standard or Minimal. Start it the way Valuation stands:
    # off where Valuation is off (it spends the same ISBNdb key), untouched
    # elsewhere. INSERT OR IGNORE keeps a choice already made. A fresh
    # database has no feature rows, so this is a no-op there.
    (41, "Start price alerts off where Valuation is off",
     """INSERT OR IGNORE INTO settings (key, value)
        SELECT 'feature.price_alerts', '0'
        WHERE EXISTS (SELECT 1 FROM settings
                      WHERE key = 'feature.valuation' AND value = '0')"""),
    (42, "Add cover edition ISBN", "ALTER TABLE items ADD COLUMN cover_edition_isbn TEXT"),
    (43, "Add cover edition language", "ALTER TABLE items ADD COLUMN cover_edition_language TEXT"),
    # A feature with no flag row reads as on, and collector is new in the same
    # release, so an upgrade would switch it on for every install — even one
    # that applied Standard or Minimal. Start it off where Valuation is off,
    # untouched elsewhere (same shape as 41). INSERT OR IGNORE keeps a choice
    # already made. A fresh database has no feature rows, so this is a no-op.
    (44, "Start collector tools off where Valuation is off",
     """INSERT OR IGNORE INTO settings (key, value)
        SELECT 'feature.collector', '0'
        WHERE EXISTS (SELECT 1 FROM settings
                      WHERE key = 'feature.valuation' AND value = '0')"""),
    # 45 and 46 are new tables, so like 40 each is a numbered entry *and* a
    # MIGRATION_TABLES copy (G1). A NULL foreign key never carries domain
    # meaning here: ON DELETE SET NULL would turn an audit of a deleted
    # location into a whole-library one, and a purged copy's entry into an
    # item-level one. So scope and location_name (45) and copy_specific (46)
    # are stored explicitly. The 46 index lives in MIGRATION_TABLES alone.
    (45, "Add audit sessions",
     "CREATE TABLE IF NOT EXISTS audit_sessions ("
     " id INTEGER PRIMARY KEY AUTOINCREMENT,"
     " location_id INTEGER REFERENCES locations(id) ON DELETE SET NULL,"
     " started_at TEXT NOT NULL DEFAULT (datetime('now')),"
     " finished_at TEXT,"
     " notes TEXT,"
     " scope TEXT NOT NULL CHECK (scope IN ('location', 'library')),"
     " location_name TEXT NOT NULL)"),
    (46, "Add audit entries",
     "CREATE TABLE IF NOT EXISTS audit_entries ("
     " id INTEGER PRIMARY KEY AUTOINCREMENT,"
     " session_id INTEGER NOT NULL REFERENCES audit_sessions(id) ON DELETE CASCADE,"
     " item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,"
     " copy_id INTEGER REFERENCES item_copies(id) ON DELETE SET NULL,"
     " location_id INTEGER REFERENCES locations(id) ON DELETE SET NULL,"
     " raw_code TEXT NOT NULL,"
     " result TEXT NOT NULL,"
     " copy_specific INTEGER NOT NULL DEFAULT 0 CHECK (copy_specific IN (0, 1)),"
     " scanned_at TEXT NOT NULL DEFAULT (datetime('now')))"),
    # MIGRATIONS-only, never in the item_copies CREATE — the deliberate
    # exception to G1's headline rule, for the reason given at 37/38 above
    # (and 31's position_order): a CREATE copy makes this ALTER raise
    # "duplicate column name" on a fresh install, which
    # _is_benign_migration_error forgives only for versions <= 21, so startup
    # would abort. test_schema_parity accepts a MIGRATIONS-only column.
    (47, "Add missing-since mark to item copies",
     "ALTER TABLE item_copies ADD COLUMN missing_since TEXT DEFAULT NULL"),
    # A new table, so a numbered entry *and* a MIGRATION_TABLES copy (G1).
    # One plain single-statement string, like 45/46 (G16).
    (48, "Add duplicate dismissals",
     "CREATE TABLE IF NOT EXISTS duplicate_dismissals ("
     " item_a_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,"
     " item_b_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,"
     " created_at TEXT NOT NULL DEFAULT (datetime('now')),"
     " PRIMARY KEY (item_a_id, item_b_id),"
     " CHECK (item_a_id < item_b_id))"),
    # Two new tables, so a numbered entry *and* a MIGRATION_TABLES copy each
    # (G1). One plain single-statement string per entry, like 45/46 (G16).
    # No logging here (G3).
    (49, "Add release watches",
     "CREATE TABLE IF NOT EXISTS release_watches ("
     " id INTEGER PRIMARY KEY AUTOINCREMENT,"
     " series_name TEXT NOT NULL UNIQUE COLLATE NOCASE,"
     " checked_at TEXT,"
     " check_state TEXT,"
     " created_at TEXT NOT NULL DEFAULT (datetime('now')))"),
    (50, "Add upcoming releases",
     "CREATE TABLE IF NOT EXISTS upcoming_releases ("
     " id INTEGER PRIMARY KEY AUTOINCREMENT,"
     " watch_id INTEGER NOT NULL REFERENCES release_watches(id) ON DELETE CASCADE,"
     " hc_book_id INTEGER NOT NULL,"
     " title TEXT NOT NULL,"
     " authors TEXT,"
     " cover_url TEXT,"
     " series_position REAL,"
     " release_date TEXT,"
     " notified_at TEXT,"
     " reminded_at TEXT,"
     " dismissed INTEGER NOT NULL DEFAULT 0,"
     " created_at TEXT NOT NULL DEFAULT (datetime('now')),"
     " UNIQUE (watch_id, hc_book_id))"),
    # A feature with no flag row reads as on, and release_radar is new in the
    # same release, so an upgrade would switch it on for every install — even
    # one that turned Hardcover off, where the daily pass would spend the
    # token. Start it off where Hardcover is off (its profile matches
    # Hardcover's), untouched elsewhere. INSERT OR IGNORE keeps a choice
    # already made. A fresh database has no feature rows, so this is a no-op
    # there.
    (51, "Start release radar off where Hardcover is off",
     """INSERT OR IGNORE INTO settings (key, value)
        SELECT 'feature.release_radar', '0'
        WHERE EXISTS (SELECT 1 FROM settings
                      WHERE key = 'feature.hardcover' AND value = '0')"""),
    (52, "Add API tokens",
     "CREATE TABLE IF NOT EXISTS api_tokens ("
     " id INTEGER PRIMARY KEY AUTOINCREMENT,"
     " user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,"
     " name TEXT NOT NULL,"
     " token_hash TEXT NOT NULL UNIQUE,"
     " prefix TEXT NOT NULL,"
     " last_used_at TEXT,"
     " created_at TEXT NOT NULL DEFAULT (datetime('now')))"),
    # A feature with no flag row reads as on, so an upgrade would switch the
    # new, Everything-only `api` feature on for every install (G135). Start it
    # off wherever an install exists, keyed on `users` having a row. INSERT OR
    # IGNORE keeps a choice already made. On a fresh install `users` does not
    # exist yet during `_backfill_versions` (MIGRATION_TABLES creates it
    # afterwards), so the statement raises `no such table: users`; that is
    # benign via `_is_benign_migration_error` because `users` is a
    # MIGRATION_TABLES table, and the setup wizard's apply_profile writes the
    # row.
    (53, "Start API tokens off on upgrade",
     """INSERT OR IGNORE INTO settings (key, value)
        SELECT 'feature.api', '0'
        WHERE EXISTS (SELECT 1 FROM users)"""),
    # The UI language this user picked; NULL follows the browser (app/i18n.py).
    (54, "Add users language", "ALTER TABLE users ADD COLUMN language TEXT"),
    # A feature with no flag row reads as on, and buy_links is new in the same
    # release, so an upgrade would switch it on for every install — even one
    # that applied Standard or Minimal (G135). Start it off where Valuation is
    # off, untouched elsewhere (same shape as 41). INSERT OR IGNORE keeps a
    # choice already made. A fresh database has no feature rows, so this is a
    # no-op there.
    (55, "Start buy links off where Valuation is off",
     """INSERT OR IGNORE INTO settings (key, value)
        SELECT 'feature.buy_links', '0'
        WHERE EXISTS (SELECT 1 FROM settings
                      WHERE key = 'feature.valuation' AND value = '0')"""),
    # Migration-only, like the 37/38 note: the column is NOT in SCHEMA. Not
    # unique on purpose (G71): Audiobookshelf can hold one ASIN in two
    # libraries. An edition identifier like isbn/upc.
    (56, "Add items ASIN", "ALTER TABLE items ADD COLUMN asin TEXT"),
    (57, "Index items ASIN", "CREATE INDEX IF NOT EXISTS idx_items_asin ON items(asin)"),
)

MIGRATION_TABLES = """
CREATE TABLE IF NOT EXISTS reading_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id       INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    status        TEXT NOT NULL,
    date_started  TEXT,
    date_finished TEXT,
    notes         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_reading_log_item ON reading_log(item_id);
CREATE INDEX IF NOT EXISTS idx_items_reading_status ON items(reading_status);

CREATE TABLE IF NOT EXISTS share_links (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    token      TEXT NOT NULL UNIQUE,
    scope      TEXT NOT NULL DEFAULT 'wishlist',
    label      TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS valuation_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    total_value  REAL NOT NULL,
    priced_count INTEGER NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    price       REAL,
    source      TEXT NOT NULL DEFAULT 'isbndb',
    observed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_price_history_item ON price_history(item_id, observed_at);

CREATE TABLE IF NOT EXISTS borrowers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS checkouts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id       INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    borrower_id   INTEGER NOT NULL REFERENCES borrowers(id),
    checked_out   TEXT NOT NULL DEFAULT (datetime('now')),
    due_date      TEXT,
    checked_in    TEXT,
    notes         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_checkouts_item ON checkouts(item_id);
CREATE INDEX IF NOT EXISTS idx_checkouts_borrower ON checkouts(borrower_id);

CREATE TABLE IF NOT EXISTS item_copies (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id            INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    copy_number        INTEGER NOT NULL DEFAULT 1,
    location_id        INTEGER REFERENCES location_nodes(id) ON DELETE SET NULL,
    position_order     INTEGER,
    condition          TEXT,
    notes              TEXT,
    acquired_date      TEXT,
    acquisition_price  REAL,
    copy_barcode       TEXT UNIQUE,
    is_primary         INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0, 1)),
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(item_id, copy_number)
);
CREATE INDEX IF NOT EXISTS idx_item_copies_item ON item_copies(item_id);
CREATE INDEX IF NOT EXISTS idx_item_copies_location ON item_copies(location_id, position_order, id);

CREATE TABLE IF NOT EXISTS audit_sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id   INTEGER REFERENCES location_nodes(id) ON DELETE SET NULL,
    started_at    TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at   TEXT,
    notes         TEXT,
    scope         TEXT NOT NULL CHECK (scope IN ('location', 'library')),
    location_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER NOT NULL REFERENCES audit_sessions(id) ON DELETE CASCADE,
    item_id       INTEGER REFERENCES items(id) ON DELETE SET NULL,
    copy_id       INTEGER REFERENCES item_copies(id) ON DELETE SET NULL,
    location_id   INTEGER REFERENCES location_nodes(id) ON DELETE SET NULL,
    raw_code      TEXT NOT NULL,
    result        TEXT NOT NULL,
    copy_specific INTEGER NOT NULL DEFAULT 0 CHECK (copy_specific IN (0, 1)),
    security_library_id INTEGER,
    security_user_id INTEGER,
    scanned_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_entries_session ON audit_entries(session_id, item_id);

CREATE TABLE IF NOT EXISTS duplicate_dismissals (
    item_a_id  INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    item_b_id  INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (item_a_id, item_b_id),
    CHECK (item_a_id < item_b_id)
);

CREATE INDEX IF NOT EXISTS idx_items_upc ON items(upc);
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_upc_type ON items(upc, media_type) WHERE upc IS NOT NULL;

CREATE TABLE IF NOT EXISTS item_links (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    item_a_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    item_b_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    link_type TEXT NOT NULL DEFAULT 'format',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(item_a_id, item_b_id)
);
CREATE INDEX IF NOT EXISTS idx_item_links_a ON item_links(item_a_id);
CREATE INDEX IF NOT EXISTS idx_item_links_b ON item_links(item_b_id);

CREATE INDEX IF NOT EXISTS idx_items_hardcover_book ON items(hardcover_book_id);
CREATE INDEX IF NOT EXISTS idx_items_platform ON items(platform);

CREATE TABLE IF NOT EXISTS log_entries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  TEXT NOT NULL DEFAULT (datetime('now')),
    level      TEXT NOT NULL,
    module     TEXT,
    message    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_log_entries_timestamp ON log_entries(timestamp);
CREATE INDEX IF NOT EXISTS idx_log_entries_level ON log_entries(level);

CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    username       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password       TEXT NOT NULL,
    display_name   TEXT,
    role           TEXT NOT NULL DEFAULT 'viewer' CHECK(role IN ('admin','editor','viewer')),
    token_version  INTEGER NOT NULL DEFAULT 1,
    language       TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS game_platforms (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    slug       TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- media_type is also added via ALTER in MIGRATIONS (39) for upgrades of a
-- database that already has this table; baked in here too (same pattern as
-- series_meta below) because this CREATE runs after the MIGRATIONS loop, so
-- on a fresh database the ALTER is skipped as benign and this line is the
-- only thing that supplies the column. It is an *advisory* scope: NULL means
-- the tag is global, and nothing strips or refuses an out-of-scope
-- association.
CREATE TABLE IF NOT EXISTS tags (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    media_type TEXT DEFAULT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS item_tags (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    tag_id  INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (item_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_item_tags_tag ON item_tags(tag_id);

-- The author index (#117b). An index over items.authors, which stays the
-- record: app/services/author_index.py is the only writer, called from the
-- item_write funnel and the boot rebuild. AUTOINCREMENT so an author id freed
-- by garbage collection is never reused by a different person — a kept
-- /browse?author_filter= URL must not silently point at someone else.
CREATE TABLE IF NOT EXISTS authors (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    name_key   TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS item_authors (
    item_id   INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    author_id INTEGER NOT NULL REFERENCES authors(id) ON DELETE CASCADE,
    position  INTEGER NOT NULL,
    role      TEXT,
    PRIMARY KEY (item_id, position)
);
CREATE INDEX IF NOT EXISTS idx_item_authors_author ON item_authors(author_id);

-- complete/hc_total/hc_missing/hc_checked_at are also added via ALTER in
-- MIGRATIONS (16-19) for upgrades of a database that already has this
-- table; baked in here too (same pattern as users.token_version above) so a
-- brand-new database gets them immediately — on first boot the MIGRATIONS
-- ALTERs run before this script creates the table, so they're no-ops here.
CREATE TABLE IF NOT EXISTS series_meta (
    name          TEXT PRIMARY KEY COLLATE NOCASE,
    description   TEXT,
    source        TEXT,
    updated_at    TEXT,
    complete      INTEGER DEFAULT NULL,
    hc_total      INTEGER DEFAULT NULL,
    hc_missing    INTEGER DEFAULT NULL,
    hc_checked_at TEXT DEFAULT NULL
);

-- Multiple ordered series memberships. The legacy items.series_name /
-- series_position pair remains the primary membership for compatibility.
CREATE TABLE IF NOT EXISTS item_series (
    item_id      INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    series_name  TEXT NOT NULL COLLATE NOCASE,
    position     REAL,
    is_primary   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (item_id, series_name)
);
CREATE INDEX IF NOT EXISTS idx_item_series_name ON item_series(series_name COLLATE NOCASE);

-- Curated collections are intentionally separate from Series and Tags. They
-- are shared library groupings and an item may belong to any number of them.
CREATE TABLE IF NOT EXISTS collections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS collection_items (
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    item_id       INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (collection_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_collection_items_item ON collection_items(item_id);

-- RomM digital-game availability (#100)
CREATE TABLE IF NOT EXISTS romm_records (
    romm_id         TEXT PRIMARY KEY,
    item_id         INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    platform_id     TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_romm_records_item
    ON romm_records(item_id);
CREATE INDEX IF NOT EXISTS idx_romm_records_platform
    ON romm_records(platform_id);

-- Komga digital comic/manga availability (#101)
CREATE TABLE IF NOT EXISTS komga_records (
    komga_id       TEXT PRIMARY KEY,
    item_id        INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    library_id     TEXT NOT NULL,
    series_id      TEXT,
    kind           TEXT NOT NULL CHECK(kind IN ('comic', 'manga')),
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_komga_records_item
    ON komga_records(item_id);
CREATE INDEX IF NOT EXISTS idx_komga_records_library
    ON komga_records(library_id, kind);

-- Periodical publications and issues (#106)
CREATE TABLE IF NOT EXISTS periodical_publications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    issn        TEXT UNIQUE COLLATE NOCASE,
    publisher   TEXT,
    language    TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_periodical_publications_title
    ON periodical_publications(title COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS periodical_issues (
    item_id             INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    publication_id      INTEGER NOT NULL REFERENCES periodical_publications(id) ON DELETE CASCADE,
    volume              TEXT,
    issue_number        TEXT,
    issue_date          TEXT,
    barcode_ean         TEXT,
    google_volume_id    TEXT,
    barcode_supplement  TEXT,
    cover_date_label    TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_periodical_issues_publication
    ON periodical_issues(publication_id, issue_date, issue_number);
CREATE INDEX IF NOT EXISTS idx_periodical_issues_barcode
    ON periodical_issues(barcode_ean, barcode_supplement);

-- Music releases, media, tracks and identifiers (#109)
CREATE TABLE IF NOT EXISTS music_releases (
    item_id                       INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    artist_credit                 TEXT,
    musicbrainz_release_id        TEXT UNIQUE,
    musicbrainz_release_group_id  TEXT,
    discogs_release_id            INTEGER,
    discogs_master_id             INTEGER,
    discogs_label                 TEXT,
    discogs_catalog_number        TEXT,
    discogs_format_summary        TEXT,
    discogs_genres                TEXT,
    discogs_styles                TEXT,
    discogs_notes                 TEXT,
    discogs_updated_at            TEXT,
    release_type                  TEXT,
    release_status                TEXT,
    release_date                  TEXT,
    first_release_date            TEXT,
    country                       TEXT,
    label                         TEXT,
    catalog_number                TEXT,
    packaging                     TEXT,
    media_count                   INTEGER,
    format_summary                TEXT,
    edition_notes                 TEXT,
    media_condition               TEXT,
    packaging_condition           TEXT,
    condition_notes               TEXT,
    metadata_source               TEXT,
    metadata_updated_at           TEXT
);
CREATE INDEX IF NOT EXISTS idx_music_releases_artist ON music_releases(artist_credit COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_music_releases_group ON music_releases(musicbrainz_release_group_id);
CREATE INDEX IF NOT EXISTS idx_music_releases_catalog ON music_releases(catalog_number COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_music_releases_discogs ON music_releases(discogs_release_id);
CREATE INDEX IF NOT EXISTS idx_music_releases_discogs_master ON music_releases(discogs_master_id);

CREATE TABLE IF NOT EXISTS music_media (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    format      TEXT,
    title       TEXT,
    track_count INTEGER,
    UNIQUE(item_id, position)
);
CREATE INDEX IF NOT EXISTS idx_music_media_item ON music_media(item_id);

CREATE TABLE IF NOT EXISTS music_tracks (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    medium_id                  INTEGER NOT NULL REFERENCES music_media(id) ON DELETE CASCADE,
    position                   INTEGER NOT NULL,
    number                     TEXT,
    title                      TEXT NOT NULL,
    artist_credit              TEXT,
    duration_ms                INTEGER,
    musicbrainz_recording_id   TEXT,
    UNIQUE(medium_id, position)
);
CREATE INDEX IF NOT EXISTS idx_music_tracks_medium ON music_tracks(medium_id);
CREATE INDEX IF NOT EXISTS idx_music_tracks_recording ON music_tracks(musicbrainz_recording_id);

CREATE TABLE IF NOT EXISTS music_identifiers (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id          INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    identifier_type  TEXT NOT NULL,
    value            TEXT NOT NULL,
    description      TEXT,
    source           TEXT NOT NULL DEFAULT 'manual',
    UNIQUE(item_id, identifier_type, value)
);
CREATE INDEX IF NOT EXISTS idx_music_identifiers_item ON music_identifiers(item_id);
CREATE INDEX IF NOT EXISTS idx_music_identifiers_value ON music_identifiers(value COLLATE NOCASE);

-- The table is also created by migration 46 for upgrades. Keeping its
-- complete definition here makes the fresh-database path explicit too.
CREATE TABLE IF NOT EXISTS legacy_book_mappings (
    barcode      TEXT PRIMARY KEY,
    isbn13       TEXT NOT NULL,
    confirmed_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK(length(barcode) = 17 AND barcode NOT GLOB '*[^0-9]*'),
    CHECK(length(isbn13) = 13
          AND isbn13 NOT GLOB '*[^0-9]*'
          AND substr(isbn13, 1, 3) IN ('978', '979'))
);

-- Named lists and their membership (#125). Also created by migrations 33 and
-- 34 for upgrades; the index lives only here, as item_copies' do.
CREATE TABLE IF NOT EXISTS lists (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    slug       TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS list_items (
    list_id  INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    item_id  INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (list_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_list_items_item ON list_items(item_id);

-- Release radar: series the user watches, and the announced volumes found for
-- them. Also created by migrations 49 and 50 for upgrades. Not in the portable
-- archive (G147): user-owned state that a move to a new server resets.
CREATE TABLE IF NOT EXISTS release_watches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    series_name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    checked_at  TEXT,                  -- last pass that got an answer; NULL = never
    check_state TEXT,                  -- NULL | 'ok' | 'not_found'
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS upcoming_releases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id        INTEGER NOT NULL REFERENCES release_watches(id) ON DELETE CASCADE,
    hc_book_id      INTEGER NOT NULL,
    title           TEXT NOT NULL,
    authors         TEXT,
    cover_url       TEXT,
    series_position REAL,
    release_date    TEXT,              -- ISO date, or NULL = announced, undated
    notified_at     TEXT,
    reminded_at     TEXT,
    dismissed       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (watch_id, hc_book_id)
);

-- API tokens: per-user bearer credentials, stored as a hash. Also created by
-- migration 52 for upgrades. Not in the portable archive (G147): credentials
-- belong to the server they were minted on.
CREATE TABLE IF NOT EXISTS api_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE,
    prefix       TEXT NOT NULL,
    last_used_at TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Published main43e244f5 registrants, now owned here unconditionally. Keep SQL,
# descriptions and numbers intact; 47 is deliberately NOT available for reuse.
_CREATE_USER_ITEM_STATE = """CREATE TABLE IF NOT EXISTS user_item_state (
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    item_id         INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    reading_status  TEXT CHECK(reading_status IS NULL OR reading_status IN ('want_to_read','reading','read')),
    date_started    TEXT,
    date_finished   TEXT,
    rating          INTEGER CHECK(rating IS NULL OR (rating >= 1 AND rating <= 5)),
    wishlist        INTEGER NOT NULL DEFAULT 0 CHECK(wishlist IN (0,1)),
    favourite       INTEGER NOT NULL DEFAULT 0 CHECK(favourite IN (0,1)),
    personal_notes  TEXT,
    progress_value  REAL,
    progress_total  REAL,
    progress_unit   TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, item_id)
)"""

_CREATE_USER_READING_LOG = """CREATE TABLE IF NOT EXISTS user_reading_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    item_id       INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    status        TEXT NOT NULL,
    date_started  TEXT,
    date_finished TEXT,
    notes         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
)"""

_USER_STATE_MIGRATIONS = (
    (48, "Add per-user item state table", _CREATE_USER_ITEM_STATE),
    (49, "Index per-user item state by item", "CREATE INDEX IF NOT EXISTS idx_user_item_state_item ON user_item_state(item_id)"),
    (50, "Index per-user consumption status", "CREATE INDEX IF NOT EXISTS idx_user_item_state_status ON user_item_state(user_id, reading_status)"),
    (51, "Index per-user wishlist", "CREATE INDEX IF NOT EXISTS idx_user_item_state_wishlist ON user_item_state(user_id, wishlist)"),
    (52, "Index per-user favourites", "CREATE INDEX IF NOT EXISTS idx_user_item_state_favourite ON user_item_state(user_id, favourite)"),
    (53, "Add per-user reading history table", _CREATE_USER_READING_LOG),
    (54, "Index per-user reading history", "CREATE INDEX IF NOT EXISTS idx_user_reading_log_user_item ON user_reading_log(user_id, item_id)"),
    (55, "Snapshot legacy item state for existing users", """
        INSERT OR IGNORE INTO user_item_state (
            user_id, item_id, reading_status, date_started, date_finished,
            wishlist, favourite
        )
        SELECT u.id, i.id, i.reading_status, i.date_started, i.date_finished,
               CASE WHEN i.owned = 0 THEN 1 ELSE 0 END, 0
          FROM users u
          CROSS JOIN items i
         WHERE i.reading_status IS NOT NULL
            OR i.date_started IS NOT NULL
            OR i.date_finished IS NOT NULL
            OR i.owned = 0
    """),
    (56, "Snapshot legacy reading history for existing users", """
        INSERT INTO user_reading_log (
            user_id, item_id, status, date_started, date_finished, notes, created_at
        )
        SELECT u.id, r.item_id, r.status, r.date_started, r.date_finished,
               r.notes, r.created_at
          FROM users u
          CROSS JOIN reading_log r
    """),
)

_CREATE_LIBRARIES = """CREATE TABLE IF NOT EXISTS libraries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description   TEXT,
    is_archived   INTEGER NOT NULL DEFAULT 0 CHECK(is_archived IN (0,1)),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
)"""

_CREATE_LIBRARY_MEMBERSHIPS = """CREATE TABLE IF NOT EXISTS library_memberships (
    library_id    INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role          TEXT NOT NULL CHECK(role IN ('viewer','editor')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (library_id, user_id)
)"""

_CREATE_LIBRARY_ITEMS = """CREATE TABLE IF NOT EXISTS library_items (
    item_id       INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    library_id    INTEGER NOT NULL REFERENCES libraries(id) ON DELETE RESTRICT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
)"""

_LIBRARY_MIGRATIONS = (
    (57, "Add Shelf libraries", _CREATE_LIBRARIES),
    (58, "Add per-library user memberships", _CREATE_LIBRARY_MEMBERSHIPS),
    (59, "Add one-library-per-item mapping", _CREATE_LIBRARY_ITEMS),
    (60, "Create default Main Library",
     "INSERT OR IGNORE INTO libraries (id, name, description) "
     "VALUES (1, 'Main Library', 'Default library created during upgrade')"),
    (61, "Assign existing catalogue items to Main Library",
     "INSERT OR IGNORE INTO library_items (item_id, library_id) SELECT id, 1 FROM items"),
    (62, "Seed Main Library memberships from existing roles",
     "INSERT OR IGNORE INTO library_memberships (library_id, user_id, role) "
     "SELECT 1, id, role FROM users WHERE role IN ('viewer','editor')"),
    (63, "Index library memberships by user",
     "CREATE INDEX IF NOT EXISTS idx_library_memberships_user ON library_memberships(user_id)"),
    (64, "Index catalogue items by library",
     "CREATE INDEX IF NOT EXISTS idx_library_items_library ON library_items(library_id)"),
)

_CREATE_PROVIDER_SERIES = """CREATE TABLE IF NOT EXISTS series_provider_meta (
    provider              TEXT NOT NULL,
    provider_series_id    TEXT NOT NULL,
    series_name           TEXT NOT NULL COLLATE NOCASE,
    kind                  TEXT NOT NULL DEFAULT 'series'
                          CHECK(kind IN ('series','franchise')),
    description           TEXT,
    publisher             TEXT,
    status                TEXT,
    age_rating            TEXT,
    total_items           INTEGER,
    total_duration_mins   INTEGER,
    metadata_json         TEXT,
    updated_at            TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (provider, provider_series_id)
)"""

_PROVIDER_SERIES_MIGRATIONS = (
    (65, "Add connected-service series metadata", _CREATE_PROVIDER_SERIES),
    (66, "Index connected-service series names",
     "CREATE INDEX IF NOT EXISTS idx_series_provider_meta_name "
     "ON series_provider_meta(series_name COLLATE NOCASE)"),
)

_FORK_MIGRATIONS = (
    tuple(_FORK_CORE_MIGRATIONS) + _USER_STATE_MIGRATIONS
    + _LIBRARY_MIGRATIONS + _PROVIDER_SERIES_MIGRATIONS
)

MIGRATION_TABLES += """
CREATE TABLE IF NOT EXISTS location_nodes (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id          INTEGER REFERENCES location_nodes(id) ON DELETE RESTRICT,
    name               TEXT NOT NULL,
    sort_order         INTEGER NOT NULL DEFAULT 0,
    legacy_location_id INTEGER UNIQUE REFERENCES locations(id) ON DELETE SET NULL,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_location_nodes_root_name
    ON location_nodes(name COLLATE NOCASE) WHERE parent_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_location_nodes_child_name
    ON location_nodes(parent_id, name COLLATE NOCASE) WHERE parent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_location_nodes_parent
    ON location_nodes(parent_id, sort_order, name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS digital_holdings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    provider    TEXT NOT NULL,
    external_id TEXT NOT NULL,
    library_id  TEXT,
    format      TEXT,
    source_url  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(item_id, provider, external_id)
);
CREATE INDEX IF NOT EXISTS idx_digital_holdings_item ON digital_holdings(item_id);
CREATE INDEX IF NOT EXISTS idx_digital_holdings_provider ON digital_holdings(provider, external_id);
"""

# Table bootstrap has no seed statements. In particular it must never replay
# personal snapshots (55/56) or library grants (62) from a request helper.
MIGRATION_TABLES += "\n" + "\n".join(
    sql + ";" for version, _description, sql in _FORK_MIGRATIONS
    if version >= 48 and version not in (55, 56, 60, 61, 62)
)

_NODE_ROOT_BACKFILL = """INSERT INTO location_nodes (name, sort_order, legacy_location_id)
    SELECT l.name, COALESCE(l.sort_order, 0), l.id FROM locations l
    WHERE NOT EXISTS (SELECT 1 FROM location_nodes n WHERE n.legacy_location_id = l.id)"""

# Upstream's flat-location backfill must never feed integer flat IDs into the
# fork's node FK. Existing copies, including child placements, are untouched.
# The media list is the published fork physical set; new media need an explicit
# storage policy, not a boot-time guess.
_NODE_COPY_BACKFILL = """INSERT INTO item_copies (item_id, copy_number, location_id, is_primary)
    SELECT i.id, 1, n.id, 1 FROM items i
    LEFT JOIN location_nodes n ON n.legacy_location_id = i.location_id
    WHERE i.owned = 1
      AND i.media_type IN ('book','kids_book','vinyl','cassette','cd','music_other',
                           'dvd','video_game','comic','manga','magazine')
      AND NOT EXISTS (SELECT 1 FROM item_copies c WHERE c.item_id = i.id)
      AND NOT EXISTS (SELECT 1 FROM settings
                      WHERE key = 'holdings_foundation_backfill_v1' AND value = '1')"""

_INLINE_HOLDINGS_BACKFILL = """INSERT INTO digital_holdings (item_id, provider, external_id, library_id)
    SELECT item_id, provider, external_id, library_id FROM (
        SELECT id AS item_id, 'audiobookshelf' AS provider, abs_id AS external_id,
               abs_library_id AS library_id FROM items
        UNION ALL SELECT id, 'romm', romm_id, romm_platform_id FROM items
        UNION ALL SELECT id, 'komga', komga_id, komga_library_id FROM items
    ) legacy
    WHERE external_id IS NOT NULL AND TRIM(external_id) != ''
      AND NOT EXISTS (SELECT 1 FROM digital_holdings d WHERE d.item_id = legacy.item_id
                      AND d.provider = legacy.provider AND d.external_id = legacy.external_id)
      AND NOT EXISTS (SELECT 1 FROM settings
                      WHERE key = 'holdings_foundation_backfill_v1' AND value = '1')"""

_ROMM_IDENTITY_BACKFILL = """INSERT INTO romm_records (romm_id, item_id, platform_id, created_at, updated_at)
    SELECT external_id, item_id, MIN(CASE WHEN TRIM(library_id) != '' THEN library_id END),
           MIN(created_at), MAX(updated_at)
    FROM (
        SELECT romm_id AS external_id, id AS item_id, romm_platform_id AS library_id,
               created_at, updated_at FROM items
        UNION ALL SELECT external_id, item_id, library_id, created_at, updated_at
                  FROM digital_holdings WHERE provider = 'romm'
    ) evidence WHERE external_id IS NOT NULL AND TRIM(external_id) != ''
    GROUP BY external_id, item_id"""

_KOMGA_IDENTITY_BACKFILL = """INSERT INTO komga_records
        (komga_id, item_id, library_id, series_id, kind, created_at, updated_at)
    SELECT e.external_id, e.item_id, MIN(CASE WHEN TRIM(e.library_id) != '' THEN e.library_id END),
           CASE WHEN e.external_id = i.komga_id THEN i.komga_series_id END,
           CASE WHEN i.media_type IN ('manga', 'digital_manga') THEN 'manga' ELSE 'comic' END,
           MIN(e.created_at), MAX(e.updated_at)
    FROM (
        SELECT komga_id AS external_id, id AS item_id, komga_library_id AS library_id,
               created_at, updated_at FROM items
        UNION ALL SELECT external_id, item_id, library_id, created_at, updated_at
                  FROM digital_holdings WHERE provider = 'komga'
    ) e JOIN items i ON i.id = e.item_id
    WHERE e.external_id IS NOT NULL AND TRIM(e.external_id) != ''
    GROUP BY e.external_id, e.item_id"""

_UPSTREAM_ADAPTATIONS = {
    25: """CREATE TABLE IF NOT EXISTS item_copies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        copy_number INTEGER NOT NULL DEFAULT 1,
        location_id INTEGER REFERENCES location_nodes(id) ON DELETE SET NULL,
        position_order INTEGER,
        condition TEXT, notes TEXT, acquired_date TEXT, acquisition_price REAL,
        copy_barcode TEXT UNIQUE,
        is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0, 1)),
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(item_id, copy_number))""",
    26: _NODE_ROOT_BACKFILL + ";\n" + _NODE_COPY_BACKFILL,
    27: "ALTER TABLE location_nodes ADD COLUMN parent_id INTEGER REFERENCES location_nodes(id) ON DELETE RESTRICT",
    28: "ALTER TABLE location_nodes ADD COLUMN name TEXT NOT NULL",
    29: _NODE_ROOT_BACKFILL,
    30: "CREATE INDEX IF NOT EXISTS idx_location_nodes_parent ON location_nodes(parent_id, sort_order, name COLLATE NOCASE)",
}
for _version, _description, _sql in _UPSTREAM_MIGRATIONS:
    if _version in (45, 46):
        _UPSTREAM_ADAPTATIONS[_version] = _sql.replace("REFERENCES locations(id)", "REFERENCES location_nodes(id)")

# Stable origin-qualified bridge: upstream 24..57 -> integrated 67..100.
# This is an adaptation map, NOT a claim the original migration was applied.
_BRIDGE_MIGRATIONS = tuple(
    (version + 43, f"Bridge upstream {version}: {description}",
     _UPSTREAM_ADAPTATIONS.get(version, sql))
    for version, description, sql in _UPSTREAM_MIGRATIONS
) + (
    (101, "Add fork copy acquisition source", "ALTER TABLE item_copies ADD COLUMN acquisition_source TEXT"),
    (102, "Add fork copy provenance", "ALTER TABLE item_copies ADD COLUMN provenance TEXT"),
    (103, "Centralize periodical carrier barcode", "ALTER TABLE periodical_issues ADD COLUMN barcode_ean TEXT"),
    (104, "Centralize periodical Google volume identity", "ALTER TABLE periodical_issues ADD COLUMN google_volume_id TEXT"),
    (105, "Index periodical Google volume identity",
     "CREATE UNIQUE INDEX IF NOT EXISTS idx_periodical_issues_google_volume "
     "ON periodical_issues(google_volume_id) WHERE google_volume_id IS NOT NULL"),
    (106, "Enforce one primary copy per item",
     "CREATE UNIQUE INDEX IF NOT EXISTS idx_item_copies_one_primary ON item_copies(item_id) WHERE is_primary = 1"),
    (107, "Snapshot fork inline digital holdings once", _INLINE_HOLDINGS_BACKFILL),
    (108, "Adopt exact fork RomM identities into sidecars", _ROMM_IDENTITY_BACKFILL),
    (109, "Adopt exact fork Komga identities into sidecars", _KOMGA_IDENTITY_BACKFILL),
    (110, "Record centralized fork holdings foundation",
     "INSERT INTO settings (key, value) VALUES ('holdings_foundation_backfill_v1', '1') "
     "ON CONFLICT(key) DO UPDATE SET value = '1'"),
    (111, "Centralize fork Discogs release identity", "ALTER TABLE music_releases ADD COLUMN discogs_release_id INTEGER"),
    (112, "Centralize fork music edition notes", "ALTER TABLE music_releases ADD COLUMN edition_notes TEXT"),
    (113, "Centralize fork music media condition", "ALTER TABLE music_releases ADD COLUMN media_condition TEXT"),
    (114, "Centralize fork music packaging condition", "ALTER TABLE music_releases ADD COLUMN packaging_condition TEXT"),
    (115, "Centralize fork music condition notes", "ALTER TABLE music_releases ADD COLUMN condition_notes TEXT"),
)
# These are immutable security snapshots, deliberately NOT foreign keys. Item,
# library or user purge must not erase the provenance needed to hide an orphan.
# Historical user ownership is unknown; only an extant item-library binding can
# be positively backfilled. Unknown past orphans remain NULL, never guessed.
_AUDIT_SECURITY_MIGRATIONS = (
    (116, "Add durable audit library security snapshot",
     "ALTER TABLE audit_entries ADD COLUMN security_library_id INTEGER"),
    (117, "Add durable audit actor security snapshot",
     "ALTER TABLE audit_entries ADD COLUMN security_user_id INTEGER"),
    (118, "Snapshot positively bound historical audit libraries",
     """UPDATE audit_entries SET security_library_id = (
            SELECT li.library_id FROM library_items li
            WHERE li.item_id = audit_entries.item_id
        ) WHERE security_library_id IS NULL AND item_id IS NOT NULL
          AND EXISTS (SELECT 1 FROM library_items li
                      WHERE li.item_id = audit_entries.item_id)"""),
)
_PRE_AUDIT_MIGRATIONS = _FORK_MIGRATIONS + _BRIDGE_MIGRATIONS
_PRE_SCAN_MIGRATIONS = _PRE_AUDIT_MIGRATIONS + _AUDIT_SECURITY_MIGRATIONS
_SCAN_SECURITY_MIGRATIONS = (
    (119, "Add durable scan library security snapshot",
     "ALTER TABLE scan_log ADD COLUMN security_library_id INTEGER"),
    (120, "Add durable scan actor security snapshot",
     "ALTER TABLE scan_log ADD COLUMN security_user_id INTEGER"),
    (121, "Snapshot positively bound historical scan libraries",
     """UPDATE scan_log SET security_library_id = (
            SELECT li.library_id FROM library_items li
            WHERE li.item_id = scan_log.item_id
        ) WHERE security_library_id IS NULL AND item_id IS NOT NULL
          AND EXISTS (SELECT 1 FROM library_items li
                      WHERE li.item_id = scan_log.item_id)"""),
)
MIGRATIONS: Sequence[tuple[int, str, str]] = _PRE_SCAN_MIGRATIONS + _SCAN_SECURITY_MIGRATIONS
_CANONICAL_MIGRATIONS = tuple(MIGRATIONS)

_LINEAGE = "fork-main-bridge-v1"
_MANIFEST_HASH = hashlib.sha256(repr(_CANONICAL_MIGRATIONS).encode()).hexdigest()
# Accept ONLY these exact, positively identified complete ledgers. A prior
# bridge gets pending append-only effects, not a new baseline or seed replay.
_MANIFEST_REVISIONS = {
    hashlib.sha256(repr(_PRE_AUDIT_MIGRATIONS).encode()).hexdigest(): _PRE_AUDIT_MIGRATIONS,
    hashlib.sha256(repr(_PRE_SCAN_MIGRATIONS).encode()).hexdigest(): _PRE_SCAN_MIGRATIONS,
    _MANIFEST_HASH: _CANONICAL_MIGRATIONS,
}
_PROVENANCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_lineage (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    lineage TEXT NOT NULL,
    initial_lineage TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS schema_migration_provenance (
    version INTEGER PRIMARY KEY REFERENCES schema_version(version) ON DELETE RESTRICT,
    source_lineage TEXT NOT NULL,
    original_version INTEGER NOT NULL,
    source_sql_sha256 TEXT NOT NULL,
    effect_sql_sha256 TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK(disposition IN
        ('legacy-recorded','executed','verified-existing','adapted')),
    notes TEXT NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_MIGRATION_TABLE_NAMES = frozenset(
    re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", MIGRATION_TABLES)
)


class SchemaLineageError(RuntimeError):
    """The database cannot be upgraded without an explicit conversion review."""


_ALTER = re.compile(r"\s*ALTER TABLE (\w+) ADD COLUMN (\w+)\b", re.I)
_CREATE = re.compile(r"\s*CREATE TABLE IF NOT EXISTS (\w+)\b", re.I)
_INDEX = re.compile(r"\s*CREATE (?:UNIQUE )?INDEX IF NOT EXISTS (\w+)\b", re.I)
_BRIDGE_TABLES = frozenset({
    "price_history", "audit_sessions", "audit_entries", "duplicate_dismissals",
    "authors", "item_authors", "romm_records", "komga_records", "lists",
    "list_items", "release_watches", "upcoming_releases", "api_tokens",
})
_METADATA_TABLES = frozenset({"schema_lineage", "schema_migration_provenance"})
_BRIDGE_COLUMNS = {
    "items": {"cover_review_dismissed", "deleted_at", "cover_edition_isbn",
              "cover_edition_language", "asin"},
    "item_copies": {"deleted_at", "missing_since", "acquisition_source", "provenance"},
    "users": {"language"},
    "tags": {"media_type"},
    "scan_log": {"security_library_id", "security_user_id"},
}


def _statements(script: str):
    """Split our static SQL without executescript's implicit transaction commit."""
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending.strip()
            pending = ""
    if pending.strip():
        # A final statement may omit its terminator, as MIGRATIONS always did.
        if not sqlite3.complete_statement(pending + ";"):
            raise ValueError("Incomplete central schema statement")
        yield pending.strip()


def _uncomment(sql: str) -> str:
    return re.sub(r"(?m)^\s*--[^\n]*(?:\n|$)", "", sql).strip()


def _columns(db, table: str) -> dict:
    # All callers supply centrally owned, validated SQL identifiers.
    return {r["name"]: r for r in db.execute(f'PRAGMA table_xinfo("{table}")')}


def _tables(db) -> set[str]:
    return {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )}


def _reference_schema():
    """A disposable shape oracle, never built from the database being upgraded."""
    reference = sqlite3.connect(":memory:")
    reference.row_factory = sqlite3.Row
    statements = list(_statements(SCHEMA + MIGRATION_TABLES + _PROVENANCE_SCHEMA))
    for sql in statements:
        if _CREATE.match(_uncomment(sql)):
            reference.execute(sql)
    for _version, _description, sql in _CANONICAL_MIGRATIONS:
        alter = _ALTER.match(sql)
        if alter and alter[2] not in _columns(reference, alter[1]):
            reference.execute(sql)
    for sql in statements + [sql for _v, _d, sql in _CANONICAL_MIGRATIONS]:
        if _INDEX.match(_uncomment(sql)):
            reference.execute(sql)
    return reference


def _index_shape(db, table: str) -> dict:
    result = {}
    for row in db.execute(f'PRAGMA index_list("{table}")'):
        name = row["name"]
        definition = db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()[0]
        predicate = re.split(r"\bWHERE\b", definition, maxsplit=1, flags=re.I) if definition else []
        predicate = _normalized_tokens(_sql_tokens(predicate[1])) if len(predicate) == 2 else ()
        # Names here come from sqlite_master, not centrally constrained input.
        quoted = name.replace('"', '""')
        keys = tuple((r["name"], r["desc"], r["coll"].casefold()) for r in db.execute(
            f'PRAGMA index_xinfo("{quoted}")'
        ) if r["key"])
        result[name] = (row["unique"], row["partial"], keys, predicate)
    return result


# SQLite exposes nullability/defaults/PKs and index collations through PRAGMAs,
# but not column collations or CHECK expressions. Parse ONLY schema metadata;
# never execute an admitted database's DDL in the canonical shape oracle.
_SQL_TOKEN = re.compile(
    r"--[^\n]*(?:\n|$)|/\*.*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|"
    r"`(?:``|[^`])*`|\[[^\]]*\]|[A-Za-z_][A-Za-z_0-9$]*|"
    r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|[<>=!]=|<>|\|\||\S",
    re.S,
)


def _sql_tokens(sql: str, *, quoted_identifiers: bool = True) -> tuple:
    tokens = []
    for match in _SQL_TOKEN.finditer(sql):
        value = match[0]
        if value.startswith(("--", "/*")):
            continue
        if value.startswith("'"):
            tokens.append(("literal", value[1:-1].replace("''", "'")))
        elif value.startswith(('"', '`', '[')):
            end = ']' if value[0] == '[' else value[0]
            unquoted = value[1:-1].replace(end + end, end)
            tokens.append(("word", unquoted.casefold()) if quoted_identifiers else ("literal", unquoted))
        elif value[0].isalpha() or value[0] == '_':
            tokens.append(("word", value.casefold()))
        else:
            tokens.append(("symbol", value))
    return tuple(tokens)


def _normalized_tokens(tokens: tuple) -> tuple:
    while len(tokens) >= 2 and tokens[0] == ("symbol", "(") and tokens[-1] == ("symbol", ")"):
        depth = 0
        for index, token in enumerate(tokens):
            depth += token == ("symbol", "(")
            depth -= token == ("symbol", ")")
            if depth == 0:
                break
        if index != len(tokens) - 1:
            break
        tokens = tokens[1:-1]
    return tokens


def _default_shape(value) -> tuple | None:
    if value is None:
        return None
    # In DEFAULT context double-quoted strings must not become identifiers or
    # keywords: DEFAULT "NULL" inserts text, NOT SQL NULL.
    tokens = _normalized_tokens(_sql_tokens(value, quoted_identifiers=False))
    # A missing DEFAULT and explicit DEFAULT NULL have identical SQLite insert
    # semantics. No column-specific nullability/default relaxation is allowed.
    return None if tokens == (("word", "null"),) else tokens


def _parenthesized(tokens: tuple, start: int) -> tuple[tuple, int]:
    if start >= len(tokens) or tokens[start] != ("symbol", "("):
        raise SchemaLineageError("Unsupported table constraint syntax")
    depth = 0
    for index in range(start, len(tokens)):
        depth += tokens[index] == ("symbol", "(")
        depth -= tokens[index] == ("symbol", ")")
        if depth == 0:
            return tokens[start + 1:index], index
    raise SchemaLineageError("Unbalanced table constraint syntax")


def _table_definition_parts(definition: str) -> tuple:
    tokens = _sql_tokens(definition)
    start = tokens.index(("symbol", "("))
    body, end = _parenthesized(tokens, start)
    parts, pending, depth = [], [], 0
    for token in body:
        if token == ("symbol", ",") and depth == 0:
            parts.append(tuple(pending))
            pending = []
            continue
        pending.append(token)
        depth += token == ("symbol", "(")
        depth -= token == ("symbol", ")")
    parts.append(tuple(pending))
    return body, parts, tokens[end + 1:]


def _table_contract(db, table: str) -> tuple:
    definition = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
    body, parts, suffix = _table_definition_parts(definition)
    collations, autoincrement = {}, set()
    names = {name.casefold() for name in _columns(db, table)}
    for part in parts:
        if not part or part[0][0] != "word" or part[0][1] not in names:
            continue
        name, depth, collation = part[0][1], 0, "binary"
        for index, token in enumerate(part):
            if depth == 0 and token == ("word", "collate"):
                if index + 1 >= len(part) or part[index + 1][0] != "word":
                    raise SchemaLineageError("Unsupported column collation syntax")
                collation = part[index + 1][1]
            if depth == 0 and token == ("word", "autoincrement"):
                autoincrement.add(name)
            depth += token == ("symbol", "(")
            depth -= token == ("symbol", ")")
        collations[name] = collation
    checks, modifiers = set(), []
    for index, token in enumerate(body):
        if token == ("word", "check"):
            expression, _end = _parenthesized(body, index + 1)
            checks.add(_normalized_tokens(expression))
        if token == ("word", "deferrable") or token == ("word", "initially"):
            modifiers.append(body[max(0, index - 1):index + 2])
        if body[index:index + 2] == (("word", "on"), ("word", "conflict")):
            policy = body[index + 2:index + 3]
            if policy != (("word", "abort"),):
                modifiers.append(policy)
    return collations, frozenset(checks), frozenset(autoincrement), tuple(modifiers), suffix


def _bootstrap_has_column(table: str, column: str) -> bool:
    """Whether trusted central CREATEs supply an ALTER's column by construction."""
    for sql in _statements(SCHEMA + MIGRATION_TABLES):
        create = _CREATE.match(_uncomment(sql))
        if create and create[1] == table:
            _body, parts, _suffix = _table_definition_parts(sql)
            return any(part and part[0] == ("word", column.casefold()) for part in parts)
    raise SchemaLineageError(f"Operation has no trusted bootstrap table: {table}")


def _unique_key(shape: tuple) -> tuple:
    _unique, partial, columns, predicate = shape
    # ASC/DESC affects ordering, not the uniqueness domain; collation does.
    return partial, tuple((name, collation) for name, _desc, collation in columns), predicate


def _validate_unique_keys(db, reference, table: str, actual: dict, expected: dict, columns: set[str], integrated: bool):
    expected_unique = {
        _unique_key(shape) for shape in expected.values()
        if shape[0] and all(name in columns for name, _desc, _coll in shape[2])
    }
    actual_unique = {_unique_key(shape) for shape in actual.values() if shape[0]}
    # Embedded PRIMARY KEY/UNIQUE identities must exist BEFORE bootstrap. A
    # missing named bootstrap index may still be installed in the migration
    # transaction, but CREATE IF NOT EXISTS cannot repair a missing table key.
    required = {
        _unique_key(expected[row["name"]])
        for row in reference.execute(f'PRAGMA index_list("{table}")')
        if row["origin"] in {"pk", "u"}
        and all(name in columns for name, _desc, _coll in expected[row["name"]][2])
    }
    if not required <= actual_unique or actual_unique - expected_unique:
        raise SchemaLineageError(f"Incompatible semantic unique keys/collations in {table}")
    if integrated and actual_unique != expected_unique:
        raise SchemaLineageError(f"Incomplete initialized unique keys in {table}")


def _validate_registry() -> dict[int, tuple[str, str]]:
    registry = {}
    for version, description, sql in MIGRATIONS:
        if version in registry or version == 47:
            raise SchemaLineageError(f"Conflicting or reserved migration identity: {version}")
        registry[version] = (description, sql)
    # Future additions may append, never replace or suppress published effects.
    if tuple(MIGRATIONS[:len(_CANONICAL_MIGRATIONS)]) != _CANONICAL_MIGRATIONS:
        raise SchemaLineageError("Published migration manifest was changed or reordered")
    if any(v <= _CANONICAL_MIGRATIONS[-1][0] for v, _d, _s in MIGRATIONS[len(_CANONICAL_MIGRATIONS):]):
        raise SchemaLineageError("New operations must append above the bridge manifest")
    return registry


def _origin(version: int, sql: str) -> tuple[str, int, str]:
    if version <= 66:
        return "fork-main43e244f5", version, sql
    if 67 <= version <= 100:
        original = version - 43
        source = next(s for v, _d, s in _UPSTREAM_MIGRATIONS if v == original)
        return "upstream7e0369e7", original, source
    return "fork-main-bridge-v1", version, sql


def _digest(sql: str) -> str:
    return hashlib.sha256(sql.encode()).hexdigest()


def _validate_shape(db, tables: set[str], *, integrated: bool, history=frozenset()) -> None:
    reference = _reference_schema()
    try:
        allowed_tables = _tables(reference)
        if not integrated:
            allowed_tables -= _BRIDGE_TABLES | _METADATA_TABLES
        unknown = tables - allowed_tables
        if unknown:
            raise SchemaLineageError(f"Unsupported or mixed schema tables: {sorted(unknown)}")
        for table in tables:
            actual = _columns(db, table)
            expected = _columns(reference, table)
            allowed_columns = set(expected)
            if integrated:
                # The exact known manifest/history selects the initialized
                # shape. Do not pretend an old bridge already has new columns,
                # nor accept unrecorded partial effects under its old ledger.
                allowed_columns -= {
                    alter[2] for version, _description, sql in _CANONICAL_MIGRATIONS
                    if version not in history and (alter := _ALTER.match(sql)) and alter[1] == table
                }
            else:
                allowed_columns -= _BRIDGE_COLUMNS.get(table, set())
            if set(actual) - allowed_columns:
                raise SchemaLineageError(f"Unsupported or mixed columns in {table}")
            allowed_missing = {
                alter[2] for version, _description, sql in _CANONICAL_MIGRATIONS
                if version not in history and (alter := _ALTER.match(sql)) and alter[1] == table
            }
            if allowed_columns - set(actual) - allowed_missing:
                message = "Incomplete initialized schema" if integrated else "Missing unrecognized base columns"
                raise SchemaLineageError(f"{message} in {table}")
            for name, column in actual.items():
                target = expected[name]
                if (column["type"].upper(), column["pk"], column["notnull"], column["hidden"], _default_shape(column["dflt_value"])) != (
                    target["type"].upper(), target["pk"], target["notnull"], target["hidden"], _default_shape(target["dflt_value"])
                ):
                    raise SchemaLineageError(f"Incompatible schema identity: {table}.{name}")
            actual_contract, expected_contract = _table_contract(db, table), _table_contract(reference, table)
            if actual_contract[0] != {name: collation for name, collation in expected_contract[0].items() if name in actual}:
                raise SchemaLineageError(f"Incompatible column collations in {table}")
            if actual_contract[1:] != expected_contract[1:]:
                raise SchemaLineageError(f"Incompatible table constraints in {table}")
            actual_fks = {
                (r["from"], r["table"], r["to"], r["on_delete"], r["on_update"], r["match"])
                for r in db.execute(f'PRAGMA foreign_key_list("{table}")')
            }
            expected_fks = {
                (r["from"], r["table"], r["to"], r["on_delete"], r["on_update"], r["match"])
                for r in reference.execute(f'PRAGMA foreign_key_list("{table}")')
                if r["from"] in actual
            }
            if actual_fks != expected_fks:
                raise SchemaLineageError(f"Incompatible foreign-key domain in {table}")
            if any(target not in tables for _col, target, _to, _delete, _update, _match in actual_fks):
                raise SchemaLineageError(f"Missing foreign-key prerequisite for {table}")
            if integrated and set(actual) != allowed_columns:
                raise SchemaLineageError(f"Incomplete initialized schema in {table}")
            actual_indexes, expected_indexes = _index_shape(db, table), _index_shape(reference, table)
            _validate_unique_keys(db, reference, table, actual_indexes, expected_indexes, set(actual), integrated)
            actual_named = {name for name in actual_indexes if not name.startswith("sqlite_")}
            expected_named = {name for name in expected_indexes if not name.startswith("sqlite_")}
            for name in actual_named & expected_named:
                if actual_indexes[name] != expected_indexes[name]:
                    raise SchemaLineageError(f"Conflicting managed index: {name}")
            if integrated and expected_named - actual_named:
                raise SchemaLineageError(f"Missing initialized indexes in {table}")
        if integrated and tables != allowed_tables:
            raise SchemaLineageError("Initialized schema is missing required tables")
    finally:
        reference.close()


def inspect_schema_lineage(db) -> str:
    """Classify in ONE read snapshot, without changing journal mode or caller work.

    A deferred read transaction prevents mixed table/history/column inventories
    when another WAL connection completes a valid upgrade between our queries.
    An existing caller transaction is neither committed, rolled back nor replaced.
    """
    own_snapshot = not db.in_transaction
    if own_snapshot:
        db.execute("BEGIN")
    try:
        lineage = _inspect_schema_snapshot(db)
        # This applies to every supported path, including integrated no-op and
        # genuinely empty databases. Never let fast-path initialization bypass
        # integrity admission before WAL or persistent DDL/data changes.
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise SchemaLineageError("Existing foreign-key violations require explicit repair")
        if [r[0] for r in db.execute("PRAGMA quick_check")] != ["ok"]:
            raise SchemaLineageError("Existing database failed SQLite quick_check")
        return lineage
    finally:
        if own_snapshot:
            # SQL rollback closes only OUR read snapshot; do not run connection
            # subclass hooks which could discard a caller's queued callbacks.
            db.execute("ROLLBACK")


def _inspect_schema_snapshot(db) -> str:
    """Read-only classification inside the caller/inspector's consistent snapshot.

    Numeric versions alone are insufficient: every recorded description must
    match its fork identity and physical/schema evidence must agree. Upstream,
    unknown, unversioned populated and mixed databases are refused untouched.
    """
    registry = _validate_registry()
    tables = _tables(db)
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('view','trigger') "
        "OR (type = 'table' AND upper(sql) LIKE 'CREATE VIRTUAL TABLE%') LIMIT 1"
    ).fetchone():
        raise SchemaLineageError("Unsupported persistent view, trigger or virtual table")
    if not tables:
        return "fresh"
    if "schema_version" not in tables:
        raise SchemaLineageError("Unversioned existing database; explicit lineage review required")
    if set(_columns(db, "schema_version")) != {"version", "description", "applied_at"}:
        raise SchemaLineageError("Unsupported migration history shape")
    history = {r["version"]: r["description"] for r in db.execute(
        "SELECT version, description FROM schema_version"
    )}
    for version, description in history.items():
        if version not in registry or registry[version][0] != description:
            raise SchemaLineageError(f"Unknown, upstream or mixed migration history at {version}")
    metadata = tables & _METADATA_TABLES
    integrated = bool(metadata)
    if integrated:
        if metadata != _METADATA_TABLES:
            raise SchemaLineageError("Incomplete bridge lineage metadata")
        if set(_columns(db, "schema_lineage")) != {
            "id", "lineage", "initial_lineage", "manifest_sha256", "recorded_at"
        }:
            raise SchemaLineageError("Unsupported bridge lineage metadata shape")
        rows = db.execute("SELECT * FROM schema_lineage").fetchall()
        if len(rows) != 1 or rows[0]["id"] != 1 or rows[0]["lineage"] != _LINEAGE:
            raise SchemaLineageError("Unknown bridge lineage or manifest")
        initial_lineage = rows[0]["initial_lineage"]
        if initial_lineage not in {"fresh", "common-prefix", "fork"}:
            raise SchemaLineageError("Unsupported initial schema lineage")
        revision = _MANIFEST_REVISIONS.get(rows[0]["manifest_sha256"])
        if revision is None:
            raise SchemaLineageError("Unknown bridge lineage or manifest")
        if {v for v, _d, _s in revision} != set(history):
            raise SchemaLineageError("Initialized bridge history does not match its manifest; refusing seed replay")
        _validate_shape(db, tables, integrated=True, history=history)
        provenance = {r["version"]: r for r in db.execute("SELECT * FROM schema_migration_provenance")}
        if set(provenance) != set(history):
            raise SchemaLineageError("History and operation provenance disagree")
        legacy = {v for v, row in provenance.items() if row["disposition"] == "legacy-recorded"}
        if (initial_lineage == "fresh" and legacy
                or initial_lineage == "common-prefix" and (not legacy or any(v > 23 for v in legacy))
                or initial_lineage == "fork" and not any(24 <= v <= 66 for v in legacy)):
            raise SchemaLineageError("Initial lineage and historical operation dispositions disagree")
        for version, row in provenance.items():
            sql = registry[version][1]
            origin, original, source = _origin(version, sql)
            if (row["source_lineage"], row["original_version"], row["source_sql_sha256"], row["effect_sql_sha256"]) != (
                origin, original, _digest(source), _digest(sql)
            ):
                raise SchemaLineageError(f"Operation provenance mismatch at {version}")
            _validate_disposition(version, sql, row["disposition"], initial_lineage)
        return "integrated"

    _validate_shape(db, tables, integrated=False, history=history)
    if not history:
        # SCHEMA-only, empty bootstrap is the sole allowed pre-version case.
        base_tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA))
        if tables != base_tables or any(db.execute(f'SELECT 1 FROM "{t}" LIMIT 1').fetchone() for t in tables):
            raise SchemaLineageError("Ambiguous unversioned existing data; refusing snapshot replay")
        reference = sqlite3.connect(":memory:")
        reference.row_factory = sqlite3.Row
        try:
            reference.executescript(SCHEMA)
            if any(set(_columns(db, t)) != set(_columns(reference, t)) for t in tables):
                raise SchemaLineageError("Ambiguous unrecorded schema effects")
        finally:
            reference.close()
        return "fresh"
    if any(v > 66 for v in history):
        raise SchemaLineageError("Bridge effects without provenance")

    # Empty/deleted targets and missing version rows are NOT positive evidence
    # that an old private/security seed never ran. With an existing domain we
    # cannot distinguish a pending seed from cleared/revoked state plus erased
    # bookkeeping. Only a genuinely pre-feature domain absence is supported;
    # do not invent recovery markers or regrant/re-snapshot into later users.
    if tables & {"user_item_state", "user_reading_log"} and not {55, 56} <= set(history):
        raise SchemaLineageError("Ambiguous preexisting personal domain without complete seed history")
    if tables & {"libraries", "library_memberships", "library_items"} and 62 not in history:
        raise SchemaLineageError("Ambiguous preexisting library domain without membership seed history")

    # Main's independently registered domains could precede core46, but each
    # published domain itself was applied in order. Only core1..21 shipped the
    # pre-atomic runner; higher-number holes must not request a seed/DDL replay.
    for block in (range(22, 47), range(48, 57), range(57, 65), range(65, 67)):
        recorded = set(block) & set(history)
        if recorded and not set(range(block.start, max(recorded) + 1)) <= set(history):
            raise SchemaLineageError("Ambiguous non-prefix published domain history")

    for version, _description, sql in _FORK_MIGRATIONS:
        alter, create = _ALTER.match(sql), _CREATE.match(sql)
        if version in history:
            if alter and alter[1] in tables and alter[2] not in _columns(db, alter[1]):
                raise SchemaLineageError(f"Recorded migration {version} is missing its column")
            if create and create[1] not in tables:
                raise SchemaLineageError(f"Recorded migration {version} is missing its table")
    for seed, prerequisites in ((55, range(48, 55)), (56, range(48, 56)), (62, range(57, 62))):
        if seed in history and not set(prerequisites) <= set(history):
            raise SchemaLineageError(f"Incomplete one-time seed prerequisites at {seed}")
    if "location_nodes" in tables:
        # A root-name collision must not be guessed into a legacy-ID binding.
        if db.execute(
            "SELECT 1 FROM locations l JOIN location_nodes n ON n.parent_id IS NULL "
            "AND n.name = l.name COLLATE NOCASE WHERE NOT EXISTS "
            "(SELECT 1 FROM location_nodes mapped WHERE mapped.legacy_location_id = l.id) LIMIT 1"
        ).fetchone():
            raise SchemaLineageError("Ambiguous legacy location-to-node mapping")
    if "item_copies" in tables and db.execute(
        "SELECT 1 FROM item_copies WHERE is_primary = 1 GROUP BY item_id HAVING COUNT(*) > 1 LIMIT 1"
    ).fetchone():
        raise SchemaLineageError("Ambiguous duplicate primary copies; refusing automatic repair")
    if "settings" in tables:
        marker = db.execute(
            "SELECT value FROM settings WHERE key='holdings_foundation_backfill_v1'"
        ).fetchone()
        if marker and marker[0] not in {"0", "1"}:
            raise SchemaLineageError("Unknown holdings foundation history marker")
    _validate_connector_evidence(db, tables)
    return "fork" if any(v >= 24 for v in history) else "common-prefix"


def _validate_disposition(version: int, sql: str, disposition: str, initial_lineage: str) -> None:
    if 67 <= version <= 100 and version - 43 in _UPSTREAM_ADAPTATIONS:
        allowed = {"adapted"}
    elif _CREATE.match(sql):
        # Every known CREATE's table is centrally bootstrapped before the loop.
        allowed = {"verified-existing"}
    elif alter := _ALTER.match(sql):
        if initial_lineage == "fresh" and version <= 115:
            allowed = {"verified-existing" if _bootstrap_has_column(alter[1], alter[2]) else "executed"}
        elif version in (119, 120):
            # Neither old supported manifest nor SCHEMA contains these fields;
            # an unrecorded partial scan ALTER is refused, never adopted.
            allowed = {"executed"}
        else:
            # In particular116/117 may have been supplied on fresh bootstrap OR
            # ALTERed during a genuine prior115 upgrade with initial=fresh.
            allowed = {"executed", "verified-existing"}
    elif _INDEX.match(sql):
        allowed = {"executed"} if initial_lineage == "fresh" else {"executed", "verified-existing"}
    else:
        allowed = {"executed"}
    if version <= 66 and (initial_lineage == "fork" or initial_lineage == "common-prefix" and version <= 23):
        allowed.add("legacy-recorded")
    if disposition not in allowed:
        raise SchemaLineageError(f"Invalid origin/effect disposition for operation {version}")


def _validate_connector_evidence(db, tables: set[str]) -> None:
    """Require complete, unambiguous evidence before one-time sidecar adoption.

    No provider calls, identifier-based reassignment, same-name adoption, or
    plural->inline collapse. This gate runs before even bootstrap DDL.
    """
    if "items" not in tables:
        raise SchemaLineageError("Existing history has no catalogue table")
    evidence = {}
    items = {r["id"]: r for r in db.execute("SELECT * FROM items")}

    def add(provider, external, item_id, library):
        if external is None or not str(external).strip():
            return
        key = (provider, str(external))
        item_ids, libraries = evidence.setdefault(key, (set(), set()))
        item_ids.add(item_id)
        if library is not None and str(library).strip():
            libraries.add(str(library))

    for item_id, item in items.items():
        keys = set(item.keys())
        for provider, field, library in (
            ("audiobookshelf", "abs_id", "abs_library_id"),
            ("romm", "romm_id", "romm_platform_id"),
            ("komga", "komga_id", "komga_library_id"),
        ):
            if field in keys:
                add(provider, item[field], item_id, item[library] if library in keys else None)
    if "digital_holdings" in tables:
        for row in db.execute("SELECT item_id, provider, external_id, library_id FROM digital_holdings"):
            add(row["provider"], row["external_id"], row["item_id"], row["library_id"])
    for (provider, _external), (item_ids, libraries) in evidence.items():
        if len(item_ids) != 1 or len(libraries) > 1:
            raise SchemaLineageError(f"Ambiguous {provider} connector identity; explicit reconciliation required")
        if provider in {"komga", "romm"} and not libraries:
            raise SchemaLineageError(f"Incomplete {provider} connector library/platform evidence")
        if provider == "komga":
            item = items.get(next(iter(item_ids)))
            if item is None or item["media_type"] not in {"comic", "manga", "digital_comic", "digital_manga"}:
                raise SchemaLineageError("Unsupported Komga kind evidence; refusing a guessed conversion")


def _record_provenance(db, version: int, sql: str, disposition: str, notes: str) -> None:
    origin, original, source = _origin(version, sql)
    db.execute(
        "INSERT INTO schema_migration_provenance "
        "(version, source_lineage, original_version, source_sql_sha256, effect_sql_sha256, disposition, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (version, origin, original, _digest(source), _digest(sql), disposition, notes),
    )


def _apply_migration(db, version: int, sql: str) -> str:
    alter, create, index = _ALTER.match(sql), _CREATE.match(sql), _INDEX.match(sql)
    existing = False
    if alter and alter[2] in _columns(db, alter[1]):
        # Only frozen, known effects may be verified rather than executed.
        # A newly appended duplicate ALTER is a real defect, not a recovery.
        if version not in {v for v, _d, _s in _CANONICAL_MIGRATIONS}:
            db.execute(sql)
        existing = True
    elif create or index:
        name = (create or index)[1]
        existing = bool(db.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone())
        db.execute(sql)
    else:
        # An explicitly adapted operation can include multiple ordered effects.
        # Its provenance fingerprint covers ALL of them, and executescript is
        # never used (it would implicitly commit the migration transaction).
        for statement in _statements(sql):
            db.execute(statement)
    if 67 <= version <= 100 and version - 43 in _UPSTREAM_ADAPTATIONS:
        return "adapted"
    return "verified-existing" if existing else "executed"


def _run_migrations(db: sqlite3.Connection) -> list[str]:
    """Atomically install the central schema, effects and provenance.

    Classification precedes ALL persistent writes and is repeated under the
    write lock. No executescript, missing-table forgiveness, logging, router
    registration, destructive retirement or independently committing service
    callback belongs inside this transaction. Caller transactions are preserved.
    """
    initial = inspect_schema_lineage(db)
    nested = db.in_transaction
    db.execute("SAVEPOINT shelf_schema_bridge" if nested else "BEGIN IMMEDIATE")
    try:
        initial = inspect_schema_lineage(db)
        tables = _tables(db)
        history = set() if "schema_version" not in tables else {
            r[0] for r in db.execute("SELECT version FROM schema_version")
        }
        if initial == "integrated" and history == {v for v, _d, _s in MIGRATIONS}:
            if nested:
                db.execute("RELEASE SAVEPOINT shelf_schema_bridge")
            else:
                db.commit()
            return []
        # Tables first; all prerequisites exist when data effects execute.
        # Indexes wait until ALTERs finish. Baked-in columns are explicitly
        # verified, never treated as SQL that ran unchanged.
        statements = list(_statements(SCHEMA + MIGRATION_TABLES + _PROVENANCE_SCHEMA))
        for sql in statements:
            if _CREATE.match(_uncomment(sql)):
                db.execute(sql)
        logs = []
        for version, description, sql in MIGRATIONS:
            if version in history:
                if initial != "integrated":
                    _record_provenance(db, version, sql, "legacy-recorded",
                                       "Retained published fork history; not replayed or asserted executed")
                continue
            disposition = _apply_migration(db, version, sql)
            db.execute("INSERT INTO schema_version (version, description) VALUES (?, ?)", (version, description))
            _record_provenance(db, version, sql, disposition,
                               "Node-domain compatibility adaptation" if disposition == "adapted"
                               else "Central prerequisite bootstrap verified" if disposition == "verified-existing"
                               else "SQL executed in the same transaction as this history row")
            logs.append(f"Applied migration {version}: {description} ({disposition})")
        for sql in statements:
            if _INDEX.match(_uncomment(sql)):
                db.execute(sql)
        if initial != "integrated":
            _seed_game_platforms(db)
        db.execute(
            "INSERT INTO schema_lineage (id, lineage, initial_lineage, manifest_sha256) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET manifest_sha256 = excluded.manifest_sha256",
            (_LINEAGE, initial, _MANIFEST_HASH),
        )
        # Postconditions are part of the transaction, including shape/FKs.
        inspect_schema_lineage(db)
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise SchemaLineageError("Schema bridge produced a foreign-key violation")
        if nested:
            db.execute("RELEASE SAVEPOINT shelf_schema_bridge")
        else:
            db.commit()
    except BaseException:
        if nested:
            db.execute("ROLLBACK TO SAVEPOINT shelf_schema_bridge")
            db.execute("RELEASE SAVEPOINT shelf_schema_bridge")
        else:
            db.rollback()
        raise
    if initial == "fresh":
        logs.insert(0, f"Backfilled {len(MIGRATIONS)} migration records on an empty database")
    return logs


def validate_and_migrate_staged_connection(db: sqlite3.Connection) -> dict[str, object]:
    """Validate/migrate ONLY a caller-owned disposable staging connection.

    Requires an idle connection with sqlite3.Row; does not open a path, select
    DATA_DIR, change globals/env, create backups/covers, install views, rebuild
    derived service indexes, replace the live DB or log. The caller retains and
    closes the connection and owns separate upload validation/atomic replacement.
    Success commits the schema/ledger transaction and returns its lineage/report.
    """
    if db.in_transaction or db.row_factory is not sqlite3.Row:
        raise ValueError("Staged migration requires an idle sqlite3.Row connection")
    initial = inspect_schema_lineage(db)
    if [r[0] for r in db.execute("PRAGMA quick_check")] != ["ok"]:
        raise SchemaLineageError("Staged database failed SQLite quick_check")
    if db.execute("PRAGMA foreign_key_check").fetchone():
        raise SchemaLineageError("Staged database has foreign-key violations")
    db.execute("PRAGMA foreign_keys=ON")
    logs = _run_migrations(db)
    return {
        "initial_lineage": initial,
        "lineage": inspect_schema_lineage(db),
        "manifest_sha256": _MANIFEST_HASH,
        "migration_logs": logs,
    }


def _ensure_initialized_tables(db, names: set[str]) -> None:
    """Compatibility helpers validate central ownership; never lazy-seed users."""
    if not getattr(db, "_schema_lineage_verified", False):
        if inspect_schema_lineage(db) != "integrated":
            raise SchemaLineageError("Central schema is not initialized; run init_db before domain services")
    tables = _tables(db)
    if not _METADATA_TABLES <= tables or not names <= tables:
        raise SchemaLineageError("Central schema is not initialized; run init_db before domain services")
    row = db.execute("SELECT lineage, manifest_sha256 FROM schema_lineage WHERE id = 1").fetchone()
    if not row or (row[0], row[1]) != (_LINEAGE, _MANIFEST_HASH):
        raise SchemaLineageError("Unsupported central schema lineage")


def ensure_library_schema(db) -> None:
    _ensure_initialized_tables(db, {"libraries", "library_memberships", "library_items"})


def ensure_user_state_schema(db) -> None:
    _ensure_initialized_tables(db, {"user_item_state", "user_reading_log"})


def ensure_provider_series_schema(db) -> None:
    _ensure_initialized_tables(db, {"series_provider_meta"})


def ensure_holdings_schema(db) -> None:
    _ensure_initialized_tables(db, {"location_nodes", "item_copies", "digital_holdings"})


def ensure_audit_schema(db) -> None:
    """Validate durable audit security provenance, never repair or infer it."""
    _ensure_initialized_tables(db, {"audit_sessions", "audit_entries"})
    if not {"security_library_id", "security_user_id"} <= set(_columns(db, "audit_entries")):
        raise SchemaLineageError("Audit schema requires initialized durable security snapshots")


def ensure_scan_history_schema(db) -> None:
    """Validate scan provenance without lazy DDL, seeding or orphan inference."""
    _ensure_initialized_tables(db, {"scan_log"})
    if not {"security_library_id", "security_user_id"} <= set(_columns(db, "scan_log")):
        raise SchemaLineageError("Scan history requires initialized durable security snapshots")


def ensure_series_memberships_schema(db) -> None:
    _ensure_initialized_tables(db, {"item_series"})


def ensure_periodical_schema(db) -> None:
    _ensure_initialized_tables(db, {"periodical_publications", "periodical_issues"})


def _seed_game_platforms(db: sqlite3.Connection) -> None:
    """Seed game_platforms table from config defaults if empty."""
    count = db.execute("SELECT COUNT(*) as c FROM game_platforms").fetchone()["c"]
    if count > 0:
        return
    from app.config import GAME_PLATFORMS
    for i, (slug, name) in enumerate(GAME_PLATFORMS.items()):
        db.execute(
            "INSERT OR IGNORE INTO game_platforms (slug, name, sort_order) VALUES (?, ?, ?)",
            (slug, name, i),
        )


def get_setting(db, key: str) -> str:
    """Get a single setting value with env var override.

    Sensitive values stored encrypted in the DB are transparently decrypted.
    """
    from app.config import get_setting_value
    from app.crypto import SENSITIVE_KEYS, decrypt_value, get_encryption_key
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    raw = row["value"] if row else None
    if raw and key in SENSITIVE_KEYS:
        raw = decrypt_value(raw, get_encryption_key(), key_name=key)
    return get_setting_value(key, raw)


def set_setting(db, key: str, value: str) -> None:
    """Write one setting row, replacing any stored value.

    Non-sensitive keys only: this stores `value` as given. A key in
    `crypto.SENSITIVE_KEYS` goes through `routers/settings._upsert_setting`,
    which encrypts it first and then calls this.
    """
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
        (key, value, value),
    )


def get_all_settings(db) -> dict[str, str]:
    """Get all settings as a dict with env overrides applied.

    Sensitive values are decrypted before being returned.
    """
    from app.config import get_setting_value
    from app.crypto import SENSITIVE_KEYS, decrypt_value, get_encryption_key
    rows = db.execute("SELECT key, value FROM settings").fetchall()
    secret = get_encryption_key()
    settings = {}
    for r in rows:
        val = r["value"]
        if val and r["key"] in SENSITIVE_KEYS:
            val = decrypt_value(val, secret, key_name=r["key"])
        settings[r["key"]] = val
    return {k: get_setting_value(k, v) for k, v in settings.items()}


def get_game_platforms(db) -> dict[str, str]:
    """Get game platforms as {slug: name} dict, ordered by sort_order then name."""
    rows = db.execute(
        "SELECT slug, name FROM game_platforms ORDER BY sort_order, name"
    ).fetchall()
    return {r["slug"]: r["name"] for r in rows}


def get_reading_history(db, item_id: int) -> list:
    """Every reading_log row for an item, newest first.

    Read-only; rendered by fragments/reading_status.html from BOTH of its
    renderers — pages.item_detail and items.set_reading_status. Wiring only
    one of them leaves the fragment's history silently empty after an HTMX
    status toggle.

    No LIMIT: the "Read N times" heading must be the true count, and a
    per-item row count is bounded by human reading. Rows are not filtered by
    status — the app only ever inserts 'read', and archive-imported rows
    should render too. Indexed by idx_reading_log_item.
    """
    return db.execute(
        "SELECT id, status, date_started, date_finished FROM reading_log "
        "WHERE item_id = ? ORDER BY date_finished DESC, id DESC",
        (item_id,),
    ).fetchall()


def gc_orphaned_series_meta(db, *names: str | None) -> None:
    """Delete series_meta rows for any of the given series names that no
    longer have any item pointing at them (case-insensitive, matching the
    NOCASE collation on both series_meta.name and series_name usage).

    Pass the OLD series name(s) a write just moved items away from — a
    series_meta row can only go orphaned when its name stops being
    referenced, so there's never a reason to GC a brand-new name.

    Call this against the same `db` connection/transaction that performed
    the items UPDATE, and only after that UPDATE has executed. SQLite
    connections see their own uncommitted writes, so this does not need to
    wait for get_db()'s commit-on-exit — but it does need the UPDATE to have
    already run on this connection, or the "still referenced?" check below
    will see stale rows.

    Modeled on the tag GC in app/routers/tags.py's remove_tag().
    """
    seen: set[str] = set()
    for name in names:
        if not name:
            continue
        key = name.strip().casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        db.execute(
            "DELETE FROM series_meta WHERE name = ? COLLATE NOCASE "
            "AND NOT EXISTS ("
            "SELECT 1 FROM items WHERE series_name = ? COLLATE NOCASE"
            ") AND NOT EXISTS ("
            "SELECT 1 FROM item_series WHERE series_name = ? COLLATE NOCASE"
            ")",
            (name, name, name),
        )


def init_db():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Do not open through get_db: its WAL setup is a persistent change and its
    # request views assume the schema has already passed lineage validation.
    db = sqlite3.connect(str(DATABASE_PATH), factory=_Connection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        migration_logs = _run_migrations(db)
        db.execute("PRAGMA journal_mode=WAL")
        # Public defaults are boot maintenance, not a reason to reopen the
        # immutable migration ledger or replay private/security snapshots. A
        # validated integrated install may legitimately have an empty table.
        # Serialize the empty check and seed against other startup writers.
        db.execute("BEGIN IMMEDIATE")
        _seed_game_platforms(db)
        db.commit()
        # Derived index rebuild remains upstream-owned, AFTER the atomic bridge
        # committed. It cannot commit an in-progress schema/data migration.
        from app.services import author_index
        built = author_index.rebuild_all(db)
        if built:
            migration_logs.append(built)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    # Only now, with the migration transaction committed and its connection
    # closed, is it safe for SQLiteHandler to open its own connection and
    # write these records to log_entries.
    for line in migration_logs:
        logger.info("%s", line)


class _Connection(sqlite3.Connection):
    """Ordinary notifications and protected resource outcomes are separate.

    Critical callbacks are synchronous filesystem-only operations: no SQL,
    logging, network or awaits. In particular, recovery must not reacquire a
    SQLite lock after an engine failure has already released it. SQL transaction
    boundaries and direct calls to sqlite3's base methods are not supported for
    resource-owning transactions; use these commit/rollback/close methods.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._after_commit = []
        self._after_rollback = []
        self._critical_resource_outcomes = []
        self._resource_outcome_running = False
        self._closed = False
        self._schema_lineage_verified = False

    @staticmethod
    def _annotate_cleanup_errors(primary, errors):
        if errors:
            primary.resource_cleanup_errors = (*getattr(primary, "resource_cleanup_errors", ()), *errors)
            for error in errors:
                primary.add_note(f"Transaction cleanup also failed: {type(error).__name__}: {error}")

    @classmethod
    def _raise_outcome_errors(cls, errors):
        if errors:
            cls._annotate_cleanup_errors(errors[0], errors[1:])
            raise errors[0]

    @staticmethod
    def _attempt_callbacks(callbacks, errors):
        for callback in callbacks:
            try:
                callback()
            except BaseException as exc:
                errors.append(exc)

    def _check_resource_transaction(self):
        if self._resource_outcome_running:
            raise sqlite3.ProgrammingError("SQL is forbidden during critical resource outcomes")
        if self._critical_resource_outcomes and not self.in_transaction:
            raise sqlite3.ProgrammingError(
                "Pending critical resources require explicit rollback() or close() before more work"
            )

    class _ResourceCursor(sqlite3.Cursor):
        def execute(self, *args, **kwargs):
            self.connection._check_resource_transaction()
            return super().execute(*args, **kwargs)

        def executemany(self, *args, **kwargs):
            self.connection._check_resource_transaction()
            return super().executemany(*args, **kwargs)

        def executescript(self, *args, **kwargs):
            self.connection._check_resource_transaction()
            if self.connection._critical_resource_outcomes:
                raise sqlite3.ProgrammingError("executescript cannot end a critical resource transaction")
            return super().executescript(*args, **kwargs)

    def cursor(self, factory=None):
        self._check_resource_transaction()
        if factory is not None and self._critical_resource_outcomes:
            raise sqlite3.ProgrammingError("Custom cursors cannot bypass critical resource protection")
        return super().cursor(factory or self._ResourceCursor)

    def execute(self, *args, **kwargs):
        return self.cursor().execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        return self.cursor().executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        return self.cursor().executescript(*args, **kwargs)

    def _register_critical_resource_outcome(
        self, *, before_commit, after_commit, before_rollback, after_outcome,
    ):
        """Protected capability; return idempotent unregister for local recovery.

        after_outcome releases external ownership AFTER the native outcome and
        all critical callbacks, even when they fail. No services import belongs
        here: the connection knows only this callback protocol.
        """
        self._check_resource_transaction()
        if not self.in_transaction:
            raise sqlite3.ProgrammingError("Critical resources require an active outer transaction")
        if getattr(self, "autocommit", False) is True:
            raise sqlite3.ProgrammingError("Critical resources require effective native commit/rollback methods")
        callbacks = (before_commit, after_commit, before_rollback, after_outcome)
        if not all(callable(callback) for callback in callbacks):
            raise TypeError("Critical resource outcome callbacks must be callable")
        self._critical_resource_outcomes.append(callbacks)

        def unregister():
            if callbacks in self._critical_resource_outcomes:
                self._critical_resource_outcomes.remove(callbacks)

        return unregister

    def _native_commit(self):
        """Only the SQLite commit; outcome handling stays in commit()."""
        super().commit()

    def commit(self):
        self._check_resource_transaction()
        try:
            # A failed local undo must never become successful publication just
            # because its caller catches the error and commits unrelated work.
            errors = []
            self._resource_outcome_running = True
            try:
                self._attempt_callbacks((entry[0] for entry in self._critical_resource_outcomes), errors)
            finally:
                self._resource_outcome_running = False
            self._raise_outcome_errors(errors)
            self._native_commit()
        except BaseException as primary:
            try:
                self.rollback()
            except BaseException as cleanup:
                self._annotate_cleanup_errors(primary, [cleanup])
            raise
        # Irrevocable native success: cleanup errors cannot trigger undo.
        critical, self._critical_resource_outcomes = self._critical_resource_outcomes, []
        self._after_rollback = []
        callbacks, self._after_commit = tuple(self._after_commit), []
        errors = []
        self._resource_outcome_running = True
        try:
            self._attempt_callbacks((entry[1] for entry in critical), errors)
        finally:
            self._attempt_callbacks((entry[3] for entry in critical), errors)
            self._resource_outcome_running = False
        self._attempt_callbacks(callbacks, errors)
        self._raise_outcome_errors(errors)

    def rollback(self):
        if self._resource_outcome_running:
            raise sqlite3.ProgrammingError("Reentrant critical resource outcome")
        critical, self._critical_resource_outcomes = self._critical_resource_outcomes, []
        self._after_commit = []
        callbacks, self._after_rollback = tuple(self._after_rollback), []
        errors = []
        self._resource_outcome_running = True
        try:
            self._attempt_callbacks((entry[2] for entry in reversed(critical)), errors)
        finally:
            try:
                super().rollback()
            except BaseException as exc:
                errors.insert(0, exc)
            finally:
                self._attempt_callbacks((entry[3] for entry in critical), errors)
                self._resource_outcome_running = False
        # Ordinary notifications retain native post-unlock, reverse ordering.
        self._attempt_callbacks(reversed(callbacks), errors)
        self._raise_outcome_errors(errors)

    def close(self):
        if self._closed:
            return
        if self._resource_outcome_running:
            raise sqlite3.ProgrammingError("Reentrant critical resource outcome")
        try:
            if self.in_transaction or self._after_rollback or self._critical_resource_outcomes:
                self.rollback()
        finally:
            super().close()
            self._closed = True

    def __exit__(self, kind, value, traceback):
        # sqlite3's native context manager does not call Python overrides of
        # commit/rollback. Preserve its behavior while honoring resource hooks.
        if kind is not None:
            try:
                self.rollback()
            except BaseException as cleanup:
                self._annotate_cleanup_errors(value, [cleanup])
        else:
            try:
                self.commit()
            except BaseException as primary:
                # A swallowed engine auto-abort still needs context recovery.
                # Native success has already disarmed critical recovery, so a
                # post-commit cleanup failure can NEVER enter this branch.
                if self._critical_resource_outcomes:
                    try:
                        self.rollback()
                    except BaseException as cleanup:
                        self._annotate_cleanup_errors(primary, [cleanup])
                raise
        return False


def after_commit(conn, callback) -> None:
    """Run `callback` once `conn`'s current transaction commits; drop it on rollback.

    A connection `get_db()` did not open has no hook, so the callback runs now.
    """
    hooks = getattr(conn, "_after_commit", None)
    if hooks is None:
        callback()
    elif callback not in hooks:
        hooks.append(callback)


def after_rollback(conn, callback) -> None:
    """Register critical recovery until rollback/close, never an eager fallback.

    A plain sqlite3 connection cannot observe a future caller rollback. Refuse
    registration rather than incorrectly finalize a filesystem recovery journal.
    """
    hooks = getattr(conn, "_after_rollback", None)
    if hooks is None:
        raise ValueError("Rollback recovery requires a Shelf-managed connection")
    if callback not in hooks:
        hooks.append(callback)


def _install_live_views(conn) -> None:
    # The soft-delete read seam. Every read of items in app/ goes through this
    # view rather than the physical table (scripts/check_items_live.py is the
    # gate); writes keep hitting items.
    #
    # TEMP, not persistent: backups are taken with VACUUM INTO, which copies
    # the whole persistent schema, and the restore validator in
    # app/routers/settings.py refuses any uploaded database that contains a
    # view — so a persistent items_live would make every backup taken after it
    # shipped unrestorable through Shelf's own UI. SELECT *, not a column list,
    # so a later ALTER TABLE items needs no change here.
    #
    # The trade: a connection that bypasses get_db() has no view and fails with
    # "no such table: items_live". That is loud, which is the point. It is also
    # why this is not conditional on the table existing: a caller may open an
    # empty connection before initialization. A view's body is resolved at use,
    # not at creation. init_db itself deliberately bypasses request views until
    # its read-only lineage gate and schema transaction have completed.
    conn.execute(
        "CREATE TEMP VIEW IF NOT EXISTS items_live AS "
        "SELECT * FROM items WHERE deleted_at IS NULL"
    )
    # The same seam for physical copies. A copy is live only if it is not
    # trashed AND its item is not trashed, which is why this one joins the
    # items relation where items_live does not have to. That join is the
    # design decision: trashing an item then needs no write to its copies at
    # all, and the readers that never look at the item — Shelf Fill's
    # per-location totals, apply_copy_order, _append_copy_position — stop
    # counting a trashed item's copies with no per-site predicate. One choke
    # point, not twenty (G29, restated in G105).
    #
    # Same TEMP reasoning as above: a persistent view would ride into every
    # VACUUM INTO backup and make it unrestorable. `c.*` rather than a column
    # list, so a later ALTER TABLE item_copies needs no change here. Resolved
    # at use, not at creation, so this is safe before SCHEMA has run.
    conn.execute(
        "CREATE TEMP VIEW IF NOT EXISTS copies_live AS "
        "SELECT c.* FROM item_copies c JOIN items i ON i.id = c.item_id "
        "WHERE c.deleted_at IS NULL AND i.deleted_at IS NULL"
    )
    from app.services.catalogue_security import install_catalogue_views
    install_catalogue_views(conn)


@contextmanager
def get_db():
    conn = sqlite3.connect(str(DATABASE_PATH), factory=_Connection)
    conn.row_factory = sqlite3.Row
    try:
        # Every database must pass the read-only gate before a connection can
        # change its journal mode. init_db remains the explicit upgrade API.
        conn._schema_lineage_verified = inspect_schema_lineage(conn) == "integrated"
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _install_live_views(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
