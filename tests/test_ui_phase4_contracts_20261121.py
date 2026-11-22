"""Rendered UI boundaries: genuine controls, media truth tables and namespaces."""
from html.parser import HTMLParser

import pytest


class _Element:
    def __init__(self, tag, attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children = []

    @property
    def text(self):
        return "".join(child.text if isinstance(child, _Element) else child for child in self.children)

    def find_all(self, tag=None, **attrs):
        found = []
        if (tag is None or self.tag == tag) and all(
            key in self.attrs and (value is None or self.attrs[key] == value) for key, value in attrs.items()
        ):
            found.append(self)
        for child in self.children:
            if isinstance(child, _Element):
                found.extend(child.find_all(tag, **attrs))
        return found

    def one(self, tag=None, **attrs):
        matches = self.find_all(tag, **attrs)
        assert len(matches) == 1, (tag, attrs, len(matches))
        return matches[0]

    def within(self, ancestor):
        node = self.parent
        while node is not None:
            if node is ancestor:
                return True
            node = node.parent
        return False


class _Markup(HTMLParser):
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = _Element("document")
        self.stack = [self.root]
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = _Element(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in self._VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for position in range(len(self.stack) - 1, 0, -1):
            if self.stack[position].tag == tag:
                del self.stack[position:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


@pytest.mark.parametrize("media_type", [
    "book", "kids_book", "audiobook", "ebook", "magazine", "dvd", "vinyl", "cassette",
    "cd", "digital_music", "music_other", "comic", "digital_comic", "manga", "digital_manga",
    "video_game", "digital_game",
])
def test_edit_media_truth_table_keeps_successful_fields_and_pending_artwork(editor_client, db, media_type):
    from tests.conftest import _insert_item

    item_id = _insert_item(db, title="All original media fields", isbn=None, media_type=media_type)
    db.commit()
    response = editor_client.get(f"/item/{item_id}/edit")
    assert response.status_code == 200
    root = _Markup(response.text).root
    form = root.one("form", id="item-edit-form")
    assert form.attrs["action"] == f"/api/items/{item_id}"
    assert form.attrs["method"] == "post"
    assert form.attrs["enctype"] == "multipart/form-data"
    assert form.one("select", name="media_type").one("option", selected=None).attrs["value"] == media_type
    books = {"book", "kids_book", "audiobook", "ebook", "comic", "digital_comic", "manga", "digital_manga"}
    upcs = {"dvd", "vinyl", "cassette", "cd", "music_other", "comic", "manga", "video_game"}
    for name, expected in (("isbn", books), ("upc", upcs), ("language", books),
                           ("platform", {"video_game", "digital_game"}), ("asin", {"audiobook"})):
        control = form.one(id=name)
        assert control.attrs["name"] == name
        assert "disabled" not in control.attrs
        group = control.parent
        while "data-media-types" not in group.attrs:
            group = group.parent
            assert group is not None
        supported = set(group.attrs["data-media-types"].split())
        assert supported == expected
        assert group.attrs["data-always-visible"] == "false"
    for name in ("cover_choice_url", "cover_choice_edition_isbn", "cover_choice_edition_language"):
        pending = form.one("input", name=name)
        assert pending.attrs["type"] == "hidden"
        assert pending.attrs["value"] == ""
        assert "disabled" not in pending.attrs
    assert form.one("img", **{"data-edit-cover-preview": None})
    assert form.one(**{"data-edit-cover-placeholder": None})
    assert root.one(id="cover-candidates").within(root.one(**{"data-item-edit-root": None}))
    assert form.one("button", **{"hx-get": f"/api/items/{item_id}/cover-search?editor=1"}).attrs["type"] == "button"
    tags = root.one(id="edit-tags")
    assert not tags.within(form)
    assert tags.one("form").attrs["hx-post"] == f"/api/items/{item_id}/tags"


def test_settings_keeps_flat_delete_and_node_tree_forms_in_their_own_domains(admin_client, db):
    from tests.conftest import _insert_location, _insert_node_location

    flat = _insert_location(db, "O'Brien <Flat>")
    node = _insert_node_location(db, "Node shelf")
    assert flat != node
    db.commit()
    before = {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY id")]
              for table in ("locations", "location_nodes")}
    response = admin_client.get("/settings")
    assert response.status_code == 200
    root = _Markup(response.text).root
    legacy = root.one(**{"data-testid": f"legacy-location-row-{flat}"})
    remove = legacy.one("form", action=f"/api/locations/{flat}/delete")
    assert remove.attrs["data-confirm"] == "Delete location 'O'Brien <Flat>'?"
    assert "O&#39;Brien &lt;Flat&gt;" in response.text
    tree = root.one(**{"data-testid": f"location-row-{node}"})
    assert tree.one("form", action=f"/api/location-tree/{node}/delete")
    assert root.one("form", action="/api/location-tree")
    after = {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY id")]
             for table in ("locations", "location_nodes")}
    assert after == before


def test_shortcut_help_has_only_its_real_account_action_and_preserved_modal(viewer_client):
    response = viewer_client.get("/browse")
    assert response.status_code == 200
    root = _Markup(response.text).root
    assert not root.find_all(id="shortcut-help-trigger")
    action = root.one("button", **{"data-testid": "account-shortcuts-action"})
    assert action.attrs["@click"] == "openShortcuts()"
    assert "Keyboard shortcuts" in action.text
    assert root.one(id="shortcut-modal").one("button", id="shortcut-modal-close")


def test_edit_existing_artwork_uses_the_real_preview_and_preserves_empty_pending_choice(editor_client, db):
    from tests.conftest import _insert_item

    item_id = _insert_item(db, title="Existing artwork", isbn=None, cover_path="covers/existing.jpg")
    db.commit()
    response = editor_client.get(f"/item/{item_id}/edit")
    assert response.status_code == 200
    form = _Markup(response.text).root.one("form", id="item-edit-form")
    preview = form.one("img", **{"data-edit-cover-preview": None})
    assert preview.attrs[":src"] == "preview || '/covers/existing.jpg'"
    assert preview.attrs["alt"] == "Current artwork"
    assert not form.find_all(**{"data-edit-cover-placeholder": None})
    for name in ("cover_choice_url", "cover_choice_edition_isbn", "cover_choice_edition_language"):
        assert form.one("input", name=name).attrs["value"] == ""


def test_edit_keeps_stored_out_of_type_values_visible_and_successful(editor_client, editor_user, db):
    from tests.conftest import _insert_item

    item_id = _insert_item(
        db, title="Stored cross-type metadata", media_type="dvd", isbn="9780306406157",
        language="100%d<unknown>", series_name="Stored saga", series_position=3, page_count=200,
        narrator="Stored narrator", duration_mins=90, asin="B0D1234567",
        reading_status="read", date_started="2026-01-01", date_finished="2026-01-02",
        personal_user_id=editor_user["id"],
    )
    db.commit()
    response = editor_client.get(f"/item/{item_id}/edit")
    assert response.status_code == 200
    form = _Markup(response.text).root.one("form", id="item-edit-form")
    expected = {
        "isbn": "9780306406157", "series_name": "Stored saga", "series_position": "3.0",
        "page_count": "200", "narrator": "Stored narrator", "duration_mins": "90",
        "asin": "B0D1234567", "date_started": "2026-01-01", "date_finished": "2026-01-02",
    }
    for name, value in expected.items():
        field = form.one("input", name=name)
        assert field.attrs["value"] == value
        assert "disabled" not in field.attrs
    assert form.one("select", name="language").one("option", selected=None).attrs["value"] == "100%d<unknown>"
    assert form.one("select", name="reading_status").one("option", selected=None).attrs["value"] == "read"
    for name in (*expected, "language", "reading_status"):
        group = form.one(name=name).parent
        while "data-media-types" not in group.attrs:
            group = group.parent
            assert group is not None
        assert group.attrs["data-always-visible"] == "true"


@pytest.mark.parametrize("path", ["/browse", "/api/search"])
def test_personal_browse_wishlist_excludes_shared_only_neither_and_other_actor(
    admin_client, admin_user, viewer_user, db, path,
):
    from tests.conftest import _insert_item

    _insert_item(db, title="Exact actor wanted", isbn=None, owned=0, wishlisted=True,
                 personal_user_id=admin_user["id"], personal_state={"wishlist": 1})
    _insert_item(db, title="Shared only wanted", isbn=None, owned=0, wishlisted=True)
    _insert_item(db, title="Other actor wanted", isbn=None, owned=0,
                 personal_user_id=viewer_user["id"], personal_state={"wishlist": 1})
    _insert_item(db, title="Neither wanted", isbn=None, owned=0)
    db.commit()
    response = admin_client.get(f"{path}?owned=0")
    assert response.status_code == 200
    assert "Exact actor wanted" in response.text
    for title in ("Shared only wanted", "Other actor wanted", "Neither wanted"):
        assert title not in response.text


def test_home_recent_does_not_turn_a_personal_wish_into_a_shared_badge(admin_client, admin_user, db):
    from tests.conftest import _insert_item

    item_id = _insert_item(db, title="Only my wish", isbn=None, owned=0,
                           personal_user_id=admin_user["id"], personal_state={"wishlist": 1})
    db.commit()
    response = admin_client.get("/")
    assert response.status_code == 200
    card = _Markup(response.text).root.one(**{"data-testid": "home-recent-items"}).one("a", href=f"/item/{item_id}")
    assert "Only my wish" in card.text
    assert not [span for span in card.find_all("span") if span.text == "Wishlist"]


def test_authorized_admin_trash_blocker_is_escaped_and_actionable(admin_client, db):
    from app.services.item_write import trash_item
    from tests.conftest import _insert_item

    title = "<script>Blocked & title</script>"
    blocker = _insert_item(db, title=title, isbn=None)
    trash_item(db, blocker)
    editing = _insert_item(db, title="Editing item", isbn=None)
    db.commit()
    before = [tuple(row) for row in db.execute("SELECT * FROM items ORDER BY id")]
    response = admin_client.get(f"/item/{editing}/edit?error=identifier_in_trash&trashed={blocker}")
    assert response.status_code == 200
    banner = _Markup(response.text).root.one(**{"data-testid": "edit-error"})
    assert title in banner.text
    assert "&lt;script&gt;Blocked &amp; title&lt;/script&gt;" in response.text
    assert title not in response.text
    assert banner.one("a", href="/trash").text == "Restore it from Trash"
    assert [tuple(row) for row in db.execute("SELECT * FROM items ORDER BY id")] == before


@pytest.mark.parametrize("blocker_kind", ["authorized", "hidden", "missing", "live"])
def test_editor_trash_blocker_names_only_authorized_trash_and_never_offers_admin_action(
    editor_client, db, blocker_kind,
):
    from app.services import libraries
    from app.services.item_write import trash_item
    from tests.conftest import _insert_item

    title = "<script>Private blocker</script>"
    if blocker_kind == "missing":
        blocker = 999999
    else:
        library_id = libraries.DEFAULT_LIBRARY_ID
        if blocker_kind == "hidden":
            library_id = libraries.create_library(db, "Private blocker library")["id"]
        blocker = _insert_item(db, title=title, isbn=None, _library_id=library_id)
        if blocker_kind != "live":
            trash_item(db, blocker)
    editing = _insert_item(db, title="Editor item", isbn=None)
    db.commit()
    response = editor_client.get(f"/item/{editing}/edit?error=identifier_in_trash&trashed={blocker}")
    assert response.status_code == 200
    banner = _Markup(response.text).root.one(**{"data-testid": "edit-error"})
    assert not banner.find_all("a")
    assert title not in response.text
    if blocker_kind == "authorized":
        assert title in banner.text
        assert "&lt;script&gt;Private blocker&lt;/script&gt;" in response.text
        assert "Ask an administrator to restore it from Trash" in banner.text
    else:
        assert title not in banner.text
        assert "&lt;script&gt;Private blocker&lt;/script&gt;" not in response.text
        assert banner.text == "That identifier belongs to an item in Trash or is otherwise unavailable. Nothing was saved."


@pytest.mark.parametrize("suffix", ["", "/edit"])
@pytest.mark.parametrize("kind", ["missing", "hidden", "trashed"])
def test_unavailable_item_gets_have_the_exact_native_redirect_without_disclosure(editor_client, db, suffix, kind):
    from app.services import libraries
    from app.services.item_write import trash_item
    from tests.conftest import _insert_item

    title = "Unavailable item title"
    if kind == "missing":
        item_id = 999999
    else:
        library_id = libraries.DEFAULT_LIBRARY_ID
        if kind == "hidden":
            library_id = libraries.create_library(db, "Hidden item library")["id"]
        item_id = _insert_item(db, title=title, isbn=None, _library_id=library_id)
        if kind == "trashed":
            trash_item(db, item_id)
    db.commit()
    response = editor_client.get(f"/item/{item_id}{suffix}", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/browse"
    assert title not in response.text
    personal = editor_client.get(f"/api/items/{item_id}/personal-state")
    assert personal.status_code == 404
    assert title not in personal.text


def test_native_admin_recovery_is_a_core_settings_library_link_not_global_navigation(admin_client):
    from app.database import get_db
    from app.features import FEATURES, set_feature_enabled

    with get_db() as db:
        for key in FEATURES:
            set_feature_enabled(db, key, False)
    response = admin_client.get("/settings")
    assert response.status_code == 200
    root = _Markup(response.text).root
    library = root.one("div", **{"x-show": "tab === 'library'", "class": "space-y-8"})
    link = root.one("a", href="/settings/library-recovery")
    assert link.within(library)
    display = library.one("form", action="/api/settings/display")
    assert display.attrs["method"] == "post"
    assert link.text == "Recover items without a library"
    destination = admin_client.get(link.attrs["href"])
    assert destination.status_code == 200
    assert not _Markup(admin_client.get("/browse").text).root.find_all("a", href="/settings/library-recovery")
