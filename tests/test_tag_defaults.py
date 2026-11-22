"""Default tags at add time: the `GET /api/tags` suggestion source and the
`tags_svc.default_tags` block under which `insert_item` tags what it files,
in the item's own transaction."""

import contextvars
import re
from unittest.mock import AsyncMock, patch

import pytest

from app import config
from app.routers import items_catalog
from app.services import provider_result
from app.services import tags as tags_svc
from app.services.item_write import insert_item, trash_item
from tests.conftest import _insert_item
from tests.test_legacy_book import KRISTY_SUPPLEMENT, KRISTY_UPC
from tests.test_legacy_book_scan import KRISTY_ISBN13, KRISTY_UPC5


def _tag_rows(db):
    return [dict(r) for r in db.execute(
        "SELECT name, media_type FROM tags ORDER BY name COLLATE NOCASE"
    ).fetchall()]


def _item_tag_names(db, item_id):
    return [r["name"] for r in tags_svc.get_item_tags(db, item_id)]


class TestListTagsRoute:
    def test_editor_gets_starters_flagged_with_their_type(self, editor_client):
        resp = editor_client.get("/api/tags")
        assert resp.status_code == 200
        rows = resp.json()["tags"]
        # An empty library offers every type's starters, in config order.
        expected = [
            {"name": name, "media_type": media_type, "starter": True}
            for media_type, names in config.TAG_SUGGESTIONS.items()
            for name in names
        ]
        assert rows == expected

    def test_user_tags_first_with_their_scope(self, db, editor_client):
        tags_svc.get_or_create_tag(db, "signed")
        tags_svc.get_or_create_tag(db, "Anime", media_type="dvd")
        db.commit()
        rows = editor_client.get("/api/tags").json()["tags"]
        user = [r for r in rows if not r["starter"]]
        assert user == [
            {"name": "Anime", "media_type": "dvd", "starter": False},
            {"name": "signed", "media_type": None, "starter": False},
        ]
        # User rows precede every starter.
        first_starter = next(i for i, r in enumerate(rows) if r["starter"])
        assert all(not r["starter"] for r in rows[:first_starter])
        assert all(r["starter"] for r in rows[first_starter:])

    def test_scoped_tag_retires_that_types_starters_only(self, db, editor_client):
        tags_svc.get_or_create_tag(db, "Anime", media_type="dvd")
        db.commit()
        rows = editor_client.get("/api/tags").json()["tags"]
        starter_types = {r["media_type"] for r in rows if r["starter"]}
        assert "dvd" not in starter_types
        assert "book" in starter_types
        assert {"name": "Cookbook", "media_type": "book", "starter": True} in rows

    def test_starter_the_user_already_has_is_not_repeated(self, db, editor_client):
        tags_svc.get_or_create_tag(db, "cookbook")
        db.commit()
        rows = editor_client.get("/api/tags").json()["tags"]
        book_starters = [r["name"].casefold() for r in rows
                         if r["starter"] and r["media_type"] == "book"]
        assert "cookbook" not in book_starters

    def test_viewer_is_forbidden(self, viewer_client):
        assert viewer_client.get("/api/tags").status_code == 403

    def test_unauthenticated_is_sent_to_login(self, client, admin_user):
        # AuthMiddleware redirects before require_role runs, so an anonymous
        # request never reaches the route's own 401.
        client.cookies.delete("access_token")
        resp = client.get("/api/tags", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"


class TestDefaultTagsBlock:
    def test_insert_inside_the_block_is_tagged(self, db):
        with tags_svc.default_tags("Cookbook; Signed"):
            item_id = insert_item(db, title="Inside")
        db.commit()
        assert _item_tag_names(db, item_id) == ["Cookbook", "Signed"]

    def test_insert_outside_any_block_is_not_tagged(self, db):
        with tags_svc.default_tags("Cookbook"):
            pass
        item_id = insert_item(db, title="Outside")
        db.commit()
        assert _item_tag_names(db, item_id) == []
        assert _tag_rows(db) == []

    def test_restore_through_insert_item_is_tagged(self, db):
        isbn = "9780441013593"
        trashed = _insert_item(db, isbn=isbn, media_type="book")
        trash_item(db, trashed)
        with tags_svc.default_tags("Cookbook"):
            item_id = insert_item(db, title="Back", isbn=isbn, media_type="book")
        db.commit()
        assert item_id == trashed
        assert _item_tag_names(db, trashed) == ["Cookbook"]

    @pytest.mark.parametrize("raw", ["", " ; ;", None])
    def test_blank_raw_is_a_noop(self, db, raw):
        with tags_svc.default_tags(raw):
            insert_item(db, title="Blank")
        db.commit()
        assert _tag_rows(db) == []

    def test_unknown_name_created_once_global_and_reused(self, db):
        with tags_svc.default_tags("Cookbook"):
            first = insert_item(db, title="First")
        with tags_svc.default_tags("cookbook "):
            second = insert_item(db, title="Second")
        db.commit()
        assert _tag_rows(db) == [{"name": "Cookbook", "media_type": None}]
        assert _item_tag_names(db, first) == ["Cookbook"]
        assert _item_tag_names(db, second) == ["Cookbook"]

    def test_a_context_copied_inside_the_block_is_disarmed_on_exit(self, db):
        # A task spawned inside the block (cover enrichment) copies the context.
        with tags_svc.default_tags("Cookbook"):
            copied = contextvars.copy_context()
        item_id = copied.run(insert_item, db, title="Later")
        db.commit()
        assert _item_tag_names(db, item_id) == []


class TestTagWriteFailureLeavesNoPartialItem:
    """G118: the item and its default tags commit together or not at all, so
    a failed tag write leaves nothing behind and a retry files it whole."""

    def test_manual_add_rolls_back_the_item_and_retry_files_it_tagged(
            self, editor_client, db):
        form = {"title": "Atomic Manual", "tags": "Cookbook"}
        with patch.object(tags_svc, "attach_tags",
                          side_effect=RuntimeError("simulated tag write failure")):
            try:
                resp = editor_client.post("/api/items/manual", data=form)
                assert resp.status_code >= 500
            except RuntimeError:
                pass
        assert db.execute(
            "SELECT COUNT(*) FROM items WHERE title = 'Atomic Manual'"
        ).fetchone()[0] == 0

        resp = editor_client.post("/api/items/manual", data=form)
        assert 'data-scan-status="added"' in resp.text
        rows = db.execute(
            "SELECT id FROM items WHERE title = 'Atomic Manual'").fetchall()
        assert len(rows) == 1
        assert _item_tag_names(db, rows[0]["id"]) == ["Cookbook"]

    def test_scan_rolls_back_the_item_and_retry_files_it_tagged(self, admin_client, db):
        isbn = "9780441013593"
        form = {"isbn": isbn, "media_type": "book", "mode": "add", "tags": "Cookbook"}
        with _found(), patch("app.routers.items.cover_queue.enqueue"):
            with patch.object(tags_svc, "attach_tags",
                              side_effect=RuntimeError("simulated tag write failure")):
                try:
                    resp = admin_client.post("/api/scan", data=form)
                    assert resp.status_code >= 500
                except RuntimeError:
                    pass
            assert db.execute(
                "SELECT COUNT(*) FROM items WHERE isbn = ?", (isbn,)
            ).fetchone()[0] == 0

            resp = admin_client.post("/api/scan", data=form)
        assert 'data-scan-status="added"' in resp.text
        item_id = db.execute(
            "SELECT id FROM items WHERE isbn = ?", (isbn,)).fetchone()["id"]
        assert _item_tag_names(db, item_id) == ["Cookbook"]


def _found(title="New Book", authors="Some Author"):
    async def _lookup(isbn13, hc_token, client, *, google_api_key=None):
        meta = {"title": title, "authors": authors}
        return meta, "openlibrary", {}, provider_result.found("openlibrary", meta)

    return patch("app.routers.items_common._lookup_metadata", new=_lookup)


class TestScanAttachesDefaultTags:
    """`POST /api/scan`'s scan wrapper: only a filing status takes the
    session's default tags (design contract table, plan-tags-scan-defaults)."""

    ISBN = "9780441013593"

    def test_add_mode_new_isbn_is_tagged(self, admin_client, db):
        with self._found_and_enqueue():
            resp = admin_client.post("/api/scan", data={
                "isbn": self.ISBN, "media_type": "book", "mode": "add", "tags": "Cookbook",
            })
        assert 'data-scan-status="added"' in resp.text
        item_id = db.execute(
            "SELECT id FROM items WHERE isbn = ?", (self.ISBN,)).fetchone()["id"]
        assert _item_tag_names(db, item_id) == ["Cookbook"]

    def test_wishlist_mode_is_tagged(self, admin_client, db):
        with self._found_and_enqueue():
            resp = admin_client.post("/api/scan", data={
                "isbn": self.ISBN, "media_type": "book", "mode": "wishlist", "tags": "Cookbook",
            })
        assert 'data-scan-status="wishlisted"' in resp.text
        item_id = db.execute(
            "SELECT id FROM items WHERE isbn = ?", (self.ISBN,)).fetchone()["id"]
        assert _item_tag_names(db, item_id) == ["Cookbook"]

    def test_add_mode_over_a_trashed_isbn_restores_and_tags(self, admin_client, db):
        item_id = insert_item(db, title="Stored Title", source="manual",
                              isbn=self.ISBN, media_type="book")
        trash_item(db, item_id)
        db.commit()
        with self._found_and_enqueue(title="Provider Spelling"):
            resp = admin_client.post("/api/scan", data={
                "isbn": self.ISBN, "media_type": "book", "mode": "add", "tags": "Cookbook",
            })
        assert 'data-scan-status="restored"' in resp.text
        assert _item_tag_names(db, item_id) == ["Cookbook"]

    def test_live_duplicate_is_not_tagged(self, admin_client, db):
        item_id = _insert_item(db, isbn=self.ISBN)
        db.commit()
        resp = admin_client.post("/api/scan", data={
            "isbn": self.ISBN, "media_type": "book", "mode": "add", "tags": "Cookbook",
        })
        assert 'data-scan-status="duplicate"' in resp.text
        assert _item_tag_names(db, item_id) == []

    def test_add_mode_promotes_a_wishlisted_row_without_tagging(self, admin_client, db):
        item_id = _insert_item(db, isbn=self.ISBN, owned=0, wishlisted=True)
        db.commit()
        with patch("app.routers.items_common._lookup_metadata",
                   new=AsyncMock(side_effect=AssertionError("no lookup for a known ISBN"))):
            resp = admin_client.post("/api/scan", data={
                "isbn": self.ISBN, "media_type": "book", "mode": "add", "tags": "Cookbook",
            })
        assert 'data-scan-status="promoted"' in resp.text
        assert _item_tag_names(db, item_id) == []

    def test_trashed_isbn_in_lookup_mode_is_not_tagged(self, admin_client, db):
        item_id = insert_item(db, title="Gone", source="manual",
                              isbn=self.ISBN, media_type="book")
        trash_item(db, item_id)
        db.commit()
        resp = admin_client.post("/api/scan", data={
            "isbn": self.ISBN, "media_type": "book", "mode": "lookup", "tags": "Cookbook",
        })
        assert 'data-scan-status="in_trash"' in resp.text
        assert _item_tag_names(db, item_id) == []

    @staticmethod
    def _found_and_enqueue(title="New Book", authors="Some Author"):
        from contextlib import ExitStack

        stack = ExitStack()
        stack.enter_context(_found(title=title, authors=authors))
        stack.enter_context(patch("app.routers.items.cover_queue.enqueue"))
        return stack


class TestLegacyContinuationEchoesTags:
    """Drift 4 / G103: `legacy_ambiguous` and `legacy_incomplete` echo the
    session's default tags as a hidden input, so the item that finally lands
    after the continuation is still tagged. Scrape and re-post exactly what
    the card rendered (G36) rather than hand-picking fields."""

    @pytest.fixture(autouse=True)
    def _no_upc_lookup(self, monkeypatch):
        from app.services import upcitemdb

        async def _forbidden(upc, client):
            raise AssertionError("no UPC lookup")

        monkeypatch.setattr(upcitemdb, "lookup", _forbidden)

    @staticmethod
    def _hidden(html):
        return dict(re.findall(
            r'<input type="hidden" name="([^"]+)" value="([^"]*)"', html))

    @staticmethod
    def _candidate_form(html, isbn13):
        """The ambiguous card renders one `<form>` per candidate, each with
        its own `legacy_confirm_isbn13` — scrape only the chosen one's
        fields, not a blend of every form on the card."""
        for form in re.findall(r"<form.*?</form>", html, re.S):
            if f'name="legacy_confirm_isbn13" value="{isbn13}"' in form:
                return form
        raise AssertionError(f"no candidate form for {isbn13}")

    def test_legacy_incomplete_echoes_and_the_landed_item_is_tagged(
        self, admin_client, db
    ):
        """A single verified candidate resolves the bare scan outright
        (mirrors `TestBareLegacyUpc.test_a_single_verified_candidate_...`)."""
        lookup_mock = AsyncMock(side_effect=AssertionError("no metadata cascade"))
        with patch("app.routers.items_common._lookup_metadata", new=lookup_mock):
            card = admin_client.post("/api/scan", data={
                "isbn": KRISTY_UPC, "media_type": "book", "mode": "add",
                "tags": "Cookbook",
            })

        assert 'data-scan-status="legacy_incomplete"' in card.text
        payload = self._hidden(card.text)
        assert payload.get("tags") == "Cookbook"
        payload["legacy_supplement"] = KRISTY_SUPPLEMENT

        async def lookup(isbn, hc_token, client, *, google_api_key=None):
            if isbn == KRISTY_ISBN13:
                meta = {"title": "Kristy", "authors": "Ann M. Martin"}
                return meta, "openlibrary", {}, provider_result.found("openlibrary", meta)
            return None, "manual", {}, provider_result.no_match("openlibrary")

        with patch("app.routers.items_common._lookup_metadata", new=lookup), \
             patch("app.routers.items.cover_queue.enqueue"):
            filed = admin_client.post("/api/scan", data=payload)

        assert filed.status_code == 200
        item = db.execute(
            "SELECT id FROM items WHERE isbn = ?", (KRISTY_ISBN13,)).fetchone()
        assert item is not None
        assert _item_tag_names(db, item["id"]) == ["Cookbook"]

    def test_legacy_ambiguous_echoes_and_the_chosen_item_is_tagged(
        self, admin_client, db
    ):
        """Two verified candidates stay ambiguous (mirrors
        `TestLegacyBookAdd.test_two_verified_candidates_remain_ambiguous`)."""
        async def lookup(isbn, hc_token, client, *, google_api_key=None):
            meta = {"title": f"Candidate {isbn}", "authors": "Scholastic"}
            return meta, "openlibrary", {}, provider_result.found("openlibrary", meta)

        with patch("app.routers.items_common._lookup_metadata", new=lookup), \
             patch("app.routers.items.cover_queue.enqueue"):
            card = admin_client.post("/api/scan", data={
                "isbn": KRISTY_UPC5, "media_type": "book", "mode": "add",
                "tags": "Cookbook",
            })

        assert 'data-scan-status="legacy_ambiguous"' in card.text
        payload = self._hidden(self._candidate_form(card.text, KRISTY_ISBN13))
        assert payload.get("tags") == "Cookbook"
        assert payload.get("legacy_confirm_isbn13") == KRISTY_ISBN13

        with patch("app.routers.items_common._lookup_metadata", new=lookup), \
             patch("app.routers.items.cover_queue.enqueue"):
            filed = admin_client.post("/api/scan", data=payload)

        assert filed.status_code == 200
        item = db.execute(
            "SELECT id FROM items WHERE isbn = ?", (KRISTY_ISBN13,)).fetchone()
        assert item is not None
        assert _item_tag_names(db, item["id"]) == ["Cookbook"]


class TestCatalogAddsAttachDefaultTags:
    """T3: `/api/books/add`, `/api/games/add` and `/api/dvds/add` each file
    their item inside `tags_svc.default_tags`."""

    def test_book_add_is_tagged(self, editor_client, db, monkeypatch):
        # `download_cover` would otherwise make a real outbound call on this
        # fresh (never-restored) row — orthogonal to what this test pins.
        monkeypatch.setattr(
            items_catalog.covers, "download_cover", AsyncMock(return_value=None))
        with _found(title="Tagged Book"):
            resp = editor_client.post("/api/books/add", data={
                "isbn": "9780441013593", "media_type": "book", "tags": "Cookbook",
            })
        assert 'data-scan-status="added"' in resp.text
        item_id = db.execute(
            "SELECT id FROM items WHERE isbn = ?", ("9780441013593",)).fetchone()["id"]
        assert _item_tag_names(db, item_id) == ["Cookbook"]

    def test_game_add_is_tagged(self, editor_client, db, monkeypatch):
        monkeypatch.setattr(items_catalog, "get_setting", lambda db, key: "configured")
        monkeypatch.setattr(
            items_catalog.igdb, "lookup_game",
            AsyncMock(return_value={
                "title": "Tagged Game", "description": None, "publisher": None,
                "publish_year": None, "series_name": None, "cover_url": None,
            }),
        )
        resp = editor_client.post("/api/games/add", data={
            "igdb_id": "123", "tags": "Cookbook",
        })
        assert 'data-scan-status="added"' in resp.text
        item_id = db.execute(
            "SELECT id FROM items WHERE title = 'Tagged Game' AND media_type = 'video_game'"
        ).fetchone()["id"]
        assert _item_tag_names(db, item_id) == ["Cookbook"]

    def test_dvd_add_is_tagged(self, editor_client, db):
        resp = editor_client.post("/api/dvds/add", data={
            "title": "Tagged DVD", "tags": "Cookbook",
        })
        assert 'data-scan-status="added"' in resp.text
        item_id = db.execute(
            "SELECT id FROM items WHERE title = 'Tagged DVD' AND media_type = 'dvd'"
        ).fetchone()["id"]
        assert _item_tag_names(db, item_id) == ["Cookbook"]


class TestManualAddAttachesDefaultTags:
    """T3: `POST /api/items/manual` reads its form by hand, so the hook
    reads `form.get("tags")` rather than a `Form(...)` parameter."""

    def test_manual_add_is_tagged(self, editor_client, db):
        resp = editor_client.post("/api/items/manual", data={
            "title": "Tagged Manual", "tags": "Cookbook",
        })
        assert 'data-scan-status="added"' in resp.text
        item_id = db.execute(
            "SELECT id FROM items WHERE title = 'Tagged Manual'"
        ).fetchone()["id"]
        assert _item_tag_names(db, item_id) == ["Cookbook"]

    def test_manual_add_over_a_live_duplicate_is_not_tagged(self, editor_client, db):
        isbn = "9780441013593"
        item_id = _insert_item(db, isbn=isbn, media_type="book")
        db.commit()
        resp = editor_client.post("/api/items/manual", data={
            "title": "Duplicate Manual", "isbn": isbn, "media_type": "book",
            "tags": "Cookbook",
        })
        assert 'data-scan-status="duplicate"' in resp.text
        assert _item_tag_names(db, item_id) == []
