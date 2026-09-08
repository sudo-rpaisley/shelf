from pathlib import Path

# Correct synthetic scanner identifiers so add-mode tests reach the ACL paths
# rather than stopping at ISBN validation. Keep every fixture in canonical
# storage form, matching Shelf's production write funnel.
tests = Path("tests/test_library_scan_acl_037.py")
text = tests.read_text()
replacements = {
    "9780000015002": "9780000015006",
    "9780000015019": "9780000015013",
    "9780000015026": "9780000015020",
    "9780000015033": "9780000015037",
    "9780000015040": "9780000015044",
    "9780000015057": "9780000015051",
    "9780000015064": "9780000015068",
    "9780000015071": "9780000015075",
    "9780000015088": "9780000015082",
    "9780000015095": "9780000015099",
    "9780000015101": "9780000015105",
    "9780000015118": "9780000015112",
    "9780000015125": "9780000015129",
    # UPC/EAN values on items are stored as canonical EAN-13, not GTIN-14.
    'upc="04006381333931"': 'upc="4006381333931"',
}
for old, new in replacements.items():
    text = text.replace(old, new)
tests.write_text(text)

# The scanner's historical Wishlist regression encoded the old shared-state
# behaviour. Existing catalogue rows must now remain owned and only gain the
# acting user's personal Wishlist flag.
path = Path("tests/test_scan_modes.py")
text = path.read_text()
old = '''class TestWishlistMode:
    def test_wishlist_sets_owned_zero(self, admin_client, db):
        """Wishlist mode should create item with owned=0."""
        # We can't easily test full metadata lookup without mocking external APIs,
        # but we can test the duplicate path returns correctly
        item_id = _insert_item(db, title="Already Here", isbn="9780000000002")
        db.commit()
        resp = admin_client.post("/api/scan", data={
            "isbn": "9780000000002", "media_type": "book", "mode": "wishlist",
        })
        assert resp.status_code == 200
        assert b"duplicate" in resp.content
'''
new = '''class TestWishlistMode:
    def test_wishlist_existing_item_is_personal(self, admin_client, admin_user, db):
        """Wishlist mode on an existing row changes only the acting user's state."""
        item_id = _insert_item(db, title="Already Here", isbn="9780000000002", owned=1)
        db.commit()
        resp = admin_client.post("/api/scan", data={
            "isbn": "9780000000002", "media_type": "book", "mode": "wishlist",
        })
        assert resp.status_code == 200
        assert b"wishlisted" in resp.content
        with get_db() as check_db:
            owned = check_db.execute(
                "SELECT owned FROM items WHERE id = ?", (item_id,)
            ).fetchone()["owned"]
            state = check_db.execute(
                "SELECT wishlist FROM user_item_state WHERE user_id = ? AND item_id = ?",
                (admin_user["id"], item_id),
            ).fetchone()
        assert owned == 1
        assert state["wishlist"] == 1
'''
if old not in text:
    raise SystemExit("legacy Wishlist test anchor not found")
path.write_text(text.replace(old, new, 1))

# Inventory is a shared mutation workflow. The SQL predicate is the primary
# filter; retain an explicit item-policy check before rendering as defence in
# depth. Anchor this immediately before the unique `missing =` projection so
# it remains stable even if the query formatting changes.
path = Path("app/routers/items.py")
text = path.read_text()
old = '''        ).fetchall()

    missing = [dict(i) for i in items if i["id"] not in scanned]
'''
new = '''        ).fetchall()
        items = [
            row for row in items
            if libraries.has_item_role(
                db, dict(request.state.user), int(row["id"]), "editor"
            )
        ]

    missing = [dict(i) for i in items if i["id"] not in scanned]
'''
if old not in text:
    raise SystemExit("inventory missing projection anchor not found")
path.write_text(text.replace(old, new, 1))

print("scanner ACL refinements applied")
