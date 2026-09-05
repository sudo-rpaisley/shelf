import os
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

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

    monkeypatch.setattr("app.config.DATA_DIR", data_dir)
    monkeypatch.setattr("app.config.DATABASE_PATH", db_path)
    monkeypatch.setattr("app.config.COVERS_DIR", covers_dir)
    _redirect_stale_path_constants(monkeypatch, data_dir, db_path, covers_dir)

    from app.config import SECRET_ENV_VARS
    for env_name in SECRET_ENV_VARS.values():
        monkeypatch.delenv(env_name, raising=False)

    import app.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_cached_secret_key", None)

    import app.crypto as crypto_mod
    monkeypatch.setattr(crypto_mod, "_cached_encryption_key", None)

    import app.nav as nav_mod
    monkeypatch.setattr(nav_mod, "_cached_settings", None)

    import app.currency as currency_mod
    monkeypatch.setattr(currency_mod, "_cached_currency", None)

    import app.services.igdb as igdb_mod
    monkeypatch.setattr(igdb_mod, "_token_cache", {})

    import app.services.outbound as outbound_mod
    outbound_mod.reset()

    import app.services.cover_queue as cover_queue_mod
    cover_queue_mod.reset()

    from app.database import init_db
    init_db()

    # Do not add harness-only SQLite triggers here. Shelf intentionally rejects
    # trigger-bearing backup databases during restore, and the test database
    # should exercise that exact production security rule.

    from app.auth import get_secret_key
    get_secret_key()


@pytest.fixture
def db():
    """Yield a database connection for direct queries in tests."""
    from app.database import get_db
    with get_db() as conn:
        yield conn


_TEST_CSRF_TOKEN = "test-csrf-token-fixed"


@pytest.fixture
def client(monkeypatch):
    """FastAPI TestClient with rate limiting disabled and CSRF pre-seeded."""
    monkeypatch.setenv("SHELF_DISABLE_RATE_LIMIT", "1")
    monkeypatch.setenv("SHELF_DISABLE_COVER_ENRICH", "1")  # no network from import tests
    from app.main import app
    c = TestClient(app, base_url="https://testserver")
    c.cookies.set("csrf_token", _TEST_CSRF_TOKEN)
    c.headers.update({"X-CSRF-Token": _TEST_CSRF_TOKEN})
    return c


def _create_user(username, password, display_name, role):
    """Create a test user with legacy Main Library access by default.

    The general suite predates first-class libraries and models an upgraded
    single-library installation. Dedicated permission tests create raw users
    when they need to exercise the no-membership case.
    """
    from app.auth import hash_password
    from app.database import get_db
    from app.services import libraries

    with get_db() as conn:
        conn.execute(
            "INSERT INTO users (username, password, display_name, role) VALUES (?, ?, ?, ?)",
            (username, hash_password(password), display_name, role),
        )
        row = conn.execute(
            "SELECT id, username, role, display_name FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        data = dict(row)
        if role in ("viewer", "editor"):
            libraries.set_membership(
                conn,
                libraries.DEFAULT_LIBRARY_ID,
                data["id"],
                role,
            )
        return data


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
    return _create_user("editor", "password123", "Editor User", "editor")


@pytest.fixture
def viewer_user():
    """Create a viewer user (committed) and return their info dict."""
    return _create_user("viewer", "password123", "Viewer User", "viewer")


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


def _insert_item(
    db,
    title="Test Book",
    isbn="9780000000001",
    media_type="book",
    _library_id=1,
    **kwargs,
):
    """Insert a test item and return its ID.

    General tests model an upgraded single-library installation, so new fixture
    items are assigned to Main Library by default. Pass ``_library_id=None`` in
    a permission test when an intentionally unmapped item is required.

    Legacy migration tests construct databases from before first-class
    libraries existed. In those databases the default library row is absent;
    the fixture must stay neutral rather than creating modern access state in
    the middle of the historical migration under test.

    Tests written before per-user state expressed activity through the legacy
    ``items`` columns. When such a fixture is created after a test user already
    exists, mirror those explicit personal-looking values into that user's
    state. This models migrations 55-56's snapshot semantics without teaching
    production code to make newly-created users inherit shared state.
    """
    fields = {"title": title, "isbn": isbn, "media_type": media_type, "source": "test"}
    fields.update(kwargs)
    cols = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    cursor = db.execute(f"INSERT INTO items ({cols}) VALUES ({placeholders})", list(fields.values()))
    item_id = cursor.lastrowid

    if _library_id is not None:
        try:
            library_exists = db.execute(
                "SELECT 1 FROM libraries WHERE id = ?",
                (int(_library_id),),
            ).fetchone()
        except sqlite3.OperationalError:
            library_exists = None
        if library_exists:
            from app.services import libraries
            libraries.assign_item(db, item_id, int(_library_id))

    has_personal_fixture_state = (
        fields.get("reading_status") is not None
        or fields.get("date_started") is not None
        or fields.get("date_finished") is not None
        or fields.get("owned") == 0
    )
    if has_personal_fixture_state:
        from app.services import user_state

        user_state.ensure_schema(db)
        users = db.execute("SELECT id FROM users").fetchall()
        for row in users:
            changes = {}
            if fields.get("reading_status") is not None:
                changes["reading_status"] = fields.get("reading_status")
            if fields.get("date_started") is not None:
                changes["date_started"] = fields.get("date_started")
            if fields.get("date_finished") is not None:
                changes["date_finished"] = fields.get("date_finished")
            if fields.get("owned") == 0:
                changes["wishlist"] = 1
            if changes:
                user_state.save_state(db, row["id"], item_id, **changes)

    return item_id


def _insert_borrower(db, name="Test Borrower"):
    """Insert a test borrower and return its ID."""
    cursor = db.execute("INSERT INTO borrowers (name) VALUES (?)", (name,))
    return cursor.lastrowid


def _insert_location(db, name="Test Location"):
    """Insert a test location and return its ID."""
    cursor = db.execute("INSERT INTO locations (name) VALUES (?)", (name,))
    return cursor.lastrowid