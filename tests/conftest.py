import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _redirect_stale_path_constants(monkeypatch, data_dir, db_path, covers_dir):
    """Point every already-imported app module's copy of a config path at tmp.

    `from app.config import COVERS_DIR` freezes the value at import time, so
    patching app.config alone leaves those modules writing to the real /data
    — app.services.covers did exactly that, and a manual add with an ISBN
    tried to mkdir /data/covers from the unit suite.

    The bindings are discovered rather than listed so that a module gaining
    one later is covered without anyone remembering to update this file. Any
    app-module attribute with one of these three names is that config
    constant by construction; the Path check keeps an unrelated same-named
    value (a string, a mock) from being clobbered.
    """
    targets = {"DATA_DIR": data_dir, "DATABASE_PATH": db_path, "COVERS_DIR": covers_dir}
    for name, module in list(sys.modules.items()):
        if module is None or not (name == "app" or name.startswith("app.")):
            continue
        for attr, value in targets.items():
            current = getattr(module, attr, None)
            if isinstance(current, Path) and current != value:
                monkeypatch.setattr(module, attr, value, raising=False)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Redirect all DB and filesystem operations to a temp directory."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    covers_dir = data_dir / "covers"
    covers_dir.mkdir()
    db_path = data_dir / "shelf.db"

    # app.config is the source of truth: modules imported *after* this point
    # read these values, and so does anything that resolves them at call time.
    monkeypatch.setattr("app.config.DATA_DIR", data_dir)
    monkeypatch.setattr("app.config.DATABASE_PATH", db_path)
    monkeypatch.setattr("app.config.COVERS_DIR", covers_dir)

    # Modules already imported that did `from app.config import COVERS_DIR`
    # bound the real /data path at import time and never see the line above
    # (app.services.archive documents the same trap), so redirect their
    # copies too.
    _redirect_stale_path_constants(monkeypatch, data_dir, db_path, covers_dir)

    # Clear every secret-credential env var so a developer's or CI runner's
    # shell can't leak a real credential into a settings-render test. Iterate
    # .values() — SECRET_ENV_VARS is settings-key -> ENV_NAME, and the two
    # collide in exactly the way that makes this easy to get backwards:
    # app/routers/pages.py iterates the *keys* (is_env_override takes a
    # settings key), this fixture needs the *values* (actual shell variable
    # names). `for env_name in SECRET_ENV_VARS` would yield 'tmdb_api_key'
    # etc. and clear nothing — a silent no-op.
    from app.config import SECRET_ENV_VARS
    for env_name in SECRET_ENV_VARS.values():
        monkeypatch.delenv(env_name, raising=False)

    # A developer shell that exports the E2E stub URL must not reach the unit suite.
    monkeypatch.delenv("SHELF_UPC_LOOKUP_URL", raising=False)
    monkeypatch.delenv("SHELF_ISSN_PORTAL_URL", raising=False)
    # Nor one that turns the translated UI on: tests opt in (tests/test_i18n.py).
    monkeypatch.delenv("SHELF_I18N", raising=False)

    # Reset cached secret key so each test gets a fresh one
    import app.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_cached_secret_key", None)

    # Reset cached encryption key — each test's key file lives in its own tmp dir
    import app.crypto as crypto_mod
    monkeypatch.setattr(crypto_mod, "_cached_encryption_key", None)
    # decrypt_value warns once per setting key per process; without this reset
    # the first test to see an undecryptable value silences every later one.
    monkeypatch.setattr(crypto_mod, "_warned_undecryptable", set())

    # Reset the nav settings cache — it would otherwise carry one test's
    # integration config (and hidden-tab set) into the next test's nav.
    import app.nav as nav_mod
    monkeypatch.setattr(nav_mod, "_cached_settings", None)

    # Reset the feature-flag cache (G13) — one test's disabled feature would
    # otherwise stay disabled in the next test's routes, nav and jobs.
    import app.features as features_mod
    monkeypatch.setattr(features_mod, "_cached_flags", None)
    monkeypatch.setattr(features_mod, "_generation", 0)

    # Reset the currency cache — otherwise one test's display currency
    # leaks into the next test's money formatting.
    import app.currency as currency_mod
    monkeypatch.setattr(currency_mod, "_cached_currency", None)

    # Reset the item_copies column cache. It holds PRAGMA table_info output,
    # which is the same for every test database, so nothing leaks today — but
    # G13's rule is unconditional for a module cache read at request time, and
    # a schema-by-hand test would otherwise inherit the previous shape.
    import app.services.item_copies as item_copies_mod
    item_copies_mod.reset_column_cache()

    # Reset the Trash expired-count cache — one test's trashed rows and
    # retention setting would otherwise answer the next test's count (G13).
    import app.services.trash as trash_mod
    monkeypatch.setattr(trash_mod, "_cache", None)

    # Reset the LibraryThing related-ISBN cache (G13) — it is keyed by ISBN
    # and reset only when the token changes, so two tests using the same
    # token would share one test's cached answers.
    import app.services.librarything as librarything_mod
    librarything_mod.reset_cache()

    # Reset the IGDB token cache — otherwise one test's cached OAuth token
    # leaks into the next test's credential pair.
    import app.services.igdb as igdb_mod
    monkeypatch.setattr(igdb_mod, "_token_cache", {})

    # Reset the per-host rate limiter registry. Its asyncio.Locks bind to the
    # loop they are first awaited on, and each test runs its own loop, so a
    # carried-over registry would hand a test a lock from a dead loop.
    import app.services.outbound as outbound_mod
    outbound_mod.reset()

    # Reset the cover enrichment queue. Its asyncio.Queue binds to the loop
    # it is created on, and the counters are process-global, so a carried-over
    # queue leaks depth and gave-up counts into unrelated tests.
    import app.services.cover_queue as cover_queue_mod
    cover_queue_mod.reset()

    # Initialize schema
    from app.database import init_db
    init_db()

    # Pre-seed and cache the secret key so get_secret_key() never opens a
    # second connection while a test's db fixture connection is already open.
    from app.auth import get_secret_key
    get_secret_key()


