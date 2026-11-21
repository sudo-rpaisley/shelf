"""Phase4 real-stage/publication, metadata cascade and editor contracts.

Authoring only until the orchestrator freezes the candidate and opens gates.
Shared fixture records the REAL stage; it never substitutes a publisher.
"""

import random
import struct
import zlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


def _png_bytes(seed=0):
    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload)))

    rng = random.Random(4 + seed)
    pixels = b"".join(b"\x00" + rng.randbytes(32 * 3) for _ in range(48))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 48, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


PNG_BYTES = _png_bytes()


def session_principal(db, user):
    row = dict(db.execute(
        "SELECT id, username, role, display_name, token_version, language FROM users WHERE id = ?",
        (user["id"],)).fetchone())
    row["display_name"] = row["display_name"] or row["username"]
    return row


@pytest.fixture
def cover_stages(monkeypatch):
    import httpx
    from app.routers import items_common
    from app.services import catalogue_security, libraries

    real_stage = items_common.CoverStage

    class Capture:
        def __init__(self):
            self.created = []
            self.stores = []
            self.clients = []

        def latest(self, item_id):
            stage = self.created[-1]
            assert stage.item_id == item_id
            assert isinstance(stage, real_stage)
            assert stage.download_id < 0 and stage.download_id != item_id
            assert len({s.download_id for s in self.created}) == len(self.created)
            return stage

        def write(self, item_id, download_id):
            stage = self.latest(item_id)
            assert download_id == stage.download_id
            stage.path.parent.mkdir(parents=True, exist_ok=True)
            stage.path.write_bytes(PNG_BYTES)
            return f"covers/{stage.download_id}.jpg"

        def assert_clean(self):
            for stage in self.created:
                for suffix in stage.SUFFIXES:
                    assert not stage.path_for(suffix).exists()

        def assert_published(self, db, item_id, *, principal, kwargs):
            stage, path, recorded_kwargs, actor, library_id, result, original = self.stores[-1]
            assert stage is self.latest(item_id)
            assert path == f"covers/{stage.download_id}.jpg"
            assert recorded_kwargs == kwargs
            assert actor == principal
            assert library_id == 1
            assert result == f"covers/{item_id}.jpg"
            row = dict(db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone())
            assert row["cover_path"] == result
            assert (row["cover_edition_isbn"], row["cover_edition_language"]) == (kwargs.get("edition") or (None, None))
            for field in original.keys() - {"cover_path", "cover_edition_isbn", "cover_edition_language", "updated_at"}:
                assert row[field] == original[field], field
            assert libraries.item_library_id(db, item_id) == library_id
            assert stage.path.with_name(f"{item_id}.jpg").read_bytes() == PNG_BYTES
            self.assert_clean()

    capture = Capture()
    real_enter = httpx.AsyncClient.__aenter__
    async def enter(client):
        result = await real_enter(client)
        capture.clients.append(result)
        return result
    monkeypatch.setattr(httpx.AsyncClient, "__aenter__", enter)

    class CapturedStage(real_stage):
        def __init__(self, item_id):
            super().__init__(item_id)
            capture.created.append(self)

        def store(self, db, cover_path, **kwargs):
            actor = catalogue_security.current_actor()
            actor = dict(actor) if actor is not None else None
            library_id = libraries.item_library_id(db, self.item_id)
            original = dict(db.execute("SELECT * FROM items WHERE id = ?", (self.item_id,)).fetchone())
            result = super().store(db, cover_path, **kwargs)
            capture.stores.append((self, cover_path, dict(kwargs), actor, library_id, result, original))
            return result

    monkeypatch.setattr(items_common, "CoverStage", CapturedStage)
    return capture


def _format_bytes(kind):
    import base64

    if kind == "png":
        return PNG_BYTES
    if kind == "jpeg":
        return (Path(__file__).parent / "fixtures/intake/eleven_books.jpg").read_bytes()
    if kind == "gif":
        return base64.b64decode("R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==") + b"\x00" * 100
    if kind == "webp":
        return base64.b64decode("UklGRiIAAABXRUJQVlA4IBYAAAAwAQCdASoBAAEADsD+JaQAA3AAAAAA") + b"\x00" * 100
    raise AssertionError(kind)


