from pathlib import Path

root = Path(__file__).resolve().parents[1]
path = root / "tests/test_komga_series_browse.py"
text = path.read_text()

old_import = "from app.services import komga_records, komga_series_browse\n"
new_import = old_import + "from app.services.item_write import insert_item\n"
if "from app.services.item_write import insert_item" not in text:
    if text.count(old_import) != 1:
        raise RuntimeError("unexpected Komga series test imports")
    text = text.replace(old_import, new_import, 1)

marker = "\ndef test_explicit_series_filter_keeps_individual_item_browse(db):\n"
test = '''\ndef test_grouping_happens_before_pagination_without_repeating_a_series(db):
    _add(db, "book-a1", "series-a", 1, name="A Series")
    _add(db, "book-a2", "series-a", 2, name="A Series")
    _add(db, "book-a3", "series-a", 3, name="A Series")
    insert_item(db, {"title": "B Standalone", "media_type": "book", "source": "test"})
    insert_item(db, {"title": "C Standalone", "media_type": "book", "source": "test"})

    first, raw_total, display_total = komga_series_browse.fetch_page(
        db, "", [], "i.title COLLATE NOCASE ASC",
        limit=2, offset=0, values={},
    )
    second, raw_total_2, display_total_2 = komga_series_browse.fetch_page(
        db, "", [], "i.title COLLATE NOCASE ASC",
        limit=2, offset=2, values={},
    )

    assert raw_total == raw_total_2 == 5
    assert display_total == display_total_2 == 3
    assert len(first) == 2
    assert len(second) == 1
    assert sum(item["browse_series_id"] == "series-a" for item in first + second) == 1
    assert {item["title"] for item in first + second if not item["browse_series_group"]} == {
        "B Standalone",
        "C Standalone",
    }
'''
if "test_grouping_happens_before_pagination_without_repeating_a_series" not in text:
    if text.count(marker) != 1:
        raise RuntimeError("pagination insertion marker changed")
    text = text.replace(marker, test + marker, 1)

path.write_text(text)