@pytest.fixture
def db():
    """Yield a database connection for direct queries in tests."""
    from app.database import get_db
    with get_db() as conn:
        yield conn


@pytest.fixture
def established_restore_keys():
    """Real startup keys exist BEFORE a restore, including a refused upload.

    TestClient does not enter the application's lifespan in these unit tests.
    Restore must not create keys as a side effect; establish that native serving
    precondition explicitly instead of weakening the staged-key boundary.
    """
    from app.auth import get_secret_key
    from app.crypto import get_encryption_key

    return get_encryption_key(), get_secret_key()


_TEST_CSRF_TOKEN = "test-csrf-token-fixed"


@pytest.fixture
def client(monkeypatch):
    """FastAPI TestClient with rate limiting disabled and CSRF pre-seeded."""
    monkeypatch.setenv("SHELF_DISABLE_RATE_LIMIT", "1")
    monkeypatch.setenv("SHELF_DISABLE_COVER_ENRICH", "1")  # no network from import tests
    monkeypatch.setenv("SHELF_DISABLE_PRICE_ALERTS", "1")  # no ISBNdb pass from the lifespan loop
    monkeypatch.setenv("SHELF_DISABLE_RELEASE_RADAR", "1")  # no Hardcover pass from the lifespan loop
    from app.main import app
    c = TestClient(app, base_url="https://testserver")
    c.cookies.set("csrf_token", _TEST_CSRF_TOKEN)
    c.headers.update({"X-CSRF-Token": _TEST_CSRF_TOKEN})
    return c


