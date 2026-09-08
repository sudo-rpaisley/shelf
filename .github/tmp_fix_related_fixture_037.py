from pathlib import Path

path = Path('tests/test_related_media_ui.py')
text = path.read_text()
text = text.replace(
    'from app.services import media_groups\n',
    'from app.services import libraries, media_groups\n',
    1,
)
old = '''def _item(db, title, media_type="book", authors=None):\n    return db.execute(\n        "INSERT INTO items (title, media_type, authors, owned) VALUES (?, ?, ?, 1)",\n        (title, media_type, authors),\n    ).lastrowid\n'''
new = '''def _item(db, title, media_type="book", authors=None):\n    item_id = db.execute(\n        "INSERT INTO items (title, media_type, authors, owned) VALUES (?, ?, ?, 1)",\n        (title, media_type, authors),\n    ).lastrowid\n    libraries.assign_item(db, item_id, libraries.DEFAULT_LIBRARY_ID)\n    return item_id\n'''
if old not in text:
    raise SystemExit('related-media fixture anchor not found')
path.write_text(text.replace(old, new, 1))
