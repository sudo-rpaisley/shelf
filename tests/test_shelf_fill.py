import re
from pathlib import Path

from app.database import get_db
from app.services import holdings, location_tree
from app.services.item_write import insert_item


def _nested_location():
    with get_db() as db:
        legacy_id = db.execute(
            "INSERT INTO locations (name, sort_order) VALUES ('Living Room', 1)"
        ).lastrowid
        holdings.ensure_foundation(db)
        # This flat row was added AFTER init. Ensure must not sweep/reseed it;
        # explicitly project it, with unequal domains so the seam is meaningful.
        assert location_tree.legacy_location_to_node(db, legacy_id) is None
        db.execute("INSERT INTO location_nodes (id, name) VALUES (100, 'Unmapped node control')")
        root_id = location_tree.legacy_location_to_node(db, legacy_id, create=True)
        assert root_id is not None and root_id != legacy_id
        bookcase_id = db.execute(
            "INSERT INTO location_nodes (parent_id, name, sort_order) VALUES (?, 'Bookcase', 1)",
            (root_id,),
        ).lastrowid
        shelf_id = db.execute(
            "INSERT INTO location_nodes (parent_id, name, sort_order) VALUES (?, 'Shelf 1', 1)",
            (bookcase_id,),
        ).lastrowid
    return legacy_id, root_id, bookcase_id, shelf_id


def _physical_item(isbn, legacy_id, *, title="Test Book", owned=1):
    with get_db() as db:
        return insert_item(
            db,
            title=title,
            isbn=isbn,
            media_type="book",
            source="test",
            owned=owned,
            location_id=legacy_id,
        )


def test_shelf_fill_picker_uses_hierarchical_paths(editor_client):
    _nested_location()

    response = editor_client.get("/api/shelf-fill/location-picker")

    assert response.status_code == 200
    assert 'id="shelf-fill-location"' in response.text
    assert "Living Room › Bookcase › Shelf 1" in response.text


def test_shelf_fill_existing_item_moves_primary_copy_and_sets_position(editor_client):
    legacy_id, _, _, shelf_id = _nested_location()
    item_id = _physical_item("9780000000101", legacy_id)

    response = editor_client.post(
        "/api/shelf-fill/scan",
        data={"isbn": "9780000000101", "location_node_id": shelf_id, "media_type": "auto"},
    )

    assert response.status_code == 200
    assert "shelved" in response.text
    assert "Living Room › Bookcase › Shelf 1" in response.text
    assert "position 1" in response.text
    with get_db() as db:
        copy = db.execute(
            "SELECT location_id, position_order FROM item_copies "
            "WHERE item_id = ? AND is_primary = 1",
            (item_id,),
        ).fetchone()
        item = db.execute("SELECT location_id FROM items WHERE id = ?", (item_id,)).fetchone()
    assert copy["location_id"] == shelf_id
    assert copy["position_order"] == 1
    assert item["location_id"] == legacy_id


def test_shelf_fill_scan_order_appends_positions(editor_client):
    legacy_id, _, _, shelf_id = _nested_location()
    first_id = _physical_item("9780000000118", legacy_id, title="First")
    second_id = _physical_item("9780000000125", legacy_id, title="Second")

    for code in ("9780000000118", "9780000000125"):
        response = editor_client.post(
            "/api/shelf-fill/scan",
            data={"isbn": code, "location_node_id": shelf_id, "media_type": "auto"},
        )
        assert response.status_code == 200

    with get_db() as db:
        rows = db.execute(
            "SELECT item_id, position_order FROM item_copies "
            "WHERE location_id = ? ORDER BY position_order",
            (shelf_id,),
        ).fetchall()
    assert [(row["item_id"], row["position_order"]) for row in rows] == [
        (first_id, 1),
        (second_id, 2),
    ]


def test_shelf_fill_wishlist_item_becomes_owned(editor_client):
    legacy_id, _, _, shelf_id = _nested_location()
    item_id = _physical_item("9780000000132", legacy_id, owned=0)

    response = editor_client.post(
        "/api/shelf-fill/scan",
        data={"isbn": "9780000000132", "location_node_id": shelf_id, "media_type": "auto"},
    )

    assert response.status_code == 200
    assert "Moved from wishlist and shelved" in response.text
    with get_db() as db:
        item = db.execute("SELECT owned FROM items WHERE id = ?", (item_id,)).fetchone()
        copy = db.execute(
            "SELECT location_id FROM item_copies WHERE item_id = ? AND is_primary = 1",
            (item_id,),
        ).fetchone()
    assert item["owned"] == 1
    assert copy["location_id"] == shelf_id