@pytest.mark.parametrize("kind,suffix,mime", [
    ("jpeg", ".jpg", "image/jpeg"), ("png", ".png", "image/png"),
    ("gif", ".gif", "image/gif"), ("webp", ".webp", "image/webp"),
])
@pytest.mark.parametrize("declared", [False, True])
def test_canonical_staging_preserves_actual_format_and_original_bytes(kind, suffix, mime, declared):
    from app.services import covers

    content = _format_bytes(kind)
    unrelated = covers.COVERS_DIR / "user-artwork.jpg"
    unrelated.write_bytes(PNG_BYTES)
    with covers.CoverStage(41) as stage:
        assert stage.item_id == 41 and stage.download_id < 0
        relative = covers.stage_uploaded_cover(stage, content, content_type=mime if declared else None)
        assert relative == f"covers/{stage.download_id}{suffix}"
        assert stage.path_for(suffix).read_bytes() == content
        assert not (covers.COVERS_DIR / f"41{suffix}").exists()
        # A same-ID non-image file is NOT stage-owned and must not be globbed.
        not_owned = covers.COVERS_DIR / f"{stage.download_id}.txt"
        not_owned.write_bytes(b"not a cover resource")
    assert not stage.path_for(suffix).exists()
    assert unrelated.read_bytes() == PNG_BYTES
    assert not_owned.read_bytes() == b"not a cover resource"


@pytest.mark.parametrize("content,content_type", [
    (b"not an image" * 100, None), (b"RIFF" + b"\x00" * 200, "image/webp"),
    (PNG_BYTES[:99], None), (PNG_BYTES, "image/jpeg"), (PNG_BYTES, "text/html"),
])
def test_invalid_staging_has_no_file_effect(content, content_type):
    from app.services import covers

    before = {p.name: p.read_bytes() for p in covers.COVERS_DIR.iterdir()}
    with covers.CoverStage(42) as stage:
        assert covers.stage_uploaded_cover(stage, content, content_type=content_type) is None
    assert {p.name: p.read_bytes() for p in covers.COVERS_DIR.iterdir()} == before


def test_stage_allocation_never_claims_an_existing_negative_id_file(monkeypatch):
    from types import SimpleNamespace
    from app.services import covers

    existing = covers.COVERS_DIR / "-1.webp"
    existing.write_bytes(_format_bytes("webp"))
    values = iter([SimpleNamespace(int=0), SimpleNamespace(int=1)])
    monkeypatch.setattr(covers, "uuid4", lambda: next(values))
    with covers.CoverStage(43) as stage:
        assert stage.download_id == -2
        relative = covers.stage_uploaded_cover(stage, PNG_BYTES)
        assert relative == "covers/-2.png"
    assert list(covers.COVERS_DIR.iterdir()) == [existing]
    assert existing.read_bytes() == _format_bytes("webp")


def _journal_connection(path, *, managed=True):
    """Real SQLite outcome methods; minimal schema isolates resource ownership."""
    import sqlite3
    from app.database import _Connection

    conn = sqlite3.connect(path, factory=_Connection if managed else sqlite3.Connection)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        "CREATE TABLE parents(id INTEGER PRIMARY KEY); INSERT INTO parents VALUES (1);"
        "CREATE TABLE items(id INTEGER PRIMARY KEY, updated_at TEXT, cover_path TEXT, "
        "parent_id INTEGER REFERENCES parents(id) DEFERRABLE INITIALLY DEFERRED);"
        "INSERT INTO items VALUES(51, 'original', 'covers/original.jpg', 1);"
    )
    return conn


