"""Review regressions. Real-app/canonical-connection execution awaits freeze."""

import asyncio
import io
import json
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import (
    _create_user, _insert_item, _insert_personal_history, _seed_personal_state,
)


def _reader(title, content=b"validated archive cover"):
    return SimpleNamespace(
        library={"items": [{"id": 1, "title": title, "media_type": "book", "owned": 1,
                            "cover": "covers/1.jpg"}]},
        cover_names={"covers/1.jpg"}, read_cover=lambda name: content,
    )


def _apply(db, reader):
    from app.services import archive

    return archive.apply_plan(db, reader, archive.plan_archive(db, reader, mode="update"),
                              {"replace_covers": True})


def _assert_resources_released(db):
    assert getattr(db, "_transaction_resources", None) is None
    assert db._after_commit == []
    assert db._after_rollback == []


def test_export_responses_paused_before_open_keep_each_actors_private_payload_and_cleanup(db, admin_user):
    from app.routers import archive as route
    from app.services import catalogue_security

    second_user = _create_user("second-archive-admin", "password123", "Second admin", "admin")
    item_id = _insert_item(db, title="Export race item", isbn=None)
    for user, text in ((admin_user, "first actor private"), (second_user, "second actor private")):
        _seed_personal_state(db, user["id"], item_id, personal_notes=text)
        _insert_personal_history(db, user["id"], item_id, notes=text)
    db.commit()

    def prepare(user):
        token = catalogue_security.set_actor(user)
        try:
            return asyncio.run(route.export_archive(_=user))
        finally:
            catalogue_security.reset_actor(token)

    # FileResponse has not stat'ed/opened either file yet. This is exactly the
    # vulnerable gap: prepare B before the ASGI response for A starts streaming.
    first, second = prepare(admin_user), prepare(second_user)
    first_path, second_path = Path(first.path), Path(second.path)
    assert first_path != second_path
    assert first_path.is_file() and second_path.is_file()

    async def stream(response):
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await response({"type": "http", "method": "GET", "headers": [], "extensions": {},
                        "asgi": {"spec_version": "2.4"}}, receive, send)
        body = b"".join(message.get("body", b"") for message in messages)
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            return json.loads(archive.read("library.json"))

    try:
        first_payload = asyncio.run(stream(first))
        assert not first_path.exists()
        assert second_path.is_file()  # A's background task must not delete B
        second_payload = asyncio.run(stream(second))
        assert not second_path.exists()
        for payload, own, other in ((first_payload, "first actor private", "second actor private"),
                                    (second_payload, "second actor private", "first actor private")):
            assert payload["items"][0]["personal_state"]["personal_notes"] == own
            assert [row["notes"] for row in payload["reading_log"]] == [own]
            assert other not in json.dumps(payload)
    finally:
        first_path.unlink(missing_ok=True)
        second_path.unlink(missing_ok=True)


def test_failed_archive_build_cleans_only_its_own_partial_export(db, monkeypatch):
    from app import config
    from app.services import archive

    item_id = _insert_item(db, title="Export failure cover", isbn=None)
    (config.COVERS_DIR / f"{item_id}.jpg").write_bytes(b"cover bytes")
    db.execute("UPDATE items SET cover_path=? WHERE id=?", (f"covers/{item_id}.jpg", item_id))
    first = archive.build_archive(db)

    def fail(*args):
        raise OSError("injected zip failure")

    monkeypatch.setattr(zipfile.ZipFile, "write", fail)
    try:
        with pytest.raises(OSError, match="injected zip failure"):
            archive.build_archive(db)
        assert set(config.DATA_DIR.glob("shelf_archive_*.zip")) == {first}
    finally:
        first.unlink(missing_ok=True)


