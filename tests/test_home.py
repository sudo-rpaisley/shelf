"""Shelf Home page and media-family navigation contracts."""

import re

from app.config import MEDIA_FAMILIES, MEDIA_TYPES
from tests.conftest import _insert_item


def _family_card(html: str, key: str) -> str:
    """Return one Home media-family card so assertions stay scoped."""
    match = re.search(
        rf'<a[^>]*data-media-family="{re.escape(key)}"[^>]*>(.*?)</a>',
        html,
        re.S,
    )
    assert match, f"Home did not render a {key!r} family card"
    return match.group(0)


def test_every_media_type_belongs_to_exactly_one_family():
    """Formats can grow without making Home's category model ambiguous."""
    memberships = [media_type for family in MEDIA_FAMILIES.values() for media_type in family["types"]]
    assert len(memberships) == len(set(memberships))
    assert set(memberships) == set(MEDIA_TYPES)


def test_root_is_a_real_home_page(admin_client):
    response = admin_client.get("/")
    assert response.status_code == 200
    assert "Home — Shelf" in response.text
    assert "Your library" in response.text
    assert 'action="/browse"' in response.text
    assert '<a href="/" class="text-lg font-bold text-shelf-accent2 tracking-tight mr-6">Shelf</a>' in response.text


def test_home_counts_formats_by_family_and_uses_the_right_destination(admin_client, db):
    _insert_item(db, title="Family Book", media_type="book")
    _insert_item(db, title="Family eBook", media_type="ebook")
    _insert_item(db, title="Family Comic", media_type="digital_comic")
    _insert_item(db, title="Family Record", media_type="vinyl")
    db.commit()

    html = admin_client.get("/").text

    books = _family_card(html, "books")
    assert 'href="/browse?media_family_filter=books"' in books
    assert "2 items" in books

    comics = _family_card(html, "comics")
    assert "1 item" in comics

    music = _family_card(html, "music")
    assert 'href="/music"' in music
    assert "1 item" in music


def test_collection_family_filter_matches_every_format_in_that_family(admin_client, db):
    _insert_item(db, title="Physical Family Book", media_type="book")
    _insert_item(db, title="Digital Family Book", media_type="ebook")
    _insert_item(db, title="Separate Audiobook", media_type="audiobook")
    db.commit()

    response = admin_client.get("/browse?media_family_filter=books")
    assert response.status_code == 200
    assert "Physical Family Book" in response.text
    assert "Digital Family Book" in response.text
    assert "Separate Audiobook" not in response.text
    assert 'name="media_family_filter"' in response.text
    assert '<option value="books" selected>Books</option>' in response.text


def test_unknown_collection_family_matches_nothing(admin_client, db):
    _insert_item(db, title="Should Not Leak", media_type="book")
    db.commit()

    response = admin_client.get("/browse?media_family_filter=not-a-family")
    assert response.status_code == 200
    assert "Should Not Leak" not in response.text