@pytest.mark.parametrize("kind,suffix", [("jpeg", ".jpg"), ("png", ".png"), ("gif", ".gif"), ("webp", ".webp")])
@pytest.mark.parametrize("outcome", ["commit", "rollback", "failed-commit", "close"])
def test_publication_journal_follows_the_real_outer_outcome(tmp_path, kind, suffix, outcome):
    import sqlite3
    from app.services import covers

    path = tmp_path / "cover-outcome.db"
    conn = _journal_connection(path)
    destination = covers.COVERS_DIR / f"51{suffix}"
    original = _png_bytes(seed=1)
    destination.write_bytes(original)
    content = _format_bytes(kind)
    calls = []

    def authorize(db):
        assert db is conn and db.in_transaction
        assert db.execute("SELECT parent_id FROM items WHERE id = 51").fetchone() == (1,)
        assert destination.read_bytes() == original
        calls.append("authorize")

    def store(db, relative):
        assert db is conn and db.in_transaction
        assert calls == ["authorize"]
        assert relative == f"covers/51{suffix}"
        calls.append("store")
        db.execute("UPDATE items SET cover_path = ? WHERE id = 51", (relative,))

    try:
        with covers.CoverStage(51) as stage:
            pending = covers.stage_uploaded_cover(stage, content)
            assert covers.publish_staged_cover(conn, stage, pending, authorize=authorize, store=store) == f"covers/51{suffix}"
        assert calls == ["authorize", "store"]
        assert destination.read_bytes() == content
        assert not stage.path_for(suffix).exists()
        if outcome == "failed-commit":
            conn.execute("UPDATE items SET parent_id = 999 WHERE id = 51")
            with pytest.raises(sqlite3.IntegrityError):
                conn.commit()
        else:
            getattr(conn, outcome)()
        assert destination.read_bytes() == (content if outcome == "commit" else original)
        with sqlite3.connect(path) as observed:
            assert observed.execute("SELECT cover_path FROM items WHERE id = 51").fetchone()[0] == (
                f"covers/51{suffix}" if outcome == "commit" else "covers/original.jpg")
        assert sorted(p.name for p in covers.COVERS_DIR.iterdir()) == [destination.name]
    finally:
        conn.close()


def test_publication_refuses_raw_connection_before_store_or_final_file_change(tmp_path):
    from app.services import covers
    from app.services.transaction_resources import TransactionResourceError

    conn = _journal_connection(tmp_path / "raw-cover.db", managed=False)
    destination = covers.COVERS_DIR / "51.png"
    destination.write_bytes(_png_bytes(seed=1))
    original = destination.read_bytes()
    stores = []
    try:
        with covers.CoverStage(51) as stage:
            pending = covers.stage_uploaded_cover(stage, PNG_BYTES)
            with pytest.raises(TransactionResourceError, match="commit AND rollback"):
                covers.publish_staged_cover(conn, stage, pending, authorize=lambda db: None,
                    store=lambda db, relative: stores.append(relative))
        assert stores == []
        assert destination.read_bytes() == original
        assert list(covers.COVERS_DIR.iterdir()) == [destination]
    finally:
        conn.close()


def test_multiple_publications_unwind_in_reverse_to_original_bytes(tmp_path):
    from app.services import covers

    conn = _journal_connection(tmp_path / "stacked-cover.db")
    destination = covers.COVERS_DIR / "51.png"
    original = _png_bytes(seed=1)
    destination.write_bytes(original)
    try:
        for seed in (2, 3):
            content = _png_bytes(seed)
            with covers.CoverStage(51) as stage:
                pending = covers.stage_uploaded_cover(stage, content)
                covers.publish_staged_cover(conn, stage, pending, authorize=lambda db: None,
                    store=lambda db, relative: db.execute("UPDATE items SET cover_path = ? WHERE id = 51", (relative,)))
            assert destination.read_bytes() == content
        conn.rollback()
        assert destination.read_bytes() == original
        assert list(covers.COVERS_DIR.iterdir()) == [destination]
    finally:
        conn.close()


def test_store_refusal_rolls_back_local_sql_and_file_before_unrelated_commit(tmp_path):
    from app.services import covers

    conn = _journal_connection(tmp_path / "local-refusal.db")
    destination = covers.COVERS_DIR / "51.png"
    original = _png_bytes(seed=1)
    destination.write_bytes(original)

    def refuse(db, relative):
        db.execute("UPDATE items SET cover_path = ? WHERE id = 51", (relative,))
        raise ValueError("domain writer refused")

    try:
        with covers.CoverStage(51) as stage:
            pending = covers.stage_uploaded_cover(stage, PNG_BYTES)
            with pytest.raises(ValueError, match="domain writer refused"):
                covers.publish_staged_cover(conn, stage, pending, authorize=lambda db: None, store=refuse)
        conn.execute("INSERT INTO parents VALUES (2)")
        conn.commit()
        assert conn.execute("SELECT cover_path FROM items WHERE id = 51").fetchone() == ("covers/original.jpg",)
        assert destination.read_bytes() == original
        assert list(covers.COVERS_DIR.iterdir()) == [destination]
    finally:
        conn.close()


