from pathlib import Path


p = Path("tests/test_library_item_reads.py")
s = p.read_text()

# These are ACL tests, not identifier-validation tests. Avoid coupling them to
# arbitrary synthetic ISBN check digits; title/library placement is sufficient
# for the security contract being exercised.
s = s.replace(
    "def _item_in_library(db, library_id: int, *, title: str, isbn: str, **fields) -> int:\n"
    "    item_id = _insert_item(db, title=title, isbn=isbn, **fields)\n",
    "def _item_in_library(db, library_id: int, *, title: str, **fields) -> int:\n"
    "    item_id = _insert_item(db, title=title, isbn=None, **fields)\n",
)

for isbn in (
    "9780000011001",
    "9780000011002",
    "9780000011010",
    "9780000011011",
    "9780000011028",
    "9780000011029",
    "9780000011035",
    "9780000011036",
):
    s = s.replace(f', isbn="{isbn}"', "")
    s = s.replace(f'        isbn="{isbn}",\n', "")

if "isbn=\"978000001" in s:
    raise SystemExit("synthetic ACL ISBN remained in generated tests")

p.write_text(s)