def test_shelf_fill_copy_barcode_moves_exact_copy(editor_client):
    legacy_id, root_id, _, shelf_id = _nested_location()
    item_id = _physical_item("9780000000149", legacy_id)
    with get_db() as db:
        second_copy_id = db.execute(
            "INSERT INTO item_copies "
            "(item_id, copy_number, location_id, copy_barcode, is_primary) "
            "VALUES (?, 2, ?, 'COPY-0002', 0)",
            (item_id, root_id),
        ).lastrowid
        primary_id = db.execute(
            "SELECT id FROM item_copies WHERE item_id = ? AND is_primary = 1",
            (item_id,),
        ).fetchone()["id"]

    response = editor_client.post(
        "/api/shelf-fill/scan",
        data={"isbn": "COPY-0002", "location_node_id": shelf_id, "media_type": "auto"},
    )

    assert response.status_code == 200
    assert "copy 2" in response.text
    with get_db() as db:
        second = db.execute(
            "SELECT location_id, position_order FROM item_copies WHERE id = ?",
            (second_copy_id,),
        ).fetchone()
        primary = db.execute(
            "SELECT location_id FROM item_copies WHERE id = ?", (primary_id,)
        ).fetchone()
    assert second["location_id"] == shelf_id
    assert second["position_order"] == 1
    assert primary["location_id"] == root_id


def test_shelf_fill_rejects_digital_item(editor_client):
    _, _, _, shelf_id = _nested_location()
    with get_db() as db:
        insert_item(
            db,
            title="Digital Test",
            isbn="9780000000156",
            media_type="ebook",
            source="test",
            owned=1,
        )

    response = editor_client.post(
        "/api/shelf-fill/scan",
        data={"isbn": "9780000000156", "location_node_id": shelf_id, "media_type": "auto"},
    )

    assert response.status_code == 200
    assert "Shelf Fill only works with physical media" in response.text


def test_shelf_fill_unknown_scan_delegates_to_add_then_places(editor_client, monkeypatch):
    legacy_id, _, _, shelf_id = _nested_location()
    from app.routers import items

    async def fake_add(
        request,
        isbn,
        media_type="book",
        location_id=None,
        platform="",
        mode="add",
        borrower_id=None,
        legacy_confirm_isbn13="",
        legacy_supplement="",
        tags="",
        reading_status="",
        audit_id=None,
    ):
        assert isbn == "9780000000163"
        assert location_id == legacy_id
        assert mode == "add"
        assert borrower_id is None and audit_id is None
        assert legacy_confirm_isbn13 == legacy_supplement == tags == reading_status == ""
        with get_db() as db:
            item_id = insert_item(
                db,
                title="New Scan",
                isbn=isbn,
                media_type="book",
                source="test",
                location_id=location_id,
            )
        return request.app.state.templates.TemplateResponse(
            request,
            "fragments/scan_result.html",
            {
                "status": "added",
                "isbn": isbn,
                "title": "New Scan",
                "authors": None,
                "cover_path": None,
                "item_id": item_id,
                "source": "test",
                "media_type_label": "Book",
            },
        )

    monkeypatch.setattr(items, "_scan_isbn_inner", fake_add)

    response = editor_client.post(
        "/api/shelf-fill/scan",
        data={"isbn": "9780000000163", "location_node_id": shelf_id, "media_type": "auto"},
    )

    assert response.status_code == 200
    assert "Added and shelved" in response.text
    with get_db() as db:
        copy = db.execute(
            "SELECT c.location_id, c.position_order FROM item_copies c "
            "JOIN items i ON i.id = c.item_id WHERE i.isbn = ? AND c.is_primary = 1",
            ("9780000000163",),
        ).fetchone()
    assert copy["location_id"] == shelf_id
    assert copy["position_order"] == 1


