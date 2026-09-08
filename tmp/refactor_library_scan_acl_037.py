from pathlib import Path

# Keep items_common as the stable patching facade while moving scanner domain
# state out to app/services/scanner_state.py. Existing callers/tests can keep
# patching the historical helper names without regrowing the capped module.
path = Path("app/routers/items_common.py")
text = path.read_text()
text = text.replace(
    "from app.services import covers, detect, googlebooks, hardcover, libraries, national, openlibrary, provider_result, user_state\n",
    "from app.services import covers, detect, googlebooks, hardcover, national, openlibrary, provider_result, scanner_state, user_state\n",
    1,
)

start = text.index("def _default_library_edit_allowed(request: Request) -> bool:")
end = text.index("async def _lookup_metadata", start)
wrappers = '''def _default_library_edit_allowed(request: Request) -> bool:
    return scanner_state.default_library_edit_allowed(dict(request.state.user))


def _scan_duplicate_response(
    request: Request, templates, existing, barcode: str, *, mode: str,
    media_type: str | None = None,
):
    return scanner_state.duplicate_response(
        request, templates, existing, barcode, mode=mode, media_type=media_type
    )


'''
text = text[:start] + wrappers + text[end:]

start = text.index("_SCAN_LOG_RETENTION_DAYS = 90")
end = text.index("# Values are the funnel's job", start)
log_wrapper = '''# Compatibility patch point retained for existing security tests and any
# extension that deliberately resets the prune clock. The service owns the
# implementation; the facade synchronises this one scalar before/after calls.
_scan_log_last_prune = scanner_state._scan_log_last_prune


def _log_scan(
    isbn: str, media_type: str, result: str, item_id: int | None = None,
    mode: str = "add",
):
    global _scan_log_last_prune
    scanner_state._scan_log_last_prune = _scan_log_last_prune
    result_value = scanner_state.log_scan(isbn, media_type, result, item_id, mode)
    _scan_log_last_prune = scanner_state._scan_log_last_prune
    return result_value


'''
text = text[:start] + log_wrapper + text[end:]

start = text.index("def _save_item(")
end = text.index("async def _fetch_preview_cover", start)
save_wrapper = '''def _save_item(
    metadata: dict, isbn13: str, media_type: str, location_id: int | None,
    source: str, hc_ids: dict, *, owned: int = 1,
    wishlist_user_id: int | None = None,
) -> int:
    return scanner_state.save_scanned_item(
        metadata, isbn13, media_type, location_id, source, hc_ids,
        owned=owned, wishlist_user_id=wishlist_user_id,
    )


'''
text = text[:start] + save_wrapper + text[end:]
path.write_text(text)

# The legacy barcode regression must verify the same modern personal state as
# ordinary quick-rate. The compatibility barcode mapping changes identity
# resolution, not the state ownership model.
path = Path("tests/test_legacy_book_scan.py")
text = path.read_text()
text = text.replace(
    '''    def test_remembered_mapping_supports_every_existing_item_mode(
        self, admin_client, db
    ):
''',
    '''    def test_remembered_mapping_supports_every_existing_item_mode(
        self, admin_client, admin_user, db
    ):
''',
    1,
)
old = '''        row = db.execute(
            "SELECT location_id, reading_status, date_finished FROM items WHERE id = ?",
            (item_id,),
        ).fetchone()
        assert row["location_id"] == home
        assert row["reading_status"] == "read"
        assert row["date_finished"] is not None
'''
new = '''        row = db.execute(
            "SELECT location_id FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        personal = db.execute(
            "SELECT reading_status, date_finished FROM user_item_state "
            "WHERE user_id = ? AND item_id = ?",
            (admin_user["id"], item_id),
        ).fetchone()
        assert row["location_id"] == home
        assert personal["reading_status"] == "read"
        assert personal["date_finished"] is not None
'''
if old not in text:
    raise SystemExit("legacy quick-rate assertion anchor not found")
path.write_text(text.replace(old, new, 1))

print("scanner ACL architecture refactor applied")
