"""Library visibility and personal-state regressions for Home and Stats."""

from app.services import home_dashboard, libraries, user_state
from tests.conftest import _insert_item


def _private_item(db, *, title: str, isbn: str, **fields):
    library = libraries.create_library(db, f"Private {title}")
    item_id = _insert_item(
        db,
        title=title,
        isbn=isbn,
        _library_id=library["id"],
        **fields,
    )
    return library, item_id


def test_home_summary_uses_accessible_catalogue_and_personal_wishlist(
    db, viewer_user
):
    wanted = _insert_item(
        db,
        title="Visible Personal Wishlist",
        isbn="9780000013107",
        owned=1,
    )
    shared_unowned = _insert_item(
        db,
        title="Visible Legacy Unowned",
        isbn="9780000013114",
        owned=0,
    )
    _, hidden = _private_item(
        db,
        title="Hidden Personal Wishlist",
        isbn="9780000013121",
        owned=1,
    )
    user_state.save_state(db, viewer_user["id"], wanted, wishlist=1)
    user_state.save_state(db, viewer_user["id"], hidden, wishlist=1)

    summary = home_dashboard.dashboard_summary(db, user=viewer_user)

    assert summary["total_count"] == 2
    assert summary["owned_count"] == 1
    assert summary["wishlist_count"] == 1
    assert [row["id"] for row in summary["recent_items"] if row["wishlist"]] == [wanted]
    assert hidden not in {row["id"] for row in summary["recent_items"]}
    # Shared owned=0 is no longer a person's Wishlist.
    assert shared_unowned not in {
        row["id"] for row in summary["recent_items"] if row["wishlist"]
    }


def test_home_page_never_renders_hidden_recent_item(db, viewer_client):
    _insert_item(db, title="Visible Home Item", isbn="9780000013138")
    _private_item(db, title="Secret Home Item", isbn="9780000013145")
    db.commit()

    response = viewer_client.get("/")

    assert response.status_code == 200
    assert "Visible Home Item" in response.text
    assert "Secret Home Item" not in response.text


def test_stats_scopes_catalogue_rows_and_uses_personal_reading_state(
    db, viewer_client, viewer_user
):
    visible = _insert_item(
        db,
        title="Visible Stats Book",
        isbn="9780000013152",
        authors="Visible Author",
        estimated_value=12.0,
        # Deliberately contradictory legacy state: personal state is authoritative.
        reading_status="read",
        date_finished="2098-01-01",
    )
    _, hidden = _private_item(
        db,
        title="Secret Stats Book",
        isbn="9780000013169",
        authors="Secret Author",
        estimated_value=9999.0,
    )
    user_state.save_state(
        db,
        viewer_user["id"],
        visible,
        reading_status="read",
        date_finished="2024-04-02",
    )
    user_state.save_state(
        db,
        viewer_user["id"],
        hidden,
        reading_status="read",
        date_finished="2099-04-02",
        wishlist=1,
    )
    db.commit()

    response = viewer_client.get("/stats")

    assert response.status_code == 200
    assert "Visible Stats Book" in response.text
    assert "Secret Stats Book" not in response.text
    assert ">Visible Author<" in response.text
    assert "Secret Author" not in response.text
    assert "2024" in response.text
    assert "2098" not in response.text
    assert "2099" not in response.text
    assert "$12" in response.text
    assert "$9,999" not in response.text


def test_non_admin_stats_do_not_leak_global_valuation_history(
    db, viewer_client, admin_client
):
    db.execute(
        "INSERT INTO valuation_history (total_value, priced_count, created_at) "
        "VALUES (123456, 2, '2026-01-01 00:00:00')"
    )
    db.execute(
        "INSERT INTO valuation_history (total_value, priced_count, created_at) "
        "VALUES (654321, 3, '2026-02-01 00:00:00')"
    )
    db.commit()

    viewer_html = viewer_client.get("/stats").text
    admin_html = admin_client.get("/stats").text

    assert "snapshots cover the whole catalogue" in viewer_html
    assert "snapshots cover the whole catalogue" not in admin_html
    assert "Run batch valuations" not in admin_html


def test_admin_home_and_stats_keep_global_catalogue_recovery_visibility(
    db, admin_client
):
    _private_item(
        db,
        title="Admin Recovery Dashboard Item",
        isbn="9780000013176",
    )
    db.commit()

    assert "Admin Recovery Dashboard Item" in admin_client.get("/").text
    assert "Admin Recovery Dashboard Item" in admin_client.get("/stats").text