def test_error_after_actual_atomic_replace_recovers_the_original_file_and_row(tmp_path, monkeypatch):
    from app.services import covers

    conn = _journal_connection(tmp_path / "replace-error.db")
    destination = covers.COVERS_DIR / "51.png"
    original = _png_bytes(seed=1)
    destination.write_bytes(original)
    try:
        with covers.CoverStage(51) as stage:
            pending = covers.stage_uploaded_cover(stage, PNG_BYTES)
            source = stage.path_for(".png")
            real_replace = type(source).replace
            def replace(path, target):
                result = real_replace(path, target)
                if path == source:
                    raise OSError("observed after real atomic replace")
                return result
            monkeypatch.setattr(type(source), "replace", replace)
            with pytest.raises(OSError, match="after real atomic replace"):
                covers.publish_staged_cover(conn, stage, pending, authorize=lambda db: None,
                    store=lambda db, relative: db.execute("UPDATE items SET cover_path = ? WHERE id = 51", (relative,)))
        conn.commit()
        assert conn.execute("SELECT cover_path FROM items WHERE id = 51").fetchone() == ("covers/original.jpg",)
        assert destination.read_bytes() == original
        assert list(covers.COVERS_DIR.iterdir()) == [destination]
    finally:
        conn.close()


@pytest.fixture
def install_actor():
    from app.services import catalogue_security

    tokens = []
    def install(principal):
        tokens.append(catalogue_security.set_actor(principal))
    yield install
    for token in reversed(tokens):
        catalogue_security.reset_actor(token)


@pytest.mark.parametrize("mutation", ["deleted", "reused-id", "username", "role", "token-version"])
@pytest.mark.parametrize("kind", ["jpeg", "webp"])
@pytest.mark.parametrize("request_actor", [False, True])
def test_captured_admin_identity_is_rechecked_before_same_stage_publication(db, mutation, kind, request_actor, install_actor):
    from fastapi import HTTPException
    from app.routers import items_common
    from app.services import covers, item_write
    from tests.conftest import _insert_item

    admin_id = db.execute("INSERT INTO users (username, password, role) VALUES ('captured-admin', 'not-a-login', 'admin')").lastrowid
    principal = session_principal(db, {"id": admin_id})
    item_id = _insert_item(db, title="Keep captured target", isbn=None, cover_path="covers/original.jpg")
    db.commit()
    if request_actor:
        install_actor(principal)
    with item_write._write_scope(db):
        bindings = items_common.authorize_item_targets(db, [item_id], user=principal)
    db.commit()
    before = tuple(db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone())
    original = covers.COVERS_DIR / "original.jpg"
    original.write_bytes(_png_bytes(seed=1))
    with items_common.CoverStage(item_id) as stage:
        pending = covers.stage_uploaded_cover(stage, _format_bytes(kind))
        if mutation in ("deleted", "reused-id"):
            db.execute("DELETE FROM users WHERE id = ?", (admin_id,))
            if mutation == "reused-id":
                db.execute("INSERT INTO users (id, username, password, role) VALUES (?, 'replacement-admin', 'not-a-login', 'admin')", (admin_id,))
        elif mutation == "username":
            db.execute("UPDATE users SET username = 'renamed-admin' WHERE id = ?", (admin_id,))
        elif mutation == "role":
            db.execute("UPDATE users SET role = 'viewer' WHERE id = ?", (admin_id,))
        else:
            db.execute("UPDATE users SET token_version = token_version + 1 WHERE id = ?", (admin_id,))
        db.commit()
        with pytest.raises(HTTPException) as refusal:
            stage.store(db, pending, user=principal, library_ids=bindings)
        assert refusal.value.status_code == 403
    assert tuple(db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()) == before
    assert db.execute("SELECT library_id FROM library_items WHERE item_id = ?", (item_id,)).fetchone()[0] == 1
    assert {p.name: p.read_bytes() for p in covers.COVERS_DIR.iterdir()} == {"original.jpg": _png_bytes(seed=1)}


