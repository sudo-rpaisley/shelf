"""Tests for add-only physical comic and magazine issue ranges."""

import pytest

from app.services import bulk_issues, periodical_records
from app.services.item_write import insert_item


def _seed_comic(db, *, issue=1, location_id=None):
    return insert_item(
        db,
        title=f"Night Watch #{issue}",
        media_type="comic",
        series_name="Night Watch",
        series_position=float(issue),
        location_id=location_id,
        owned=1,
        source="test",
    )


def _seed_magazine(
    db,
    *,
    title="VW Motoring",
    issue_number="1",
    volume=None,
    location_id=None,
):
    item_id = insert_item(
        db,
        title=title,
        media_type="magazine",
        series_name=title,
        publisher="Test Publisher",
        language="en",
        location_id=location_id,
        owned=1,
        source="test",
    )
    publication_id = periodical_records.upsert_publication(
        db,
        title=title,
        publisher="Test Publisher",
        language="en",
    )
    periodical_records.link_issue(
        db,
        item_id=item_id,
        publication_id=publication_id,
        volume=volume,
        issue_number=issue_number,
    )
    return item_id, publication_id


def test_adds_physical_comic_issue_range(db):
    _seed_comic(db, issue=1)

    result = bulk_issues.add_comic_issue_range(
        db,
        series_name="Night Watch",
        first_issue=10,
        last_issue=12,
    )

    assert result.created == 3
    assert result.skipped == 0
    rows = db.execute(
        "SELECT title, series_position, owned FROM items "
        "WHERE series_name = 'Night Watch' AND series_position BETWEEN 10 AND 12 "
        "ORDER BY series_position"
    ).fetchall()
    assert [(r["title"], r["series_position"], r["owned"]) for r in rows] == [
        ("Night Watch #10", 10.0, 1),
        ("Night Watch #11", 11.0, 1),
        ("Night Watch #12", 12.0, 1),
    ]


def test_comic_range_skips_existing_positions(db):
    _seed_comic(db, issue=10)
    _seed_comic(db, issue=11)

    result = bulk_issues.add_comic_issue_range(
        db,
        series_name="Night Watch",
        first_issue=10,
        last_issue=12,
    )

    assert result.created == 1
    assert result.skipped == 2
    count = db.execute(
        "SELECT COUNT(*) FROM items WHERE series_name = 'Night Watch' "
        "AND series_position BETWEEN 10 AND 12"
    ).fetchone()[0]
    assert count == 3


def test_adds_magazine_range_with_publication_links_and_volume(db):
    _, publication_id = _seed_magazine(db, issue_number="1", volume="7")

    result = bulk_issues.add_magazine_issue_range(
        db,
        publication_id=publication_id,
        volume="7",
        first_issue=10,
        last_issue=12,
    )

    assert result.created == 3
    rows = db.execute(
        "SELECT i.title, i.series_position, pi.volume, pi.issue_number, pi.publication_id "
        "FROM items i JOIN periodical_issues pi ON pi.item_id = i.id "
        "WHERE pi.publication_id = ? AND pi.issue_number IN ('10','11','12') "
        "ORDER BY CAST(pi.issue_number AS INTEGER)",
        (publication_id,),
    ).fetchall()
    assert [(r["title"], r["series_position"], r["volume"], r["issue_number"]) for r in rows] == [
        ("VW Motoring", None, "7", "10"),
        ("VW Motoring", None, "7", "11"),
        ("VW Motoring", None, "7", "12"),
    ]
    assert {r["publication_id"] for r in rows} == {publication_id}


def test_magazine_range_allows_reused_numbers_in_different_volume(db):
    _, publication_id = _seed_magazine(db, issue_number="1", volume="1")

    result = bulk_issues.add_magazine_issue_range(
        db,
        publication_id=publication_id,
        volume="2",
        first_issue=1,
        last_issue=2,
    )

    assert result.created == 2
    assert result.skipped == 0
    rows = db.execute(
        "SELECT volume, issue_number FROM periodical_issues "
        "WHERE publication_id = ? AND issue_number = '1' ORDER BY volume",
        (publication_id,),
    ).fetchall()
    assert [(row["volume"], row["issue_number"]) for row in rows] == [
        ("1", "1"),
        ("2", "1"),
    ]