def test_scan_script_exposes_sticky_shelf_fill_mode():
    script = Path("static/js/scan.js").read_text()
    flow = Path("static/js/shelf-fill-flow.js").read_text()
    template = Path("app/templates/scan.html").read_text()

    assert "{id: 'shelf_fill', label: t('Shelf Fill')}" in script
    assert "shelf_fill_location" in script
    assert "/api/shelf-fill/scan" in script
    assert "mode: localStorage.getItem('shelf_scan_mode') || 'add'" in script
    assert "shelfLocation: localStorage.getItem('shelf_fill_location') || ''" in script
    persist = re.search(r"        persistShelfLocation\(\) \{(.*?)\n        \}", script, re.S)
    assert persist is not None
    assert "this.shelfLocation = select ? select.value : '';" in persist.group(1)
    assert "localStorage.setItem('shelf_fill_location', this.shelfLocation);" in persist.group(1)
    assert "this._shelfFlow.changed();" in persist.group(1)

    # Placement was extracted, not removed. Bind the loaded helper to the
    # actual scanPage instance and its camera call chain, not concatenated text.
    scripts = re.findall(r'<script\b[^>]*src="([^"]+)"', template)
    assert scripts.count("/static/js/shelf-fill-flow.js") == 1
    assert scripts.count("/static/js/scan.js") == 1
    assert scripts.index("/static/js/shelf-fill-flow.js") < scripts.index("/static/js/scan.js")
    assert 'x-data="scanPage"' in template and 'data-shelf-fill-origin="scan"' in template
    init = re.search(r"        init\(\) \{(.*?)\n        \},", script, re.S)
    assert init is not None
    factory = re.search(r"this\._shelfFlow = createShelfFillFlow\(\{(.*?)\}\);", init.group(1), re.S)
    assert factory is not None
    assert " ".join(factory.group(1).split()) == (
        "root: this.$el, form: form, results: document.getElementById('scan-results'), "
        "node: function () { return document.getElementById('shelf-fill-location'); }, "
        "active: function () { return self.mode === 'shelf_fill'; }"
    )
    assert "var form = document.querySelector('form[hx-post=\"/api/scan\"]');" in init.group(1)
    on_scan = re.search(r"        async onScan\(code\) \{(.*?)\n        \}", script, re.S)
    assert on_scan is not None
    camera = on_scan.group(1)
    capture = "var shelfContext = scanMode === 'shelf_fill' ? this._shelfFlow.begin() : null;"
    assert capture in camera
    first_await = re.search(r"\bawait\s+\w", camera)
    assert first_await is not None
    assert camera.index(capture) < first_await.start()
    assert "if (scanMode === 'shelf_fill' && !shelfContext) return;" in camera
    assert "if (shelfContext) card = await this._shelfFlow.scan(shelfContext, formData);" in camera
    assert "if (shelfContext && !this._shelfFlow.current(shelfContext)) return;" in camera
    assert re.search(r"finally\s*\{\s*this.scanLoading = false;\s*if \(shelfContext\) this\._shelfFlow.finish\(\);", camera)

    placement = re.search(r"    async function place\(entry\) \{(.*?)\n    \}", flow, re.S)
    assert placement is not None
    place = placement.group(1)
    assert "var context = begin(entry.context);" in place
    assert "body.set('item_id', entry.id);" in place
    assert "body.set('location_node_id', context.node);" in place
    assert "body.set('shelf_fill_origin', origin);" in place
    assert re.search(
        r"fetch\('/api/shelf-fill/place',\s*\{\s*method: 'POST', "
        r"headers: \{'X-CSRF-Token': session, 'HX-Request': 'true'\}, body: body\s*\}\)", place,
    )
    assert "var session = window.csrfToken();" in flow
    assert "options.root.getAttribute('data-shelf-fill-origin') || 'scan'" in flow
    assert re.search(
        r"if \(!receipt \|\| receipt.dataset.scanItemId !== entry.id \|\| "
        r"receipt.dataset.shelfFillPlacedNode !== context.node \|\|\s*"
        r"!receipt.querySelector\('\[data-testid=\"shelf-fill-position\"\]'\)\) throw new Error\('Not placed'\);",
        place,
    )
    assert "catch (error)" in place and "entry.automatic = false;" in place
    assert "finally { finish(); }" in place
