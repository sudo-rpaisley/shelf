"""Real backup/restore and portable-archive privacy for connected-library data."""

import io
import json
import zipfile

from app import crypto, database
from tests.conftest import _insert_item


def _seed(db, user_id):
    key = crypto.get_encryption_key()
    credentials = crypto.encrypt_value(json.dumps({"token": "private-connected-credential"}), key)
    item_id = _insert_item(db, title="Portable public title", isbn=None)
    source_id = db.execute(
        "INSERT INTO source_instances (provider, name, base_url, credentials_enc) "
        "VALUES ('jellyfin', 'Screen library', 'http://screen.local', ?)", (credentials,),
    ).lastrowid
    db.execute(
        "INSERT INTO source_records (source_id, item_id, external_id, title, media_type, open_url) "
        "VALUES (?, ?, 'screen-identity', 'Remote title', 'digital_video', 'http://screen.local/web/index.html#!/details?id=screen-identity')",
        (source_id, item_id),
    )
    db.execute("INSERT INTO user_item_events (user_id, item_id, event_type, snapshot_json) VALUES (?, ?, 'updated', ?)",
               (user_id, item_id, json.dumps({"notes": "private-personal-history"})))
    event_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute("INSERT INTO user_item_state (user_id, item_id, status, notes, last_event_id) VALUES (?, ?, 'in_progress', 'private-personal-notes', ?)",
               (user_id, item_id, event_id))
    db.execute("INSERT INTO library_saved_views (user_id, name, filters_json) VALUES (?, 'Private view name', ?)",
               (user_id, json.dumps({"source": f"source:{source_id}"})))
    db.execute("INSERT INTO item_metadata_provenance (item_id, source_key, field_name, value_json) VALUES (?, ?, 'title', ?)",
               (item_id, f"source:{source_id}", json.dumps({"observed": "Remote title", "local": "Portable public title", "overridden": True})))
    other_id = _insert_item(db, title="Another public edition", isbn=None)
    db.execute("INSERT INTO connection_dismissals (item_a_id, item_b_id, evidence_key) VALUES (?, ?, 'private-dismissal-evidence')",
               (item_id, other_id))
    db.execute("INSERT INTO cover_sync_blocks (item_id, blocked) VALUES (?, 1)", (item_id,))
    db.commit()
    return item_id, source_id, credentials


def test_actual_backup_and_restore_preserves_bindings_credentials_and_private_state(admin_client, admin_user, db):
    item_id, source_id, ciphertext = _seed(db, admin_user["id"])
    backup = admin_client.get("/api/settings/backup")
    assert backup.status_code == 200 and backup.content.startswith(b"SQLite format 3")
    assert b"private-connected-credential" not in backup.content
    db.execute("DELETE FROM user_item_state")
    db.execute("DELETE FROM user_item_events")
    db.execute("DELETE FROM library_saved_views")
    db.execute("DELETE FROM item_metadata_provenance")
    db.execute("DELETE FROM connection_dismissals")
    db.execute("DELETE FROM cover_sync_blocks")
    db.execute("DELETE FROM source_instances")
    db.commit()
    restored = admin_client.post("/api/settings/restore", files={"file": ("connected.db", backup.content, "application/octet-stream")})
    assert restored.status_code == 200 and restored.json()["ok"] is True
    # Real restore invalidates old sessions rather than reusing an authenticated
    # cookie against the restored account's private data.
    assert admin_client.get("/activity", follow_redirects=False).status_code == 303
    with database.get_db() as current:
        assert current.execute("SELECT item_id FROM source_records WHERE source_id = ?", (source_id,)).fetchone()[0] == item_id
        stored = current.execute("SELECT credentials_enc FROM source_instances WHERE id = ?", (source_id,)).fetchone()[0]
        assert stored == ciphertext
        assert json.loads(crypto.decrypt_value(stored, crypto.get_encryption_key()))["token"] == "private-connected-credential"
        assert current.execute("SELECT notes FROM user_item_state").fetchone()[0] == "private-personal-notes"
        assert "private-personal-history" in current.execute("SELECT snapshot_json FROM user_item_events").fetchone()[0]
        assert current.execute("SELECT name FROM library_saved_views").fetchone()[0] == "Private view name"
        assert current.execute("SELECT COUNT(*) FROM item_metadata_provenance").fetchone()[0] == 1
        assert current.execute("SELECT evidence_key FROM connection_dismissals").fetchone()[0] == "private-dismissal-evidence"
        assert current.execute("SELECT item_id, blocked FROM cover_sync_blocks").fetchone()[:] == (item_id, 1)
        assert current.execute("PRAGMA foreign_key_check").fetchall() == []


def test_portable_archive_excludes_private_activity_and_connection_configuration(admin_client, admin_user, db):
    _seed(db, admin_user["id"])
    response = admin_client.get("/api/export/archive")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        exported = "\n".join(archive.read(name).decode("utf-8") for name in archive.namelist() if name.endswith(".json"))
    assert "Portable public title" in exported
    for private in ("private-connected-credential", "private-personal-notes", "private-personal-history",
                    "Private view name", "screen-identity", "http://screen.local", "private-dismissal-evidence"):
        assert private not in exported