@pytest.mark.parametrize("existing_cover", [False, True])
def test_apply_plan_retains_cover_recovery_and_never_commits_caller_work(db, existing_cover):
    from app import config

    marker = _insert_item(db, title="Caller transaction marker", isbn=None)
    target = _insert_item(db, title="Cover transaction target", isbn=None) if existing_cover else None
    path = config.COVERS_DIR / f"{target}.jpg" if target else None
    if path:
        path.write_bytes(b"original hand-picked bytes")
        db.execute("UPDATE items SET cover_path=? WHERE id=?", (f"covers/{target}.jpg", target))
    db.commit()
    before = "\n".join(db.iterdump())
    db.execute("UPDATE items SET publisher='uncommitted caller work' WHERE id=?", (marker,))
    report = _apply(db, _reader("Cover transaction target"))
    assert report["errors"] == []
    target = db.execute("SELECT id FROM items WHERE title='Cover transaction target'").fetchone()[0]
    path = config.COVERS_DIR / f"{target}.jpg"
    assert path.read_bytes() == b"validated archive cover"
    assert db.in_transaction
    assert db._transaction_resources is not None
    db.rollback()
    assert "\n".join(db.iterdump()) == before
    if existing_cover:
        assert path.read_bytes() == b"original hand-picked bytes"
    else:
        assert not path.exists()
    _assert_resources_released(db)


def test_multiple_apply_calls_rollback_to_original_not_intermediate_cover(db):
    from app import config

    target = _insert_item(db, title="Repeated cover", isbn=None)
    path = config.COVERS_DIR / f"{target}.jpg"
    path.write_bytes(b"original")
    db.execute("UPDATE items SET cover_path=? WHERE id=?", (f"covers/{target}.jpg", target))
    db.commit()
    for content in (b"first import", b"second import"):
        assert _apply(db, _reader("Repeated cover", content))["errors"] == []
        assert path.read_bytes() == content
    db.rollback()
    assert path.read_bytes() == b"original"
    _assert_resources_released(db)
    assert _apply(db, _reader("Repeated cover", b"committed"))["errors"] == []
    db.commit()
    _assert_resources_released(db)
    db.rollback()
    assert path.read_bytes() == b"committed"


def test_real_deferred_commit_failure_restores_cover_and_releases_resources(db):
    from app import config

    target = _insert_item(db, title="Deferred commit cover", isbn=None)
    path = config.COVERS_DIR / f"{target}.jpg"
    path.write_bytes(b"original deferred cover")
    db.execute("UPDATE items SET cover_path=? WHERE id=?", (f"covers/{target}.jpg", target))
    db.execute("CREATE TABLE review_deferred_owner(id INTEGER PRIMARY KEY, "
               "user_id INTEGER REFERENCES users(id) DEFERRABLE INITIALLY DEFERRED)")
    db.commit()
    before = "\n".join(db.iterdump())
    db.execute("INSERT INTO review_deferred_owner VALUES(1,-999999)")
    assert _apply(db, _reader("Deferred commit cover"))["errors"] == []
    with pytest.raises(sqlite3.IntegrityError):
        db.commit()
    assert not db.in_transaction
    assert path.read_bytes() == b"original deferred cover"
    assert "\n".join(db.iterdump()) == before
    _assert_resources_released(db)


@pytest.mark.parametrize("outcome", ["close", "context_rollback", "context_commit", "context_commit_failure"])
def test_canonical_connection_close_and_context_exit_own_resource_outcome(tmp_path, monkeypatch, outcome):
    from app import config
    from app.database import _Connection
    from app.services import archive

    covers = tmp_path / "canonical-cover-probe"
    covers.mkdir()
    monkeypatch.setattr(config, "COVERS_DIR", covers)
    path = covers / "1.jpg"
    path.write_bytes(b"original canonical cover")
    conn = sqlite3.connect(":memory:", factory=_Connection)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("CREATE TABLE items(id INTEGER PRIMARY KEY,title TEXT,updated_at TEXT)")
    conn.execute("CREATE TABLE review_deferred_child(item_id INTEGER REFERENCES items(id) DEFERRABLE INITIALLY DEFERRED)")
    conn.execute("INSERT INTO items VALUES(1,'original',NULL)")
    conn.commit()

    @archive._atomic_archive_apply
    def mutate(db):
        db.execute("UPDATE items SET title='uncommitted'")
        archive._install_cover(_reader("unused", b"replacement"), 1, "covers/1.jpg")

    try:
        if outcome == "close":
            mutate(conn)
            conn.close()
        elif outcome == "context_rollback":
            with pytest.raises(RuntimeError, match="outer context refusal"):
                with conn:
                    mutate(conn)
                    raise RuntimeError("outer context refusal")
        elif outcome == "context_commit_failure":
            with pytest.raises(sqlite3.IntegrityError):
                with conn:
                    mutate(conn)
                    conn.execute("INSERT INTO review_deferred_child VALUES(-999999)")
            assert not conn.in_transaction
            _assert_resources_released(conn)
        else:
            with conn:
                mutate(conn)
            _assert_resources_released(conn)
            conn.close()
        expected = b"replacement" if outcome == "context_commit" else b"original canonical cover"
        assert path.read_bytes() == expected
    finally:
        conn.close()