def _create_user(username, password, display_name, role, *, library_role=None):
    """Create a committed user; membership is an explicit fixture precondition.

    Dedicated ACL tests and later-user tests must be able to create a genuine
    nonmember. Only the named general-suite fixtures below opt into legacy
    single-library access. No personal state is copied when a user is created.
    """
    from app.auth import hash_password
    from app.database import get_db
    with get_db() as conn:
        conn.execute(
            "INSERT INTO users (username, password, display_name, role) VALUES (?, ?, ?, ?)",
            (username, hash_password(password), display_name, role),
        )
        row = conn.execute(
            "SELECT id, username, role, display_name FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        user = dict(row)
        if library_role is not None:
            from app.services import libraries

            libraries.set_membership(conn, libraries.DEFAULT_LIBRARY_ID,
                                     user["id"], library_role)
        return user


@pytest.fixture
def admin_user():
    """Create an admin user (committed) and return their info dict."""
    return _create_user("admin", "password123", "Admin User", "admin")


@pytest.fixture
def admin_client(client, admin_user):
    """TestClient with a valid admin session cookie."""
    from app.auth import create_token
    token = create_token(admin_user["id"], admin_user["username"], admin_user["role"], admin_user["display_name"])
    client.cookies.set("access_token", token)
    return client


@pytest.fixture
def editor_user():
    """Create an editor user (committed) and return their info dict."""
    return _create_user("editor", "password123", "Editor User", "editor",
                        library_role="editor")


@pytest.fixture
def viewer_user():
    """Create a viewer user (committed) and return their info dict."""
    return _create_user("viewer", "password123", "Viewer User", "viewer",
                        library_role="viewer")


@pytest.fixture
def editor_client(client, editor_user):
    """TestClient with a valid editor session cookie."""
    from app.auth import create_token
    token = create_token(editor_user["id"], editor_user["username"], editor_user["role"], editor_user["display_name"])
    client.cookies.set("access_token", token)
    return client


@pytest.fixture
def viewer_client(client, viewer_user):
    """TestClient with a valid viewer session cookie."""
    from app.auth import create_token
    token = create_token(viewer_user["id"], viewer_user["username"], viewer_user["role"], viewer_user["display_name"])
    client.cookies.set("access_token", token)
    return client


def has_bare_attribute(html: str, name: str) -> bool:
    """Is `name` an attribute in its own right, and not merely the prefix of a
    longer one?

    `assert "data-manual-add" in html` is satisfied by `data-manual-add-title`
    alone. Removing the bare hook that the click listener actually selects on
    therefore left every claiming test green while the button went inert —
    caught by mutation, not by the suite. Match the name followed by an
    attribute boundary (whitespace, `=`, `>` or `/`), never by `-` or another
    word character.
    """
    return re.search(re.escape(name) + r"(?![-\w])", html) is not None


def _insert_item(db, title="Test Book", isbn="9780000000026", media_type="book",
                 wishlisted=False, _library_id=1, personal_user_id=None,
                 personal_state=None, **kwargs):
    """Insert a test item and return its ID.

    `wishlisted=True` adds the row to the wishlist after the INSERT; it is a
    keyword of this helper, not a column, so it never reaches the statement.
    `owned=0` alone is a legal "neither" state — pass `wishlisted=True`
    explicitly for a shared acquisition wishlist seed. A personal wish is
    instead `personal_state={"wishlist": 1}, personal_user_id=<id>` and may
    coexist with ownership. Legacy reading columns are mirrored only for the
    explicitly named actor, never for all existing or subsequently created users.

    `_library_id=None` models an intentionally unmapped item. A historical
    database without the libraries table/row stays historical: this helper
    never installs the modern schema or replays security/personal seeds.
    """
    if personal_state is not None and personal_user_id is None:
        raise ValueError("a personal fixture state requires its explicit user id")
    fields = {"title": title, "isbn": isbn, "media_type": media_type, "source": "test"}
    fields.update(kwargs)
    cols = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    cursor = db.execute(f"INSERT INTO items ({cols}) VALUES ({placeholders})", list(fields.values()))
    item_id = cursor.lastrowid
    if _library_id is not None:
        has_libraries = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'libraries'"
        ).fetchone()
        if has_libraries and db.execute(
            "SELECT 1 FROM libraries WHERE id = ?", (_library_id,)
        ).fetchone():
            # Fixture linkage, not a production request/initializer. Captured
            # fork-era databases have libraries but no bridge provenance yet;
            # calling assign_item/ensure_library_schema would falsely require
            # today's initialized schema halfway through a cutoff fixture.
            db.execute("INSERT INTO library_items (library_id, item_id) VALUES (?, ?)",
                       (_library_id, item_id))
    if wishlisted:
        from app.services import lists

        lists.add(db, lists.WISHLIST, item_id)
    if personal_user_id is not None:
        changes = {key: fields[key] for key in
                   ("reading_status", "date_started", "date_finished") if key in fields}
        changes.update(personal_state or {})
        if changes:
            _seed_personal_state(db, personal_user_id, item_id, **changes)
    return item_id


