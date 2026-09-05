"""Item detail connected-service actions belong in the hero card."""

from tests.conftest import _insert_item


def _set(db, key, value):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    db.commit()


def test_komga_action_renders_in_hero_without_connected_services_card(admin_client, db):
    item_id = _insert_item(
        db,
        title="Komga Hero Comic",
        isbn=None,
        media_type="digital_comic",
        komga_id="book_hero",
        komga_library_id="lib_1",
        source="komga",
    )
    _set(db, "komga_url", "http://komga:25600")
    _set(db, "komga_public_url", "https://comics.example.test")

    html = admin_client.get(f"/item/{item_id}").text
    hero_start = html.index('data-testid="item-hero"')
    hero_end = html.index("</section>", hero_start)
    hero = html[hero_start:hero_end]

    assert 'data-testid="item-hero-actions"' in hero
    assert "Open in Komga" in hero
    assert 'href="https://comics.example.test/book/book_hero"' in hero
    assert 'data-testid="item-connected-services"' not in html
    assert "Open and sync elsewhere" not in html
