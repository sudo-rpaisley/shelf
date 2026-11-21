"""T2-T4 — a turned-off feature's controls disappear from the pages that
stay on. Each absence pin has an on/off twin (G108) and names an element or
data-testid, never a bare label (G69). Seeds commit before the request
(G48); flags are flipped with `off(...)` from tests/test_features_gate.py.

This file currently holds T2 (the item page) and T3 (Home, Browse, Trash and
Stats). T4 appends its own class here per the plan.
"""
from app.services import item_write
from tests.conftest import _insert_item, _insert_borrower
from tests.test_features_gate import off


def _seed_full_item(db):
    """The item T2's guard table needs all at once: an ISBN book in a
    series with a gap sibling, a Hardcover series total, an ABS link of its
    own, an estimated value, and an open loan. Returns (item_id, sibling_id).
    """
    borrower = _insert_borrower(db)
    item_id = _insert_item(
        db, title="Sweep Full Book", isbn="9789000091017", media_type="book",
        series_name="Sweep Saga", series_position=1, owned=1,
        estimated_value=42.5, abs_id="li_sweep1",
    )
    sibling_id = _insert_item(
        db, title="Sweep Sibling Three", isbn="9789000091024", media_type="book",
        series_name="Sweep Saga", series_position=3, owned=1,
    )
    db.execute("INSERT INTO series_meta (name, hc_total) VALUES ('Sweep Saga', 9)")
    db.execute(
        "INSERT INTO checkouts (item_id, borrower_id, checked_out) VALUES (?, ?, datetime('now'))",
        (item_id, borrower),
    )
    db.execute("INSERT INTO settings (key, value) VALUES ('hardcover_token', 'hc-token')")
    db.execute("INSERT INTO settings (key, value) VALUES ('abs_url', 'https://abs.example')")
    db.commit()
    return item_id, sibling_id


def _seed_manual_value_item(db):
    item_id = _insert_item(
        db, title="Sweep Manual Value Book", isbn="9789000091031", media_type="book",
        manual_value=15,
    )
    db.commit()
    return item_id


def _seed_also_in_abs_pair(db):
    """A physical item with no abs_id of its own, linked to a digital item
    that has one — the "Also in Audiobookshelf" arm, distinct from the
    Listen/Read arm (which fires on an item's *own* abs_id)."""
    physical = _insert_item(
        db, title="Sweep Also-In Physical", isbn="9789000091048", media_type="book",
        authors="Sweep Author",
    )
    digital = _insert_item(
        db, title="Sweep Also-In Physical", isbn="9789000091055", media_type="audiobook",
        authors="Sweep Author", abs_id="li_sweep2",
    )
    db.execute(
        "INSERT INTO item_links (item_a_id, item_b_id) VALUES (?, ?)",
        (min(physical, digital), max(physical, digital)),
    )
    db.commit()
    return physical


class TestLendingOnItemPage:
    def test_lending_off_removes_the_panel_and_both_forms(self, editor_client, db):
        lent_id, unlent_id = _seed_full_item(db)
        off("lending")

        html_lent = editor_client.get(f"/item/{lent_id}").text
        html_unlent = editor_client.get(f"/item/{unlent_id}").text

        assert 'action="/api/checkouts/' not in html_lent
        assert f'action="/api/items/{unlent_id}/checkout"' not in html_unlent
        assert ">Lending</p>" not in html_lent
        assert ">Lending</p>" not in html_unlent

    def test_lending_on_keeps_the_panel_and_both_forms(self, editor_client, db):
        lent_id, unlent_id = _seed_full_item(db)

        html_lent = editor_client.get(f"/item/{lent_id}").text
        html_unlent = editor_client.get(f"/item/{unlent_id}").text

        assert 'action="/api/checkouts/' in html_lent
        assert f'action="/api/items/{unlent_id}/checkout"' in html_unlent
        assert ">Lending</p>" in html_lent
        assert ">Lending</p>" in html_unlent


class TestSeriesOnItemPage:
    def test_series_off_shows_plain_text_no_link_no_progress(self, editor_client, db):
        item_id, _ = _seed_full_item(db)
        off("series")

        html = editor_client.get(f"/item/{item_id}").text

        assert 'data-testid="series-name"' in html
        assert "Sweep Saga" in html
        assert 'href="/series"' not in html
        assert 'data-testid="series-progress"' not in html

    def test_series_on_shows_link_and_progress(self, editor_client, db):
        item_id, _ = _seed_full_item(db)

        html = editor_client.get(f"/item/{item_id}").text

        assert 'href="/series"' in html
        assert 'data-testid="series-progress"' in html


