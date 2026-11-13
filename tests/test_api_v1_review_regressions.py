"""Independent-review regressions: real covers, URL privacy and strict queries."""

import json
from uuid import UUID

import pytest

from app import config
from app.services import api_tokens, covers, release_radar
from tests.conftest import _insert_borrower
from tests.test_api_v1_reads import PREFIX, _item, _loan, _source


@pytest.mark.parametrize("unique", [False, True])
@pytest.mark.parametrize("legacy_bare", [False, True])
def test_actual_cover_writer_paths_survive_index_and_detail(viewer_client, db, unique, legacy_bare):
    item_id = _item(db, "Real uploaded artwork")
    written = covers.save_uploaded_cover(item_id, b"\xff\xd8\xff" + b"\x00" * 512, unique=unique)
    assert written is not None and written.startswith("covers/")
    filename = written.removeprefix("covers/")
    assert (config.COVERS_DIR / filename).is_file()
    if unique:
        assert UUID(filename.removeprefix(f"{item_id}-").removesuffix(".jpg")).hex in filename
    stored = filename if legacy_bare else written
    db.execute("UPDATE items SET cover_path = ? WHERE id = ?", (stored, item_id))
    db.commit()

    index = viewer_client.get(PREFIX + "/items")
    detail = viewer_client.get(PREFIX + f"/items/{item_id}")
    assert index.status_code == detail.status_code == 200
    assert index.json()["data"][0]["cover_url"] == "/" + written
    assert detail.json()["item"]["cover_url"] == "/" + written


@pytest.mark.parametrize("path", [
    "../cover.jpg", "covers/../cover.jpg", "covers/nested/cover.jpg", "covers//cover.jpg",
    "/covers/cover.jpg", "//covers/cover.jpg", "/private/cover.jpg", "C:\\covers\\cover.jpg",
    "covers\\cover.jpg", "covers/cover.jpg?devkey=SYNTHETIC_SECRET", "covers/cover.jpg#private",
    "https://images.example/cover.jpg", "covers/%2e%2e%2fcover.jpg", "covers/cover%2ejpg",
])
def test_noncanonical_cover_paths_are_not_projected(viewer_client, db, path):
    item_id = _item(db, "Unsafe stored artwork", cover_path=path)
    db.commit()
    assert viewer_client.get(PREFIX + "/items").json()["data"][0]["cover_url"] is None
    assert viewer_client.get(PREFIX + f"/items/{item_id}").json()["item"]["cover_url"] is None


@pytest.mark.parametrize("suffix", [
    "?devkey=SYNTHETIC_SECRET", "?session_id=SYNTHETIC_SECRET",
    "#devkey=SYNTHETIC_SECRET", "#session_id=SYNTHETIC_SECRET",
    "#/preview?devkey=SYNTHETIC_SECRET", "#/preview?session_id=SYNTHETIC_SECRET",
    "?id=42;session_id=SYNTHETIC_SECRET", "?id=42&Session_ID=SYNTHETIC_SECRET",
    "?%64evkey=SYNTHETIC_SECRET", "?session%5fid=SYNTHETIC_SECRET",
    "#/preview?id=42;session_id=SYNTHETIC_SECRET",
    "?w=SYNTHETIC_SECRET", "?quality=SYNTHETIC_SECRET", "?fm=SYNTHETIC_SECRET",
    "?fit=SYNTHETIC_SECRET", "?auto=SYNTHETIC_SECRET", "?unknown=SYNTHETIC_SECRET",
])
def test_normal_radar_upsert_and_source_links_cannot_leak_credentials(viewer_client, db, suffix):
    item_id = _item(db, "Visible local volume", series_name="Visible watched series")
    url = "https://images.example/cover.jpg" + suffix
    _source(db, item_id, url=url)
    watch = db.execute("INSERT INTO release_watches (series_name) VALUES ('Visible watched series')").lastrowid
    # The real radar writer accepts HTTPS. Projection must independently reject
    # credential-bearing URLs already persisted by that ordinary write path.
    release_radar.upsert_rows(db, watch, [{"hardcover_book_id": 99, "title": "Announced volume", "cover_url": url}])
    db.commit()
    upcoming = viewer_client.get(PREFIX + "/upcoming")
    detail = viewer_client.get(PREFIX + f"/items/{item_id}")
    assert upcoming.status_code == detail.status_code == 200
    assert upcoming.json()["total"] == 1
    assert upcoming.json()["data"][0]["cover_url"] is None
    assert detail.json()["sources"]["data"][0]["url"] is None
    assert "SYNTHETIC_SECRET" not in json.dumps(upcoming.json())
    assert "SYNTHETIC_SECRET" not in json.dumps(detail.json())


@pytest.mark.parametrize("url", [
    "https://images.example/covers/123.jpg",
    "https://assets.hardcover.app/covers/123-1234567890abcdef1234567890abcdef.jpg",
    "https://images.example/covers/123.jpg?id=42&format=webp",
    "https://images.example/covers/123.jpg?w=320&h=480&fit=crop&auto=format&fm=webp&q=80",
    "https://images.example/covers/123.jpg?width=320&height=480&quality=100&fit=contain",
])
def test_normal_radar_upsert_preserves_public_images_and_bounded_transforms(viewer_client, db, url):
    _item(db, "Visible local volume", series_name="Visible watched series")
    watch = db.execute("INSERT INTO release_watches (series_name) VALUES ('Visible watched series')").lastrowid
    release_radar.upsert_rows(db, watch, [{"hardcover_book_id": 99, "title": "Announced volume", "cover_url": url}])
    db.commit()
    response = viewer_client.get(PREFIX + "/upcoming")
    assert response.status_code == 200
    assert response.json()["data"][0]["cover_url"] == url


@pytest.mark.parametrize("resource", ["loan", "list", "schema"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("bearer", [False, True])
def test_unpaged_routes_reject_unknown_queries(viewer_client, viewer_user, db, resource, method, bearer):
    item_id = _item(db, "Loaned item")
    borrower = _insert_borrower(db)
    loan_id = _loan(db, item_id, borrower)
    headers = {}
    if bearer:
        token = api_tokens.create(db, viewer_user["id"], "strict-queries")["token"]
        headers = {"Authorization": "Bearer " + token}
    db.commit()
    if bearer:
        viewer_client.cookies.clear()
    path = PREFIX + {"loan": f"/loans/{loan_id}", "list": "/lists/wishlist", "schema": "/schema"}[resource]
    assert viewer_client.request(method, path, headers=headers).status_code == 200
    for key in ("unexpected", "page", "page_size"):
        response = viewer_client.request(method, path, params={key: "1"}, headers=headers)
        assert response.status_code == 422, (method, path, key, response.text)
        assert response.headers["cache-control"] == "no-store"
        if method == "HEAD":
            assert response.content == b""
        else:
            assert response.json()["detail"][0]["loc"] == ["query", key]


def test_schema_documents_unknown_query_refusal_for_unpaged_reads(viewer_client):
    document = viewer_client.get(PREFIX + "/schema").json()
    for path in ("/loans/{loan_id}", "/lists/{slug}", "/schema"):
        operation = document["paths"][PREFIX + path]["get"]
        assert "422" in operation["responses"]
        assert not [parameter for parameter in operation.get("parameters", []) if parameter["in"] == "query"]