def test_nested_cover_journals_and_local_failures_cancel_only_their_resource(db):
    from app import config
    from app.services import archive

    path = config.COVERS_DIR / "982321.jpg"
    path.write_bytes(b"original nested cover")

    @archive._atomic_archive_apply
    def inner(conn, content, fail=False):
        archive._install_cover(_reader("unused", content), 982321, "covers/1.jpg")
        if fail:
            raise RuntimeError("inner refusal")

    @archive._atomic_archive_apply
    def outer(conn, fail=False):
        inner(conn, b"first nested")
        with pytest.raises(RuntimeError, match="inner refusal"):
            inner(conn, b"failed nested", fail=True)
        assert path.read_bytes() == b"first nested"
        inner(conn, b"last nested")
        if fail:
            raise RuntimeError("outer refusal")

    outer(db)
    db.rollback()
    assert path.read_bytes() == b"original nested cover"
    _assert_resources_released(db)
    with pytest.raises(RuntimeError, match="outer refusal"):
        outer(db, fail=True)
    assert path.read_bytes() == b"original nested cover"
    # Last successful local undo releases the fence, but the namespace remains
    # bound until the actual outer outcome (no switch to another directory).
    assert db._transaction_resources.entries == []
    assert db._transaction_resources.fence is None
    assert db._after_commit == db._after_rollback == []
    db.rollback()
    _assert_resources_released(db)
    assert path.read_bytes() == b"original nested cover"


def test_failed_archive_row_undoes_cover_immediately_then_retry_tracks_outer_rollback(db, monkeypatch):
    from app import config
    from app.services import archive

    install = archive._install_cover
    failures = []

    def fail_once(reader, item_id, arcname):
        result = install(reader, item_id, arcname)
        if not failures:
            failures.append(item_id)
            raise RuntimeError("injected failure after cover replacement")
        return result

    monkeypatch.setattr(archive, "_install_cover", fail_once)
    reader = _reader("Retry failed archive cover")
    reader.library["items"].append({**reader.library["items"][0], "id": 2})
    result = _apply(db, reader)
    assert result["imported"] == 1 and len(result["errors"]) == 1
    assert db.execute("SELECT COUNT(*) FROM items WHERE title='Retry failed archive cover'").fetchone()[0] == 1
    assert (config.COVERS_DIR / f"{failures[0]}.jpg").read_bytes() == b"validated archive cover"
    db.rollback()
    assert not (config.COVERS_DIR / f"{failures[0]}.jpg").exists()
    _assert_resources_released(db)


def test_raw_and_commit_only_connections_cannot_finalize_critical_resources_early(tmp_path):
    from app.services.transaction_resources import TransactionResourceError, register_transaction_resource

    calls = []
    raw = sqlite3.connect(":memory:")
    raw.execute("CREATE TABLE refusal(value TEXT)")
    raw.execute("INSERT INTO refusal VALUES('active raw transaction')")
    commit_only = SimpleNamespace(in_transaction=True, _after_commit=[])
    list_only = SimpleNamespace(in_transaction=True, _after_commit=[], _after_rollback=[])
    try:
        for conn in (raw, commit_only, list_only):
            with pytest.raises(TransactionResourceError, match="protected commit AND rollback"):
                register_transaction_resource(conn, publication_dir=tmp_path / "covers",
                                               after_commit=lambda: calls.append("commit"),
                                               after_rollback=lambda: calls.append("rollback"))
        assert calls == []
        assert commit_only._after_commit == list_only._after_commit == list_only._after_rollback == []
        assert getattr(list_only, "_transaction_resources", None) is None
        assert not (tmp_path / "covers").exists()
    finally:
        raw.close()


@pytest.fixture
def restore_key_material(admin_client):
    from app import config
    from app.auth import get_secret_key
    from app.crypto import get_encryption_key

    keys = get_encryption_key(), get_secret_key()
    paths = [config.DATA_DIR / name for name in ("encryption.key", "signing.key")]

    def snapshot():
        return [(path.name, path.read_bytes(), path.stat().st_mode) if path.exists()
                else (path.name, None, None) for path in paths]

    return keys, snapshot