def _seed_personal_state(db, user_id, item_id, **changes):
    """Explicit named-actor fixture seed; no DDL, authority grants or reseeding.

    Direct fixture SQL works both before and after the bridge while retaining
    real FK/CHECK constraints. Production save_state remains under test at the
    request boundary, rather than being invoked to construct its own oracle.
    """
    allowed = {"reading_status", "date_started", "date_finished", "rating",
               "wishlist", "favourite", "personal_notes", "progress_value",
               "progress_total", "progress_unit"}
    if user_id is None or not changes or set(changes) - allowed:
        raise ValueError("personal fixture seed requires an explicit actor and known fields")
    columns = ", ".join(changes)
    placeholders = ", ".join("?" for _ in changes)
    updates = ", ".join(f"{key} = excluded.{key}" for key in changes)
    db.execute(f"INSERT INTO user_item_state (user_id, item_id, {columns}) "
               f"VALUES (?, ?, {placeholders}) ON CONFLICT(user_id, item_id) DO UPDATE SET {updates}",
               (user_id, item_id, *changes.values()))


def _insert_personal_history(db, user_id, item_id, *, status="read",
                             date_started=None, date_finished=None, notes=None):
    """One actor's history; deliberately never mirrored into any other user."""
    if user_id is None:
        raise ValueError("personal history requires an explicit actor")
    return db.execute(
        "INSERT INTO user_reading_log (user_id, item_id, status, date_started, date_finished, notes) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, item_id, status, date_started, date_finished, notes),
    ).lastrowid


def _assert_ownership_partition(db):
    """Assert no item is both owned and a wishlist member.

    `owned = 1` must never coexist with wishlist membership — the one
    coupling that survives the ownership/wishlist split. `owned = 0` with no
    membership is a legal "neither" state and this check does not touch it.
    Raw-SQL test seeds bypass the app's write funnel, so this is a fixture
    sanity check, not app-code coverage.
    """
    from app.services.lists import WISHLISTED_SQL

    rows = db.execute(
        f"SELECT i.id, i.title, i.owned FROM items i "
        f"WHERE i.owned = 1 AND {WISHLISTED_SQL}"
    ).fetchall()
    assert not rows, (
        "owned items found on the wishlist: "
        + ", ".join(f"#{r['id']} {r['title']!r} (owned={r['owned']})" for r in rows)
    )


def _insert_borrower(db, name="Test Borrower"):
    """Insert a test borrower and return their ID."""
    cursor = db.execute("INSERT INTO borrowers (name) VALUES (?)", (name,))
    return cursor.lastrowid


def _insert_location(db, name="Test Location"):
    """Insert a legacy flat location, NOT a location-node/copy location."""
    cursor = db.execute("INSERT INTO locations (name) VALUES (?)", (name,))
    return cursor.lastrowid


def _insert_node_location(db, name="Test Location", *, parent_id=None):
    """Seed explicit node/flat linkage; callers choose each domain by name.

    Insert an unrelated node first so fixture correctness cannot accidentally
    depend on equal integer IDs in the two physical location domains.
    """
    from app.services.location_tree import legacy_location_to_node

    if parent_id is not None:
        return db.execute(
            "INSERT INTO location_nodes (parent_id, name) VALUES (?, ?)",
            (parent_id, name),
        ).lastrowid
    if not db.execute("SELECT 1 FROM location_nodes").fetchone():
        db.execute("INSERT INTO location_nodes (name) VALUES ('Unlinked node control')")
    flat_id = _insert_location(db, name)
    # A preceding raw flat seed may have caught the two sequences up again.
    # Preserve the unequal-ID control in mixed legacy/physical fixture setups,
    # too; never repair the test by treating either integer as both domains.
    next_node = db.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM location_nodes").fetchone()[0]
    if next_node == flat_id:
        db.execute("INSERT INTO location_nodes (name) VALUES (?)",
                   (f"Unlinked node control before flat {flat_id}",))
    node_id = legacy_location_to_node(db, flat_id, create=True)
    assert node_id is not None
    assert node_id != flat_id, "the fixture must distinguish location ID domains"
    return node_id


