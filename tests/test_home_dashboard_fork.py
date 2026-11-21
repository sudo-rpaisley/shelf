"""Product coverage for the operational Home dashboard."""

from tests.conftest import _insert_item


def test_home_loads_dashboard_after_first_paint(viewer_client, db):
    html = viewer_client.get("/").text

    assert 'hx-get="/api/home/dashboard"' in html
    assert 'data-testid="home-dashboard-loading"' in html


def test_dashboard_reports_library_health_and_locations(viewer_client, db):
    living = db.execute(
        "INSERT INTO locations (name, sort_order) VALUES ('Living Room', 0)"
    ).lastrowid
    _insert_item(
        db,
        title="Located DVD",
        isbn=None,
        media_type="dvd",
        owned=1,
        location_id=living,
        cover_path="covers/dvd.jpg",
    )
    # Physical and owned: both missing a cover and a location, but should count
    # only once in the overall needs-attention total.
    _insert_item(
        db,
        title="Unlocated Book",
        isbn="9780000020001",
        media_type="book",
        owned=1,
    )
    # Digital holdings never require a physical location.
    _insert_item(
        db,
        title="Digital Book",
        isbn="9780000020002",
        media_type="ebook",
        owned=1,
        cover_path="covers/ebook.jpg",
    )
    _insert_item(
        db,
        title="Wishlist Book",
        isbn="9780000020003",
        media_type="book",
        owned=0,
    )
    db.commit()

    response = viewer_client.get("/api/home/dashboard")
    assert response.status_code == 200
    html = response.text

    assert 'data-testid="home-dashboard"' in html
    assert "Owned" in html and ">3<" in html
    assert "Wishlist" in html and ">1<" in html
    assert "Needs attention" in html
    assert "Missing covers" in html
    assert "Physical items without a location" in html
    assert "Living Room" in html
    assert 'href="/browse?location_filter=' in html