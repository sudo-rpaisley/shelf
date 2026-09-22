"""Aggregates that read a child table without joining the items
relation, so a trashed item was still counted — the plan-soft-delete-
collisions T13 fix:

- `app.services.tags.get_all_tags` (both statements — unscoped and
  `media_type`-scoped)
- the Browse "Lent Out" filter badge, `app/routers/pages.py::browse`
- the Home dashboard "Lent out" tile, `app.services.home_dashboard.dashboard_summary`
- the `/periodicals` issue count, `app/routers/periodicals.py::periodicals_page`
  (soft-delete-trash T4, D7)

Every seed here trashes/restores through `item_write.trash_item` /
`restore_item` directly (the faster, more explicit seed) — and
every assertion is a DELTA (G34): read the count, trash one seeded row,
assert it dropped by exactly one, restore, assert it came back. Other
fixtures may already contribute to these counts, so an absolute assertion
would pass for the wrong reason.
"""

import re

from app.services import home_dashboard, tags as tags_svc
from app.services.item_write import insert_item, restore_item, trash_item
from tests.conftest import _insert_borrower


def _lend(db, item_id, borrower_id):
    db.execute(
        "INSERT INTO checkouts (item_id, borrower_id, checked_out) "
        "VALUES (?, ?, datetime('now'))",
        (item_id, borrower_id),
    )


class TestTagUsageCountUnscoped:
    """`get_all_tags(db)` — the `media_type=None` statement."""

    def test_trashing_the_tagged_item_drops_the_count_by_one_and_restoring_brings_it_back(self, db):
        item_id = insert_item(db, title="Tagged Book", source="test", media_type="book")
        tags_svc.attach_tags(db, item_id, ["signed"])
        tag_id = tags_svc.get_or_create_tag(db, "signed")

        before = {row["id"]: row["count"] for row in tags_svc.get_all_tags(db)}
        assert before[tag_id] == 1

        trash_item(db, item_id)
        after_trash = {row["id"]: row["count"] for row in tags_svc.get_all_tags(db)}
        assert after_trash[tag_id] == before[tag_id] - 1

        restore_item(db, item_id)
        after_restore = {row["id"]: row["count"] for row in tags_svc.get_all_tags(db)}
        assert after_restore[tag_id] == before[tag_id]

    def test_a_tag_whose_only_item_is_trashed_still_lists_with_zero_count(self, db):
        item_id = insert_item(db, title="Solo Tagged Item", source="test", media_type="book")
        tags_svc.attach_tags(db, item_id, ["only-here"])
        tag_id = tags_svc.get_or_create_tag(db, "only-here")

        trash_item(db, item_id)

        rows = {row["id"]: row["count"] for row in tags_svc.get_all_tags(db)}
        assert tag_id in rows
        assert rows[tag_id] == 0


class TestTagUsageCountScoped:
    """`get_all_tags(db, media_type=...)` — the second statement, reached
    only when a caller passes `media_type` (e.g. `suggestions_for`)."""

    def test_trashing_the_tagged_item_drops_the_scoped_count_by_one_and_restoring_brings_it_back(self, db):
        item_id = insert_item(db, title="Scoped Tagged Book", source="test", media_type="book")
        tags_svc.attach_tags(db, item_id, ["book-only"])
        tag_id = tags_svc.get_or_create_tag(db, "book-only")

        before = {row["id"]: row["count"] for row in tags_svc.get_all_tags(db, media_type="book")}
        assert before[tag_id] == 1

        trash_item(db, item_id)
        after_trash = {
            row["id"]: row["count"] for row in tags_svc.get_all_tags(db, media_type="book")
        }
        assert after_trash[tag_id] == before[tag_id] - 1

        restore_item(db, item_id)
        after_restore = {
            row["id"]: row["count"] for row in tags_svc.get_all_tags(db, media_type="book")
        }
        assert after_restore[tag_id] == before[tag_id]

    def test_scoped_tag_whose_only_item_is_trashed_still_lists_with_zero_count(self, db):
        item_id = insert_item(db, title="Solo Scoped Item", source="test", media_type="book")
        tags_svc.attach_tags(db, item_id, ["book-solo"])
        tag_id = tags_svc.get_or_create_tag(db, "book-solo")

        trash_item(db, item_id)

        rows = {row["id"]: row["count"] for row in tags_svc.get_all_tags(db, media_type="book")}
        assert tag_id in rows
        assert rows[tag_id] == 0