def _legacy_location(db, node_id):
    """The flat seam corresponding to a node, resolved rather than coalesced."""
    from app.services.location_tree import node_to_legacy_location

    return node_to_legacy_location(db, node_id)


def _insert_physical_item(db, *, location_id=None, **kwargs):
    """Physical-suite fixture: location_id is explicitly a NODE-domain ID.

    Only the item fixture's legacy seam is converted. Copy/audit inputs retain
    the original node ID, and the deliberately unequal fixture IDs expose any
    application code that coalesces the two domains.
    """
    return _insert_item(db, location_id=_legacy_location(db, location_id), **kwargs)


#: Matches the ALTER statements in MIGRATIONS, so `bootstrap_sql_before` can
#: work out which columns a given cutoff has not reached yet.
_ALTER_COLUMN = re.compile(r"ALTER TABLE (\w+) ADD COLUMN (\w+)", re.I)


def bootstrap_sql_before(up_to: int) -> str:
    """`MIGRATION_TABLES` as it stood *before* migration ``up_to + 1`` (G98).

    A legacy-database fixture must be built from the bootstrap schema as it
    was before the migration under test — never from the current
    `MIGRATION_TABLES`. Running the current one first creates each table
    complete, so the numbered entry the fixture exists to exercise becomes a
    no-op and the test passes with that entry deleted.

    For a *table* the fixture simply omits the CREATE. For a *column* the trap
    is quieter and only opened up at migration 39. `_is_benign_migration_error`
    forgives `duplicate column name` for versions <= `_PRE_ATOMIC_MAX_VERSION`
    (21), and every column added to a MIGRATION_TABLES-managed table before
    that — 16-19 on `series_meta` — sat under the amnesty, so a fixture could
    run the current CREATE and the redundant ALTER was waved through. Migration
    39 (`tags.media_type`) is the first one above the line, and there the same
    fixture raises instead.

    So this strips every column added by a migration **above** `up_to`,
    derived from `MIGRATIONS` rather than listed by hand: migration 40 strips
    itself and no fixture has to be revisited.
    """
    # Immutable parent-era definitions, not today's schema that can pre-create
    # the very effect a cutoff fixture exists to exercise (D1/G98).
    from tests.lineage import UPSTREAM_MIGRATION_TABLES as MIGRATION_TABLES
    from tests.lineage import UPSTREAM_MIGRATIONS as MIGRATIONS

    sql = MIGRATION_TABLES
    for version, _description, statement in MIGRATIONS:
        if version <= up_to:
            continue
        match = _ALTER_COLUMN.search(statement or "")
        if not match:
            continue
        table, column = match.group(1), match.group(2)
        # Only touch the column inside that table's own CREATE, so a column
        # name two tables share cannot be stripped from the wrong one.
        create = re.search(
            rf"(CREATE TABLE IF NOT EXISTS {table} \()(.*?)(\n\);)",
            sql,
            flags=re.S,
        )
        if not create:
            continue  # the table is not one MIGRATION_TABLES creates
        # A column the CREATE never carried needs no stripping: that is the
        # MIGRATIONS-only pattern (31's item_copies.position_order, 37 and
        # 38's deleted_at), where a copy in the CREATE would make the ALTER
        # raise on the fresh path instead. Only a column that IS in the body
        # has to come out.
        if not re.search(rf"\n\s*{column}\s", create.group(2)):
            continue
        # Two shapes, because the last column in a CREATE carries no trailing
        # comma: strip `\n  col ...,` normally, and `,\n  col ...` when the
        # column is last. Missing the second silently leaves the column in
        # place and the fixture stops being a legacy one at all.
        body, hits = re.subn(
            rf"\n\s*{column}\s+[^,\n]+,", "", create.group(2), count=1
        )
        if not hits:
            body, hits = re.subn(
                rf",\n\s*{column}\s+[^,\n]+(?=\n|$)", "", create.group(2), count=1
            )
        assert hits, (
            f"bootstrap_sql_before({up_to}) found {table}.{column} in its "
            f"CREATE but could not strip it, so the fixture it builds would "
            f"already have a column migration {version} is supposed to add — "
            f"the test would be vacuously green (G98)."
        )
        sql = sql[: create.start(2)] + body + sql[create.end(2) :]
    return sql