def test_magazine_range_skips_leading_zero_match_in_same_volume(db):
    _, publication_id = _seed_magazine(db, issue_number="011", volume="3")

    result = bulk_issues.add_magazine_issue_range(
        db,
        publication_id=publication_id,
        volume="3",
        first_issue=10,
        last_issue=12,
    )

    assert result.created == 2
    assert result.skipped == 1
    issue_numbers = {
        row["issue_number"]
        for row in db.execute(
            "SELECT issue_number FROM periodical_issues "
            "WHERE publication_id = ? AND volume = '3'",
            (publication_id,),
        ).fetchall()
    }
    assert issue_numbers == {"011", "10", "12"}


def test_magazine_range_targets_selected_publication(db):
    periodical_records.ensure_extended_schema(db)
    first_publication = db.execute(
        "INSERT INTO periodical_publications (title, issn, publisher, language) "
        "VALUES ('Shared Monthly', '1111-1111', 'One', 'en')"
    ).lastrowid
    selected_publication = db.execute(
        "INSERT INTO periodical_publications (title, issn, publisher, language) "
        "VALUES ('Shared Monthly', '2222-2222', 'Two', 'en')"
    ).lastrowid

    for publication_id in (first_publication, selected_publication):
        item_id = insert_item(
            db,
            title="Shared Monthly",
            media_type="magazine",
            series_name="Shared Monthly",
            owned=1,
            source="test",
        )
        periodical_records.link_issue(
            db,
            item_id=item_id,
            publication_id=publication_id,
            issue_number="1",
        )

    result = bulk_issues.add_magazine_issue_range(
        db,
        publication_id=selected_publication,
        first_issue=2,
        last_issue=2,
    )

    assert result.created == 1
    rows = db.execute(
        "SELECT publication_id FROM periodical_issues WHERE issue_number = '2'"
    ).fetchall()
    assert [row["publication_id"] for row in rows] == [selected_publication]


def test_new_comic_issues_inherit_granular_series_location(db):
    legacy_location_id = db.execute(
        "INSERT INTO locations (name) VALUES ('Living Room')"
    ).lastrowid
    first_id = _seed_comic(db, issue=1, location_id=legacy_location_id)
    second_id = _seed_comic(db, issue=2, location_id=legacy_location_id)

    root = db.execute(
        "SELECT id FROM location_nodes WHERE legacy_location_id = ?",
        (legacy_location_id,),
    ).fetchone()
    bookcase_id = db.execute(
        "INSERT INTO location_nodes (parent_id, name) VALUES (?, 'Bookcase')",
        (root["id"],),
    ).lastrowid
    shelf_id = db.execute(
        "INSERT INTO location_nodes (parent_id, name) VALUES (?, 'Shelf 1')",
        (bookcase_id,),
    ).lastrowid
    db.execute(
        "UPDATE item_copies SET location_id = ? WHERE item_id IN (?, ?)",
        (shelf_id, first_id, second_id),
    )

    bulk_issues.add_comic_issue_range(
        db,
        series_name="Night Watch",
        first_issue=3,
        last_issue=4,
    )

    rows = db.execute(
        "SELECT i.location_id AS legacy_location_id, c.location_id AS copy_location_id "
        "FROM items i JOIN item_copies c ON c.item_id = i.id AND c.is_primary = 1 "
        "WHERE i.series_name = 'Night Watch' AND i.series_position IN (3, 4)"
    ).fetchall()
    assert rows
    assert {row["legacy_location_id"] for row in rows} == {legacy_location_id}
    assert {row["copy_location_id"] for row in rows} == {shelf_id}


def test_new_magazine_issues_inherit_publication_location(db):
    legacy_location_id = db.execute(
        "INSERT INTO locations (name) VALUES ('Study')"
    ).lastrowid
    item_id, publication_id = _seed_magazine(
        db,
        issue_number="1",
        location_id=legacy_location_id,
    )
    root = db.execute(
        "SELECT id FROM location_nodes WHERE legacy_location_id = ?",
        (legacy_location_id,),
    ).fetchone()
    shelf_id = db.execute(
        "INSERT INTO location_nodes (parent_id, name) VALUES (?, 'Magazine Shelf')",
        (root["id"],),
    ).lastrowid
    db.execute(
        "UPDATE item_copies SET location_id = ? WHERE item_id = ?",
        (shelf_id, item_id),
    )

    bulk_issues.add_magazine_issue_range(
        db,
        publication_id=publication_id,
        first_issue=2,
        last_issue=2,
    )

    row = db.execute(
        "SELECT i.location_id AS legacy_location_id, c.location_id AS copy_location_id "
        "FROM periodical_issues pi JOIN items i ON i.id = pi.item_id "
        "JOIN item_copies c ON c.item_id = i.id AND c.is_primary = 1 "
        "WHERE pi.publication_id = ? AND pi.issue_number = '2'",
        (publication_id,),
    ).fetchone()
    assert row["legacy_location_id"] == legacy_location_id
    assert row["copy_location_id"] == shelf_id