class TestHardcoverOnItemPage:
    def test_hardcover_off_series_on_hides_push_and_series_total(self, editor_client, db):
        item_id, _ = _seed_full_item(db)
        off("hardcover")

        html = editor_client.get(f"/item/{item_id}").text

        assert 'x-data="hardcoverPush"' not in html
        assert 'data-testid="series-hc-total"' not in html

    def test_hardcover_on_shows_push_and_series_total(self, editor_client, db):
        item_id, _ = _seed_full_item(db)

        html = editor_client.get(f"/item/{item_id}").text

        assert 'x-data="hardcoverPush"' in html
        assert 'data-testid="series-hc-total"' in html


class TestValuationOnItemPage:
    def test_valuation_off_hides_estimated_but_keeps_manual(self, editor_client, db):
        estimated_id, _ = _seed_full_item(db)
        manual_id = _seed_manual_value_item(db)
        off("valuation")

        html_estimated = editor_client.get(f"/item/{estimated_id}").text
        html_manual = editor_client.get(f"/item/{manual_id}").text

        assert 'data-testid="estimated-value"' not in html_estimated
        assert "(manual)" in html_manual
        assert "15" in html_manual

    def test_valuation_on_shows_estimated_value(self, editor_client, db):
        estimated_id, _ = _seed_full_item(db)

        html = editor_client.get(f"/item/{estimated_id}").text

        assert 'data-testid="estimated-value"' in html


class TestAbsSyncOnItemPage:
    def test_abs_sync_off_hides_playback_and_also_in(self, editor_client, db):
        item_id, _ = _seed_full_item(db)
        also_in_id = _seed_also_in_abs_pair(db)
        off("abs_sync")

        html_playback = editor_client.get(f"/item/{item_id}").text
        html_also_in = editor_client.get(f"/item/{also_in_id}").text

        assert 'data-testid="abs-playback"' not in html_playback
        assert 'data-testid="abs-also-in"' not in html_also_in

    def test_abs_sync_on_shows_playback_and_also_in(self, editor_client, db):
        item_id, _ = _seed_full_item(db)
        also_in_id = _seed_also_in_abs_pair(db)

        html_playback = editor_client.get(f"/item/{item_id}").text
        html_also_in = editor_client.get(f"/item/{also_in_id}").text

        assert 'data-testid="abs-playback"' in html_playback
        assert 'data-testid="abs-also-in"' in html_also_in


class TestRommKomgaScriptsOnItemPage:
    def test_romm_off_drops_its_script_tag(self, editor_client, db):
        item_id, _ = _seed_full_item(db)
        off("romm")

        html = editor_client.get(f"/item/{item_id}").text

        assert "romm-item.js" not in html
        assert "komga-item.js" in html

    def test_komga_off_drops_its_script_tag(self, editor_client, db):
        item_id, _ = _seed_full_item(db)
        off("komga")

        html = editor_client.get(f"/item/{item_id}").text

        assert "komga-item.js" not in html
        assert "romm-item.js" in html

    def test_both_on_carries_both_script_tags(self, editor_client, db):
        item_id, _ = _seed_full_item(db)

        html = editor_client.get(f"/item/{item_id}").text

        assert '<script src="/static/js/romm-item.js"></script>' in html
        assert '<script src="/static/js/komga-item.js"></script>' in html


# --- T3: Home, Browse, Trash and Stats ---------------------------------------


def _seed_browse_items(db):
    """A lent item (loan badge/dot) and a source='romm' item (the RomM card
    action) -- /browse renders both the grid and list markup in one response
    (Alpine's x-if picks which is shown client-side; the server has no
    separate ?view=list branch), so one seed covers both surfaces.
    """
    borrower = _insert_borrower(db)
    lent_id = _insert_item(db, title="Browse Lent Item", isbn="9789000091062")
    db.execute(
        "INSERT INTO checkouts (item_id, borrower_id, checked_out) VALUES (?, ?, datetime('now'))",
        (lent_id, borrower),
    )
    romm_id = _insert_item(db, title="Browse RomM Item", isbn="9789000091079", source="romm")
    db.commit()
    return lent_id, romm_id


def _seed_trashed_lent_item(db, title, isbn):
    item_id = _insert_item(db, title=title, isbn=isbn)
    borrower = _insert_borrower(db)
    db.execute(
        "INSERT INTO checkouts (item_id, borrower_id, checked_out) VALUES (?, ?, datetime('now'))",
        (item_id, borrower),
    )
    item_write.trash_item(db, item_id)
    return item_id


class TestHomePage:
    def test_stats_series_lending_off_hide_their_links(self, editor_client, db):
        off("stats", "series", "lending")

        html = editor_client.get("/").text

        assert 'href="/stats"' not in html
        assert 'href="/series"' not in html
        assert 'data-testid="home-lent-out"' not in html

    def test_stats_series_lending_on_show_their_links(self, editor_client, db):
        html = editor_client.get("/").text

        assert 'href="/stats"' in html
        assert 'href="/series"' in html
        assert 'data-testid="home-lent-out"' in html