@pytest.mark.parametrize("mutation", ["deleted", "username", "role", "token-version"])
def test_real_http_cover_stage_retains_its_principal_across_provider_wait(
    admin_client, admin_user, db, monkeypatch, cover_stages, mutation
):
    from app.database import get_db
    from app.services import covers
    from tests.conftest import _insert_item

    item_id = _insert_item(db, title="Keep HTTP cover metadata", isbn=None, cover_path="covers/original.png")
    db.commit()
    before = tuple(db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone())
    original = covers.COVERS_DIR / "original.png"
    original.write_bytes(_png_bytes(seed=1))

    async def download(download_id, url, client):
        pending = cover_stages.write(item_id, download_id)
        with get_db() as rival:
            if mutation == "deleted":
                rival.execute("DELETE FROM users WHERE id = ?", (admin_user["id"],))
            elif mutation == "username":
                rival.execute("UPDATE users SET username = 'renamed-http-admin' WHERE id = ?", (admin_user["id"],))
            elif mutation == "role":
                rival.execute("UPDATE users SET role = 'viewer' WHERE id = ?", (admin_user["id"],))
            else:
                rival.execute("UPDATE users SET token_version = token_version + 1 WHERE id = ?", (admin_user["id"],))
        return pending

    monkeypatch.setattr(covers, "_download_to_item", download)
    response = admin_client.post(f"/api/items/{item_id}/cover-select",
        data={"url": "https://covers.openlibrary.org/b/id/400004-L.jpg"})
    assert response.status_code == 403
    assert tuple(db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()) == before
    assert cover_stages.stores == []
    cover_stages.assert_clean()
    assert {p.name: p.read_bytes() for p in covers.COVERS_DIR.iterdir()} == {"original.png": _png_bytes(seed=1)}


@pytest.mark.parametrize("mutation", ["unchanged", "role", "token-version"])
def test_canonical_checkpoint_revalidates_actual_http_actor_when_user_is_omitted(
    admin_client, admin_user, db, monkeypatch, cover_stages, mutation
):
    from app.database import get_db
    from app.routers import items_common
    from app.services import covers, item_write
    from tests.conftest import _insert_item

    target = _insert_item(db, title="Actual implicit HTTP actor", isbn=None, cover_path="covers/original.png")
    db.commit()
    before = tuple(db.execute("SELECT * FROM items WHERE id = ?", (target,)).fetchone())
    original = covers.COVERS_DIR / "original.png"
    original.write_bytes(_png_bytes(seed=1))
    entered, completed = [], []

    async def download(download_id, url, client):
        pending = cover_stages.write(target, download_id)
        with get_db() as rival:
            if mutation == "role":
                rival.execute("UPDATE users SET role = 'viewer' WHERE id = ?", (admin_user["id"],))
            elif mutation == "token-version":
                rival.execute("UPDATE users SET token_version = token_version + 1 WHERE id = ?", (admin_user["id"],))
        with get_db() as conn, item_write._write_scope(conn):
            entered.append(target)
            # The actual checkpoint call intentionally omits user. Checking
            # only a later CoverStage.store would miss this central defect.
            completed.append(items_common.authorize_item_targets(conn, [target], library_ids={target: 1}))
        return pending

    monkeypatch.setattr(covers, "_download_to_item", download)
    response = admin_client.post(f"/api/items/{target}/cover-select",
        data={"url": "https://covers.openlibrary.org/b/id/400004-L.jpg"})
    assert entered == [target]
    if mutation == "unchanged":
        assert response.status_code == 200 and response.headers["HX-Redirect"] == f"/item/{target}"
        assert completed == [{target: 1}]
        cover_stages.assert_published(db, target, principal=session_principal(db, admin_user),
            kwargs={"edition": None, "library_ids": {target: 1}})
    else:
        assert response.status_code == 403
        assert completed == [], "the implicit-actor checkpoint itself must refuse, not only a later store"
        assert tuple(db.execute("SELECT * FROM items WHERE id = ?", (target,)).fetchone()) == before
        assert cover_stages.stores == []
        assert {p.name: p.read_bytes() for p in covers.COVERS_DIR.iterdir()} == {"original.png": _png_bytes(seed=1)}
    cover_stages.assert_clean()


