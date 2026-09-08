from app.auth import create_token
from app.services import user_state
from tests.conftest import _insert_item


def _as(client, user):
    token = create_token(
        user["id"], user["username"], user["role"], user["display_name"]
    )
    client.cookies.set("access_token", token)


def test_personal_browse_routes_replace_shared_state_routes_once():
    from app.main import app

    browse = [
        route for route in app.routes
        if getattr(route, "path", None) == "/browse"
        and "GET" in (getattr(route, "methods", None) or set())
    ]
    search = [
        route for route in app.routes
        if getattr(route, "path", None) == "/api/search"
        and "GET" in (getattr(route, "methods", None) or set())
    ]

    assert len(browse) == 1
    assert len(search) == 1
    assert browse[0].endpoint.__name__ == "personal_browse"
    assert search[0].endpoint.__name__ == "personal_search_items"


def test_reading_status_filter_is_isolated_between_users(
    db, client, viewer_user, admin_user
):
    item_id = _insert_item(
        db,
        title="Private Reading State",
        isbn="9780000000033",
        owned=1,
        reading_status=None,
    )
    user_state.set_reading_status(db, viewer_user["id"], item_id, "read")
    user_state.set_reading_status(db, admin_user["id"], item_id, "reading")
    db.commit()

    _as(client, viewer_user)
    viewer = client.get("/api/search?reading_status=read")
    assert viewer.status_code == 200
    assert "Private Reading State" in viewer.text

    _as(client, admin_user)
    admin = client.get("/api/search?reading_status=read")
    assert admin.status_code == 200
    assert "Private Reading State" not in admin.text

    admin_reading = client.get("/api/search?reading_status=reading")
    assert "Private Reading State" in admin_reading.text


def test_shared_legacy_status_is_not_inherited_by_new_user(
    db, client, viewer_user
):
    _insert_item(
        db,
        title="Legacy Shared Read",
        isbn="9780000000040",
        owned=1,
        reading_status="read",
    )
    db.commit()

    _as(client, viewer_user)
    response = client.get("/api/search?reading_status=read")

    assert response.status_code == 200
    assert "Legacy Shared Read" not in response.text


def test_wishlist_filter_and_badge_are_personal_while_owned_stays_shared(
    db, client, viewer_user, admin_user
):
    item_id = _insert_item(
        db,
        title="Owned But Personally Wanted",
        isbn="9780000000057",
        owned=1,
        reading_status=None,
    )
    user_state.save_state(db, viewer_user["id"], item_id, wishlist=1)
    db.commit()

    _as(client, viewer_user)
    viewer_wishlist = client.get("/api/search?owned=0")
    assert viewer_wishlist.status_code == 200
    assert "Owned But Personally Wanted" in viewer_wishlist.text
    assert "Wishlist" in viewer_wishlist.text

    viewer_owned = client.get("/api/search?owned=1")
    assert "Owned But Personally Wanted" in viewer_owned.text

    _as(client, admin_user)
    admin_wishlist = client.get("/api/search?owned=0")
    assert admin_wishlist.status_code == 200
    assert "Owned But Personally Wanted" not in admin_wishlist.text

    admin_owned = client.get("/api/search?owned=1")
    assert "Owned But Personally Wanted" in admin_owned.text


def test_browse_first_paint_uses_personal_status_and_wishlist(
    db, client, viewer_user, admin_user
):
    item_id = _insert_item(
        db,
        title="First Paint Personal State",
        isbn="9780000000064",
        owned=1,
        reading_status="want_to_read",
    )
    user_state.set_reading_status(db, viewer_user["id"], item_id, "read")
    user_state.save_state(db, viewer_user["id"], item_id, wishlist=1)
    db.commit()

    _as(client, viewer_user)
    viewer = client.get("/browse")
    assert viewer.status_code == 200
    assert "First Paint Personal State" in viewer.text
    assert "Wishlist" in viewer.text
    assert "bg-shelf-success" in viewer.text

    _as(client, admin_user)
    admin = client.get("/browse")
    assert admin.status_code == 200
    assert "First Paint Personal State" in admin.text
    # A new account with no personal row must not inherit either the viewer's
    # Wishlist or the catalogue compatibility status.
    card_start = admin.text.index("First Paint Personal State")
    surrounding = admin.text[max(0, card_start - 1800):card_start + 1800]
    assert ">Wishlist<" not in surrounding
