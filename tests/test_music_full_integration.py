"""Focused regressions for the complete first-class Music surface."""

from pathlib import Path

from app.config import MEDIA_TYPES, MUSIC_MEDIA_TYPES
from app.routers import music


def test_music_family_is_first_class():
    assert MUSIC_MEDIA_TYPES == {
        "vinyl",
        "cassette",
        "cd",
        "digital_music",
    }
    assert MUSIC_MEDIA_TYPES <= set(MEDIA_TYPES)


def test_music_router_exposes_browse_add_detail_and_refresh():
    paths = {(route.path, ",".join(sorted(route.methods or []))) for route in music.router.routes}
    assert ("/music", "GET") in paths
    assert ("/api/music/add", "POST") in paths
    assert ("/music/item/{item_id}", "GET") in paths
    assert ("/api/music/items/{item_id}/refresh", "POST") in paths


def test_musicbrainz_formats_map_conservatively():
    assert music._infer_media_type({"media": [{"format": "12\" Vinyl"}]}) == "vinyl"
    assert music._infer_media_type({"media": [{"format": "Cassette"}]}) == "cassette"
    assert music._infer_media_type({"media": [{"format": "CD"}]}) == "cd"
    assert music._infer_media_type({"media": [{"format": "Digital Media"}]}) == "digital_music"
    # Unrecognised formats stay unset so the add screen asks for the format.
    assert music._infer_media_type({"media": [{"format": "MiniDisc"}]}) is None


def test_music_templates_keep_exact_release_workflow_visible(db, request):
    from app.services import item_write, music_catalog
    from tests.test_media_page_cards import _HTML

    root = Path(__file__).parents[1] / "app" / "templates"
    library = (root / "music.html").read_text()
    detail = (root / "music_item.html").read_text()

    assert 'action="/api/music/add"' in library
    assert "Search MusicBrainz" in library
    release = {
        "title": "Exact local release", "artist_credit": "Local artist",
        "musicbrainz_release_id": "11111111-1111-1111-1111-111111111111",
        "format_summary": "CD",
        "media": [{"position": 1, "format": "CD", "tracks": [{"number": "1", "title": "Local track"}]}],
    }
    item_id = item_write.insert_item(db, title=release["title"], authors=release["artist_credit"],
                                     media_type="cd", source="musicbrainz")
    music_catalog.save_release(db, item_id, release)
    db.commit()
    # The real sandbox is initialized and seeded before requesting the client/app.
    client = request.getfixturevalue("viewer_client")
    response = client.get("/music")  # No query means no provider search.
    assert response.status_code == 200
    local = _HTML(response.text).root.find("section", **{"data-testid": "music-local-items"})
    assert len(local) == 1
    cards = local[0].find("article", cls="media-card", **{"data-item-id": str(item_id)})
    assert len(cards) == 1
    links = cards[0].find("a", cls="media-card__detail")
    assert len(links) == 1
    href = links[0].attrs["href"]
    assert href == f"/music/item/{item_id}"
    detail_response = client.get(href, follow_redirects=False)
    assert detail_response.status_code == 200
    assert detail_response.context["item"]["id"] == item_id
    assert detail_response.context["release"]["musicbrainz_release_id"] == release["musicbrainz_release_id"]
    assert 'action="/api/music/preview"' in detail
    assert 'data-music-review-open' in detail
    assert 'action="/api/music/items/{{ item.id }}/refresh"' not in detail
    assert "Track listing" in detail
    assert "musicbrainz.org/release/" in detail