class TestHomeSummaryGrid:
    """Five tiles with Lending off fill whole rows: five columns at lg, and
    the last tile spans two columns below it."""

    @staticmethod
    def _tag(html, testid):
        start = html.rindex("<div", 0, html.index(f'data-testid="{testid}"'))
        return html[start:html.index(">", start)]

    def test_lending_off_fits_five_tiles(self, editor_client, db):
        off("lending")
        html = editor_client.get("/").text
        assert "lg:grid-cols-5" in self._tag(html, "home-summary")
        assert "col-span-2 lg:col-span-1" in self._tag(html, "home-media-types")

    def test_lending_on_keeps_six_columns(self, editor_client, db):
        html = editor_client.get("/").text
        assert "lg:grid-cols-6" in self._tag(html, "home-summary")
        assert "col-span-2" not in self._tag(html, "home-media-types")


class TestBrowsePage:
    def test_lending_and_romm_off_hide_controls_grid_view(self, editor_client, db):
        _seed_browse_items(db)
        off("lending", "romm")

        html = editor_client.get("/browse").text

        assert 'data-testid="loan-badge"' not in html
        assert 'data-testid="loan-dot"' not in html
        assert 'name="lent_out"' not in html
        assert 'data-testid="romm-card-action"' not in html

    def test_lending_and_romm_on_show_controls_grid_view(self, editor_client, db):
        _seed_browse_items(db)

        html = editor_client.get("/browse").text

        assert 'data-testid="loan-badge"' in html
        assert 'data-testid="loan-dot"' in html
        assert 'name="lent_out"' in html
        assert 'data-testid="romm-card-action"' in html

    def test_lending_and_romm_off_hide_controls_list_view(self, editor_client, db):
        _seed_browse_items(db)
        off("lending", "romm")

        html = editor_client.get("/browse?view=list").text

        assert 'data-testid="loan-badge"' not in html
        assert 'data-testid="loan-dot"' not in html
        assert 'name="lent_out"' not in html
        assert 'data-testid="romm-card-action"' not in html

    def test_lending_and_romm_on_show_controls_list_view(self, editor_client, db):
        _seed_browse_items(db)

        html = editor_client.get("/browse?view=list").text

        assert 'data-testid="loan-badge"' in html
        assert 'data-testid="loan-dot"' in html
        assert 'name="lent_out"' in html
        assert 'data-testid="romm-card-action"' in html

    def test_lent_out_bookmark_still_filters_with_lending_off(self, editor_client, db):
        lent_id, romm_id = _seed_browse_items(db)
        off("lending")

        response = editor_client.get("/browse?lent_out=1")

        assert response.status_code == 200
        html = response.text
        assert f'data-item-id="{lent_id}"' in html
        assert f'data-item-id="{romm_id}"' not in html


class TestTrashPage:
    def test_lending_off_hides_loaned_badge(self, editor_client, db):
        item_id = _seed_trashed_lent_item(db, "Trash Lent Off", "9789000091086")
        db.commit()
        off("lending")

        html = editor_client.get("/trash").text

        assert f'data-testid="trash-loaned-{item_id}"' not in html

    def test_lending_on_shows_loaned_badge(self, editor_client, db):
        item_id = _seed_trashed_lent_item(db, "Trash Lent On", "9789000091093")
        db.commit()

        html = editor_client.get("/trash").text

        assert f'data-testid="trash-loaned-{item_id}"' in html


class TestStatsPage:
    def test_valuation_off_hides_tile_and_chart(self, editor_client, db):
        db.execute("INSERT INTO valuation_history (total_value, priced_count) VALUES (100, 5)")
        db.execute("INSERT INTO valuation_history (total_value, priced_count) VALUES (150, 6)")
        db.commit()
        off("valuation")

        html = editor_client.get("/stats").text

        assert 'data-testid="stats-est-value"' not in html
        assert 'data-testid="chart-valuation"' not in html

    def test_valuation_on_shows_tile_and_chart(self, editor_client, db):
        db.execute("INSERT INTO valuation_history (total_value, priced_count) VALUES (100, 5)")
        db.execute("INSERT INTO valuation_history (total_value, priced_count) VALUES (150, 6)")
        db.commit()

        html = editor_client.get("/stats").text

        assert 'data-testid="stats-est-value"' in html
        assert 'data-testid="chart-valuation"' in html


# --- T4: Scan's modes ---------------------------------------------------------


class TestScanPage:
    def test_lending_off_carries_the_disabled_modes(self, editor_client, db):
        off("lending")

        html = editor_client.get("/scan").text

        assert 'data-off-modes="lend return"' in html

    def test_lending_on_carries_no_disabled_modes(self, editor_client, db):
        html = editor_client.get("/scan").text

        assert 'data-off-modes=""' in html
