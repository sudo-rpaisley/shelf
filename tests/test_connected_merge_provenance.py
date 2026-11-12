"""Merging source observations must not transfer ownership of manual fields."""

import asyncio
import json

import httpx
import respx

from app.services import audiobookshelf, item_merge, item_write, metadata_policy
from tests.conftest import _insert_item


@respx.mock
def test_equal_manual_title_does_not_become_provider_owned_after_merge(admin_client, db):
    remote = item_write.insert_item(db, {
        "title": "Original", "isbn": "9780000009982", "media_type": "audiobook",
        "source": "audiobookshelf", "abs_id": "li_1", "abs_library_id": "audio",
    })
    manual = item_write.insert_item(db, {"title": "Original", "media_type": "audiobook"})
    metadata_policy.record_initial(db, remote, "audiobookshelf:li_1", {"title": "Original"})
    db.commit()
    response = admin_client.post("/api/items/merge", json={"keep_id": manual, "merge_ids": [remote]})
    assert response.json() == {"ok": True, "merged": 1}
    transferred = json.loads(db.execute(
        "SELECT value_json FROM item_metadata_provenance WHERE item_id = ? AND field_name = 'title'",
        (manual,),
    ).fetchone()[0])
    assert transferred["observed"] == "Original"
    assert transferred["overridden"] is True and "applied" not in transferred

    root = "http://abs.example"
    respx.get(root + "/api/libraries").respond(200, json={"libraries": [{"id": "audio", "mediaType": "book"}]})
    respx.get(root + "/api/libraries/audio/items").respond(200, json={"results": [{
        "id": "li_1", "media": {"metadata": {"title": "Corrected", "isbn": "9780000009982"},
                                   "numAudioFiles": 1, "duration": 3600},
    }]})
    respx.get(root + "/api/items/li_1/cover").respond(404)
    stats = asyncio.run(audiobookshelf.sync(root, "fixture-credential"))
    assert stats["errors"] == 0
    assert db.execute("SELECT title, abs_id FROM items_live WHERE id = ?", (manual,)).fetchone()[:] == ("Original", "li_1")


def test_merge_keeps_the_selected_rows_existing_source_authority(db):
    keep = _insert_item(db, title="Kept", isbn=None)
    other = _insert_item(db, title="Other", isbn=None)
    metadata_policy.record_initial(db, keep, "komga:entry", {"title": "Kept"})
    metadata_policy.record_initial(db, other, "komga:entry", {"title": "Other"})
    expected = db.execute("SELECT value_json FROM item_metadata_provenance WHERE item_id = ?", (keep,)).fetchone()[0]
    item_merge.reparent_children(db, keep, other)
    assert db.execute("SELECT value_json FROM item_metadata_provenance WHERE item_id = ?", (keep,)).fetchone()[0] == expected
    assert db.execute("SELECT COUNT(*) FROM item_metadata_provenance WHERE item_id = ?", (other,)).fetchone()[0] == 0