def test_reversed_issue_range_is_rejected_without_writes(db):
    _seed_comic(db, issue=1)
    before = db.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    with pytest.raises(ValueError, match="must not be higher"):
        bulk_issues.add_comic_issue_range(
            db,
            series_name="Night Watch",
            first_issue=90,
            last_issue=10,
        )

    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == before


def test_issue_range_is_capped_at_500(db):
    _seed_comic(db, issue=1)

    with pytest.raises(ValueError, match="at most 500"):
        bulk_issues.add_comic_issue_range(
            db,
            series_name="Night Watch",
            first_issue=1,
            last_issue=501,
        )


def test_editor_comic_route_adds_range_and_reports_skips(editor_client, db):
    _seed_comic(db, issue=10)
    db.execute("COMMIT")

    response = editor_client.post(
        "/api/series/bulk-add-issues",
        data={
            "name": "Night Watch",
            "media_type": "comic",
            "from_issue": "10",
            "to_issue": "12",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "bulk_added=2" in response.headers["location"]
    assert "bulk_skipped=1" in response.headers["location"]
    html = editor_client.get(response.headers["location"]).text
    assert "Added 2 issues" in html
    assert "Skipped 1 already in Shelf" in html


def test_viewer_cannot_bulk_add_comic_range(viewer_client, db):
    _seed_comic(db, issue=1)
    db.execute("COMMIT")

    response = viewer_client.post(
        "/api/series/bulk-add-issues",
        data={
            "name": "Night Watch",
            "media_type": "comic",
            "from_issue": "2",
            "to_issue": "3",
        },
        follow_redirects=False,
    )

    assert response.status_code == 403
    detail = viewer_client.get(
        "/series/detail?name=Night%20Watch&media_type=comic"
    ).text
    assert 'data-testid="bulk-issue-form"' not in detail


def test_comic_bulk_form_is_shown_to_editor(editor_client, db):
    _seed_comic(db, issue=1)
    db.execute("COMMIT")
    url = "/series/detail?name=Night%20Watch&media_type=comic"

    assert 'data-testid="bulk-issue-form"' in editor_client.get(url).text


def test_editor_magazine_route_adds_volume_range_and_reports_skips(editor_client, db):
    _, publication_id = _seed_magazine(db, issue_number="1", volume="4")
    db.execute("COMMIT")

    response = editor_client.post(
        f"/api/magazines/publications/{publication_id}/bulk-add-issues",
        data={"volume": "4", "from_issue": "1", "to_issue": "3"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith(
        f"/magazines/publications/{publication_id}?"
    )
    assert "bulk_added=2" in response.headers["location"]
    assert "bulk_skipped=1" in response.headers["location"]
    html = editor_client.get(response.headers["location"]).text
    assert "Added 2 issues" in html
    assert "Skipped 1 already in this publication and volume" in html


def test_viewer_cannot_bulk_add_magazine_range_and_form_is_hidden(viewer_client, db):
    _, publication_id = _seed_magazine(db, issue_number="1")
    db.execute("COMMIT")

    response = viewer_client.post(
        f"/api/magazines/publications/{publication_id}/bulk-add-issues",
        data={"from_issue": "2", "to_issue": "3"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    html = viewer_client.get(f"/magazines/publications/{publication_id}").text
    assert 'data-testid="magazine-bulk-form"' not in html


def test_magazine_bulk_form_is_shown_to_editor(editor_client, db):
    _, publication_id = _seed_magazine(db, issue_number="1")
    db.execute("COMMIT")

    html = editor_client.get(f"/magazines/publications/{publication_id}").text
    assert 'data-testid="magazine-bulk-form"' in html
    assert 'data-testid="magazine-bulk-volume"' in html


def test_magazine_series_detail_does_not_offer_bulk_form(editor_client, db):
    _seed_magazine(db, issue_number="1")
    db.execute("COMMIT")

    html = editor_client.get(
        "/series/detail?name=VW%20Motoring&media_type=magazine"
    ).text
    assert 'data-testid="bulk-issue-form"' not in html