def test_expired_copied_http_actor_is_not_downgraded_to_trusted_service(db, admin_user):
    from contextvars import copy_context
    from app.routers import items_common
    from app.services import catalogue_security, item_write
    from tests.conftest import _insert_item

    target = _insert_item(db, title="Expired request scope target", isbn=None)
    db.commit()
    token = catalogue_security.set_actor(session_principal(db, admin_user))
    copied = copy_context()
    catalogue_security.reset_actor(token)
    with item_write._write_scope(db), pytest.raises(RuntimeError, match="Catalogue request scope has ended"):
        copied.run(items_common.authorize_item_targets, db, [target])


def test_only_absent_actor_and_absent_explicit_user_retain_trusted_service_context(db):
    from app.routers import items_common
    from app.services import catalogue_security, item_write
    from tests.conftest import _insert_item

    assert catalogue_security.current_actor() is None
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    target = _insert_item(db, title="Trusted non-HTTP target", isbn=None)
    db.commit()
    with item_write._write_scope(db):
        assert items_common.authorize_item_targets(db, [target], user=None, library_ids={target: 1}) == {target: 1}


@pytest.mark.parametrize("mode", ["explicit-cap", "actor-cap", "different-account"])
def test_explicit_account_never_replaces_the_request_principal_or_bearer_cap(db, install_actor, mode):
    from fastapi import HTTPException
    from app.routers import items_common
    from app.services import api_tokens, item_write
    from tests.conftest import _insert_item

    admin_id = db.execute("INSERT INTO users (username, password, role) VALUES ('cap-admin', 'not-a-login', 'admin')").lastrowid
    account = session_principal(db, {"id": admin_id})
    token = api_tokens.create(db, admin_id, "phase4 cover capability")
    db.commit()
    capped = api_tokens.resolve(token["token"])
    assert capped["id"] == admin_id and capped["role"] == "viewer" and capped["account_role"] == "admin"
    target = _insert_item(db, title="Bearer no-write target", isbn=None)
    db.commit()
    if mode == "actor-cap":
        install_actor(capped)
        explicit = account
    elif mode == "different-account":
        other_id = db.execute("INSERT INTO users (username, password, role) VALUES ('different-admin', 'not-a-login', 'admin')").lastrowid
        install_actor(session_principal(db, {"id": other_id}))
        explicit = account
    else:
        explicit = capped
    before = tuple(db.execute("SELECT * FROM items WHERE id = ?", (target,)).fetchone())
    with item_write._write_scope(db), pytest.raises(HTTPException) as refusal:
        items_common.authorize_item_targets(db, [target], user=explicit, library_ids={target: 1})
    assert refusal.value.status_code == 403
    assert tuple(db.execute("SELECT * FROM items WHERE id = ?", (target,)).fetchone()) == before


def test_real_internal_explicit_account_without_session_version_keeps_compatibility(db):
    from app.routers import items_common
    from app.services import covers
    from tests.conftest import _insert_item

    admin_id = db.execute("INSERT INTO users (username, password, role) VALUES ('internal-admin', 'not-a-login', 'admin')").lastrowid
    target = _insert_item(db, title="Internal exact target", isbn=None)
    db.commit()
    principal = {"id": admin_id, "role": "admin"}
    with items_common.CoverStage(target) as stage:
        pending = covers.stage_uploaded_cover(stage, PNG_BYTES)
        result = stage.store(db, pending, user=principal, library_ids={target: 1})
    db.commit()
    assert result == f"covers/{target}.png"
    assert (covers.COVERS_DIR / f"{target}.png").read_bytes() == PNG_BYTES
    assert not stage.path_for(".png").exists()


async def test_metadata_delegator_is_used_and_keeps_exact_signature(monkeypatch):
    from app.routers import items_common
    from app.services import metadata_lookup, provider_result

    value = ({"title": "Exact metadata"}, "openlibrary", {}, provider_result.found("isbn-cascade", {}))
    lookup = AsyncMock(return_value=value)
    monkeypatch.setattr(metadata_lookup, "lookup_metadata", lookup)
    client = object()
    assert await items_common._lookup_metadata("9780441172719", "hc-test-token", client,
        google_api_key="google-test-key") is value
    lookup.assert_awaited_once_with("9780441172719", "hc-test-token", client, google_api_key="google-test-key")


