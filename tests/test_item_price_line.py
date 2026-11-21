"""Item detail: the wishlist list-price line under the title (price alerts).

Every arm of the template's condition is reached here (G65): wishlisted or
not, a priced observation or not, the feature on or off, and the `was` half
present or absent.
"""
import re
from html import unescape

from app.currency import invalidate_cache
from app.database import get_db
from app.features import set_feature_enabled
from tests.conftest import _insert_item

_LINE = re.compile(r'<p[^>]*data-testid="list-price-line"[^>]*>(.*?)</p>', re.S)


def _obs(db, item_id, price, day):
    db.execute(
        "INSERT INTO price_history (item_id, price, observed_at) VALUES (?, ?, ?)",
        (item_id, price, f"{day} 03:00:00"),
    )


def _line(client, item_id):
    m = _LINE.search(client.get(f"/item/{item_id}").text)
    return re.sub(r"\s+", " ", unescape(m.group(1))).strip() if m else None


def _wish(db, title="Dune"):
    return _insert_item(db, title=title, isbn="9780441013593", owned=0, wishlisted=True)


def test_one_priced_observation_shows_price_without_was(viewer_client, db):
    item_id = _wish(db)
    _obs(db, item_id, 12.99, "2026-09-02")
    db.commit()
    assert _line(viewer_client, item_id) == "List price $12.99"


def test_earlier_different_price_shows_was_and_its_date(viewer_client, db):
    item_id = _wish(db)
    _obs(db, item_id, 12.99, "2026-09-02")
    _obs(db, item_id, 8.49, "2026-09-20")
    db.commit()
    assert _line(viewer_client, item_id) == "List price $8.49 · was $12.99 on 2026-09-02"


def test_price_with_a_later_miss_is_dated(viewer_client, db):
    """test-drive Obs 3: a later lookup found no price, so the line must not
    present the older one as current."""
    item_id = _wish(db)
    _obs(db, item_id, 12.99, "2026-09-02")
    _obs(db, item_id, 8.49, "2026-09-20")
    _obs(db, item_id, None, "2026-10-01")
    db.commit()
    assert _line(viewer_client, item_id) == (
        "List price $8.49 as of 2026-09-20 · was $12.99 on 2026-09-02")


def test_earlier_equal_price_shows_no_was(viewer_client, db):
    item_id = _wish(db)
    _obs(db, item_id, 8.49, "2026-09-02")
    _obs(db, item_id, 8.49, "2026-09-20")
    db.commit()
    assert _line(viewer_client, item_id) == "List price $8.49"


def test_was_skips_equal_rows_to_the_last_different_one(viewer_client, db):
    item_id = _wish(db)
    _obs(db, item_id, 15.00, "2026-08-01")
    _obs(db, item_id, 8.49, "2026-09-02")
    _obs(db, item_id, 8.49, "2026-09-20")
    db.commit()
    assert _line(viewer_client, item_id) == "List price $8.49 · was $15.00 on 2026-08-01"


def test_null_observations_are_skipped_for_both_halves(viewer_client, db):
    item_id = _wish(db)
    _obs(db, item_id, 12.99, "2026-09-01")
    _obs(db, item_id, None, "2026-09-05")
    _obs(db, item_id, 9.99, "2026-09-10")
    _obs(db, item_id, None, "2026-09-20")
    db.commit()
    # The trailing miss dates the current half (test-drive Obs 3).
    assert _line(viewer_client, item_id) == (
        "List price $9.99 as of 2026-09-10 · was $12.99 on 2026-09-01")


def test_only_null_observations_render_no_line(viewer_client, db):
    item_id = _wish(db)
    _obs(db, item_id, None, "2026-09-20")
    db.commit()
    assert _line(viewer_client, item_id) is None


def test_wishlisted_without_observations_renders_no_line(viewer_client, db):
    item_id = _wish(db)
    db.commit()
    assert _line(viewer_client, item_id) is None


def test_not_wishlisted_with_observations_renders_no_line(viewer_client, db):
    item_id = _insert_item(db, title="Owned", isbn="9780141439587", owned=1)
    _obs(db, item_id, 12.99, "2026-09-02")
    db.commit()
    assert _line(viewer_client, item_id) is None


def test_feature_off_renders_no_line(viewer_client, db):
    item_id = _wish(db)
    _obs(db, item_id, 12.99, "2026-09-02")
    db.commit()
    assert _line(viewer_client, item_id) is not None
    with get_db() as conn:
        set_feature_enabled(conn, "price_alerts", False)
    assert _line(viewer_client, item_id) is None


def test_money_uses_the_display_currency(viewer_client, db):
    item_id = _wish(db)
    _obs(db, item_id, 12.99, "2026-09-02")
    _obs(db, item_id, 8.49, "2026-09-20")
    db.execute("INSERT INTO settings (key, value) VALUES ('currency', 'SEK')")
    db.commit()
    invalidate_cache()
    assert _line(viewer_client, item_id) == "List price 8.49 kr · was 12.99 kr on 2026-09-02"