class TestHomeDashboardLentOutCount:
    """`home_dashboard.dashboard_summary` — called directly, since the count
    is already computed by this service function (no separate route logic
    to duplicate)."""

    def test_trashing_a_lent_item_drops_the_count_by_one_and_restoring_brings_it_back(self, db):
        item_id = insert_item(db, title="Lent Home Item", source="test", media_type="book")
        borrower_id = _insert_borrower(db, "Home Borrower")
        _lend(db, item_id, borrower_id)

        before = home_dashboard.dashboard_summary(db)["lent_out_count"]
        assert before >= 1

        trash_item(db, item_id)
        after_trash = home_dashboard.dashboard_summary(db)["lent_out_count"]
        assert after_trash == before - 1

        restore_item(db, item_id)
        after_restore = home_dashboard.dashboard_summary(db)["lent_out_count"]
        assert after_restore == before


class TestBrowseLentOutBadge:
    """`app/routers/pages.py::browse` — the count is inline SQL in the
    route, not a separate service function, so this hits the route through
    `admin_client` and reads the rendered "Lent Out (N)" badge in
    `browse.html`, rather than duplicating the query."""

    @staticmethod
    def _badge_count(html: str) -> int:
        import re

        match = re.search(r"Lent Out \((\d+)\)", html)
        assert match, "Lent Out badge with a count did not render"
        return int(match.group(1))

    def test_trashing_a_lent_item_drops_the_badge_by_one_and_restoring_brings_it_back(self, db, admin_client):
        # A second, untouched lent item keeps the badge above zero throughout,
        # since browse.html only renders the "(N)" suffix when N is truthy.
        control_id = insert_item(db, title="Control Lent Item", source="test", media_type="book")
        item_id = insert_item(db, title="Lent Browse Item", source="test", media_type="book")
        borrower_id = _insert_borrower(db, "Browse Borrower")
        _lend(db, control_id, borrower_id)
        _lend(db, item_id, borrower_id)
        db.commit()

        before = self._badge_count(admin_client.get("/browse").text)
        assert before >= 2

        trash_item(db, item_id)
        db.commit()
        after_trash = self._badge_count(admin_client.get("/browse").text)
        assert after_trash == before - 1

        restore_item(db, item_id)
        db.commit()
        after_restore = self._badge_count(admin_client.get("/browse").text)
        assert after_restore == before


class TestPeriodicalIssueCount:
    """`/periodicals` counts a publication's issues through `items_live`
    (soft-delete-trash T4, D7): a trashed issue drops out, and a publication
    with no issues still lists at 0."""

    def _publication(self, db, title):
        return db.execute(
            "INSERT INTO periodical_publications (title) VALUES (?)", (title,)
        ).lastrowid

    def _issue(self, db, pub_id, title):
        item_id = insert_item(db, title=title, source="test", media_type="magazine")
        db.execute(
            "INSERT INTO periodical_issues (item_id, publication_id) VALUES (?, ?)",
            (item_id, pub_id),
        )
        return item_id

    def test_a_trashed_issue_is_not_counted(self, admin_client, db):
        pub = self._publication(db, "Zine Weekly")
        self._issue(db, pub, "Zine Weekly #1")
        second = self._issue(db, pub, "Zine Weekly #2")
        self._publication(db, "Empty Quarterly")
        db.commit()
        html = admin_client.get("/periodicals").text
        assert "2 issues" in html and "0 issues" in html

        trash_item(db, second)
        db.commit()
        html = admin_client.get("/periodicals").text
        assert re.search(r"\b1 issue(?!s)", html)
        assert "2 issues" not in html
        assert "0 issues" in html
