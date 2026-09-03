"""Product coverage for the operational Home dashboard."""


def test_home_loads_dashboard_after_first_paint(viewer_client, db):
    html = viewer_client.get("/").text

    assert 'hx-get="/api/home/dashboard"' in html
    assert 'data-testid="home-dashboard-loading"' in html


def test_dashboard_reports_library_health_and_locations(viewer_client, db):
    living = db.execute(
        "INSERT INTO locations (name, sort_order) VALUES ('Living Room', 0)"
    ).lastrowid
    db.execute(
        "INSERT INTO items (title, media_type, source, owned, location_id, cover_path) "
        "VALUES ('Located DVD', 'dvd', 'test', 1, ?, 'covers/dvd.jpg')",
        (living,),
    )
    # Physical and owned: both missing a cover and a location, but should count
    # only once in the overall needs-attention total.
    db.execute(
        "INSERT INTO items (title, media_type, source, owned) "
        "VALUES ('Unlocated Book', 'book', 'test', 1)"
    )
    # Digital holdings never require a physical location.
    db.execute(
        "INSERT INTO items (title, media_type, source, owned, cover_path) "
        "VALUES ('Digital Book', 'ebook', 'test', 1, 'covers/ebook.jpg')"
    )
    db.execute(
        "INSERT INTO items (title, media_type, source, owned) "
        "VALUES ('Wishlist Book', 'book', 'test', 0)"
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