def _legacy_restore_upload(admin_client, tmp_path):
    from app.crypto import encrypt_value

    response = admin_client.get("/api/settings/backup")
    assert response.status_code == 200
    path = tmp_path / "synthetic-legacy-restore.db"
    path.write_bytes(response.content)
    source_key = "synthetic-source-only-jwt-key-restore-review"
    values = {
        "secret_key": source_key,
        "abs_token": encrypt_value("source-legacy-abs-restore-review", source_key),
        "romm_api_token": "plain-native-romm-restore-review",
        "romm_token": "plain-fork-romm-restore-review",
        "hardcover_token": encrypt_value("foreign-encrypted-credential", "synthetic-unavailable-key"),
    }
    with sqlite3.connect(path) as conn:
        conn.executemany("INSERT INTO settings(key,value) VALUES(?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", values.items())
    return path.read_bytes(), values


def test_restore_crypto_is_normalized_and_source_key_removed_before_live_backup(
    admin_client, db, tmp_path, monkeypatch, restore_key_material,
):
    from app import auth, crypto
    from app.routers import settings

    (encryption_key, signing_key), key_snapshot = restore_key_material
    upload, source = _legacy_restore_upload(admin_client, tmp_path)
    before_keys = key_snapshot()
    before_live = "\n".join(db.iterdump())
    install = settings._install_restored_database
    observed = []

    def inspect_stage(staged, staged_path):
        from app.database import get_db

        rows = dict(staged.execute("SELECT key,value FROM settings"))
        assert "secret_key" not in rows
        for name, expected in (("abs_token", "source-legacy-abs-restore-review"),
                               ("romm_api_token", source["romm_api_token"]), ("romm_token", source["romm_token"])):
            assert crypto.is_fernet_token(rows[name])
            assert crypto._fernet(encryption_key).decrypt(rows[name].encode()).decode() == expected
            assert expected.encode() not in staged_path.read_bytes()
        assert source["secret_key"].encode() not in staged_path.read_bytes()
        assert rows["hardcover_token"] == source["hardcover_token"]
        with get_db() as live:
            assert "\n".join(live.iterdump()) == before_live
        assert key_snapshot() == before_keys
        observed.append(True)
        install(staged, staged_path)

    monkeypatch.setattr(settings, "_install_restored_database", inspect_stage)
    response = admin_client.post("/api/settings/restore", files={"file": ("legacy.db", upload, "application/octet-stream")})
    assert response.status_code == 200
    assert response.json()["ok"] is True, response.json()
    assert response.json()["credentials_require_reentry"] == ["hardcover_token"]
    assert observed == [True]
    assert key_snapshot() == before_keys
    assert auth._cached_secret_key == signing_key
    assert db.execute("SELECT 1 FROM settings WHERE key='secret_key'").fetchone() is None


def test_crypto_normalization_failure_leaves_live_database_and_external_keys_unchanged(
    admin_client, db, tmp_path, monkeypatch, restore_key_material,
):
    from app import crypto
    from app.routers import settings

    _keys, key_snapshot = restore_key_material
    upload, _source = _legacy_restore_upload(admin_client, tmp_path)
    before_live, before_keys = "\n".join(db.iterdump()), key_snapshot()
    calls = []

    def fail(staged, **kwargs):
        staged.execute("UPDATE users SET token_version=token_version+900")
        staged.execute("UPDATE settings SET value='staged failure only' WHERE key='abs_token'")
        calls.append(True)
        raise RuntimeError("injected crypto normalization failure")

    def forbidden_copy(*args):
        raise AssertionError("failed crypto staging must never reach the live copy")

    monkeypatch.setattr(crypto, "normalize_sensitive_settings", fail)
    monkeypatch.setattr(settings, "_install_restored_database", forbidden_copy)
    response = admin_client.post("/api/settings/restore", files={"file": ("legacy.db", upload, "application/octet-stream")})
    assert response.json()["ok"] is False
    assert calls == [True]
    assert "\n".join(db.iterdump()) == before_live
    assert key_snapshot() == before_keys
    assert not list(settings.DATA_DIR.glob("shelf_restore_*.db*"))