@pytest.mark.parametrize("winner", ["national", "openlibrary", "hardcover", "google", "none"])
async def test_extracted_metadata_cascade_retains_order_patchability_and_typed_outcomes(monkeypatch, winner):
    from types import SimpleNamespace
    from app.routers import items_common
    from app.services import googlebooks, hardcover, national, openlibrary, provider_result

    metadata = {"title": "Cascade", "series_name": "Complete", "description": "Already present"}
    calls = []
    results = []
    def leg(name):
        async def lookup(*args, **kwargs):
            calls.append((name, args, kwargs))
            value = (provider_result.found(name, dict(metadata)) if name == winner
                     else provider_result.no_match(name))
            results.append(value)
            return value
        return lookup
    monkeypatch.setattr(national, "provider_for", lambda isbn: SimpleNamespace(lookup=leg("national")))
    monkeypatch.setattr(openlibrary, "lookup", leg("openlibrary"))
    monkeypatch.setattr(hardcover, "lookup_by_isbn", leg("hardcover"))
    monkeypatch.setattr(googlebooks, "lookup", leg("google"))
    client = object()
    result, source, ids, cascade = await items_common._lookup_metadata(
        "9780441172719", "hc-test", client, google_api_key="google-test")
    names = ["national", "openlibrary", "hardcover", "google"]
    assert [call[0] for call in calls] == (names if winner == "none" else names[:names.index(winner) + 1])
    for name, args, kwargs in calls:
        assert args == ("9780441172719", client)
        assert kwargs == ({"token": "hc-test"} if name == "hardcover" else {"api_key": "google-test"} if name == "google" else {})
    assert result == (None if winner == "none" else metadata)
    assert source == ("manual" if winner == "none" else winner)
    assert ids == ({"hardcover_book_id": None, "hardcover_edition_id": None} if winner == "hardcover" else {})
    assert isinstance(cascade, provider_result.ProviderResult)
    # The captured native combine contract returns the original winning leg,
    # including its provider/status; the generic name is only for no legs.
    assert cascade is results[0 if winner == "none" else names.index(winner)]
    assert cascade.provider == ("national" if winner == "none" else winner)
    assert cascade.status == (None if winner == "none" else 200)
    assert cascade.payload is result
    assert cascade.outcome == ("no_match" if winner == "none" else "found")


async def test_extracted_cascade_enriches_only_missing_primary_fields(monkeypatch):
    from app.routers import items_common
    from app.services import googlebooks, hardcover, national, openlibrary, provider_result

    primary = {"title": "Keep title", "description": "Keep description", "series_name": None}
    hc_data = {"title": "Do not replace", "description": "Do not replace description",
               "series_name": "Enriched series", "series_position": 2,
               "hardcover_book_id": 701, "hardcover_edition_id": 702, "cover_url": "https://assets.hardcover.app/cover.jpg"}
    primary_result = provider_result.found("openlibrary", primary)
    ol = AsyncMock(return_value=primary_result)
    hc = AsyncMock(return_value=provider_result.found("hardcover", hc_data))
    google = AsyncMock()
    monkeypatch.setattr(national, "provider_for", lambda isbn: None)
    monkeypatch.setattr(openlibrary, "lookup", ol)
    monkeypatch.setattr(hardcover, "lookup_by_isbn", hc)
    monkeypatch.setattr(googlebooks, "lookup", google)
    client = object()
    metadata, source, ids, cascade = await items_common._lookup_metadata("9780441172719", "hc-test", client)
    assert metadata == {"title": "Keep title", "description": "Keep description",
                        "series_name": "Enriched series", "series_position": 2}
    assert source == "openlibrary"
    assert ids == {"hardcover_book_id": 701, "hardcover_edition_id": 702, "cover_url": hc_data["cover_url"]}
    assert cascade is primary_result and cascade.found
    assert cascade.provider == "openlibrary" and cascade.status == 200
    assert cascade.payload is metadata
    ol.assert_awaited_once_with("9780441172719", client)
    hc.assert_awaited_once_with("9780441172719", client, token="hc-test")
    google.assert_not_awaited()