@pytest.mark.parametrize("missing", ["encryption", "signing"])
def test_restore_key_capture_refuses_missing_keys_without_key_generation(monkeypatch, missing):
    from app import auth, crypto
    from app.routers import settings

    monkeypatch.setattr(crypto, "_cached_encryption_key", None if missing == "encryption" else "synthetic-existing")
    monkeypatch.setattr(auth, "_cached_secret_key", None if missing == "signing" else "synthetic-existing-signing")
    monkeypatch.setattr(auth, "SECRET_KEY", "")
    monkeypatch.delenv("SHELF_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(crypto, "_read_keyfile", lambda name: None)

    def forbidden(*args):
        raise AssertionError("restore attempted key generation/relocation")

    monkeypatch.setattr(crypto, "_read_or_create_keyfile", forbidden)
    monkeypatch.setattr(crypto, "_write_keyfile", forbidden)
    monkeypatch.setattr(auth, "get_secret_key", forbidden)
    with pytest.raises(ValueError, match=f"existing destination {missing} key"):
        settings._restore_destination_keys()


def test_connection_crypto_normalization_uses_only_explicit_keys_and_is_idempotent(monkeypatch):
    from app import auth, crypto, database

    encryption_key, signing_key, source_key = "synthetic-dest-enc", "synthetic-dest-sign", "synthetic-source-sign"
    source_ciphertext = crypto.encrypt_value("source-only plaintext", source_key)
    destination_ciphertext = crypto.encrypt_value("already normalized", encryption_key)
    unknown = crypto.encrypt_value("unavailable plaintext", "synthetic-unavailable")
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)")
    conn.executemany("INSERT INTO settings VALUES(?,?)", [
        ("abs_token", source_ciphertext), ("komga_api_key", crypto.encrypt_value("old destination signing", signing_key)),
        ("romm_token", "plain fork alias"), ("romm_api_token", "plain native alias"),
        ("tmdb_api_key", destination_ciphertext), ("hardcover_token", unknown),
    ])

    def forbidden(*args):
        raise AssertionError("connection normalizer accessed an active DB/key resolver")

    monkeypatch.setattr(database, "get_db", forbidden)
    monkeypatch.setattr(auth, "get_secret_key", forbidden)
    monkeypatch.setattr(crypto, "get_encryption_key", forbidden)
    monkeypatch.setattr(crypto, "get_existing_encryption_key", forbidden)
    monkeypatch.setattr(crypto, "_read_keyfile", forbidden)
    monkeypatch.setattr(crypto, "_write_keyfile", forbidden)
    try:
        result = crypto.normalize_sensitive_settings(conn, encryption_key=encryption_key, signing_key=signing_key,
                                                     legacy_signing_keys=(source_key,))
        assert result == {"rewritten": 4, "undecryptable": ["hardcover_token"]}
        values = dict(conn.execute("SELECT key,value FROM settings"))
        assert values["tmdb_api_key"] == destination_ciphertext
        assert values["hardcover_token"] == unknown
        assert values["abs_token"] != source_ciphertext
        for key, expected in (("abs_token", "source-only plaintext"), ("komga_api_key", "old destination signing"),
                              ("romm_token", "plain fork alias"), ("romm_api_token", "plain native alias")):
            assert crypto._fernet(encryption_key).decrypt(values[key].encode()).decode() == expected
        assert crypto.normalize_sensitive_settings(conn, encryption_key=encryption_key, signing_key=signing_key) == {
            "rewritten": 0, "undecryptable": ["hardcover_token"],
        }
    finally:
        conn.close()


def test_explicit_zero_copy_archive_keeps_seam_and_stays_empty_after_metadata_edit(admin_client, db):
    from app.services import item_write
    from tests.test_integrated_restore_import_guards import _post_archive

    result = _post_archive(admin_client, {"items": [{
        "id": 1, "title": "Explicit zero-copy review item", "media_type": "book", "owned": 1,
        "location": "Zero-copy legacy review shelf", "copies": [],
    }]})
    assert result["errors"] == []
    item = db.execute("SELECT id,location_id FROM items WHERE title='Explicit zero-copy review item'").fetchone()
    assert item["location_id"] is not None
    item_write.update_item_fields(db, item["id"], {"publisher": "Metadata only"})
    assert db.execute("SELECT COUNT(*) FROM item_copies WHERE item_id=?", (item["id"],)).fetchone()[0] == 0
    assert db.execute("SELECT location_id FROM items WHERE id=?", (item["id"],)).fetchone()[0] == item["location_id"]
