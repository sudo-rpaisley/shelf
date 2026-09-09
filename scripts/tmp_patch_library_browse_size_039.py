from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f"anchor not found in {path}: {old[:100]!r}")
    p.write_text(text.replace(old, new, 1))


# Keep catalogue visibility queries inside the library policy service. Besides
# being the right ownership boundary, this keeps items_common.py under its
# enforced module-size ceiling.
p = Path("app/services/libraries.py")
s = p.read_text().rstrip()
if "def visible_locations(" in s:
    raise SystemExit("visible_locations already exists")
s += r'''


def visible_locations(db, user: dict | None):
    """Locations represented by items visible to ``user``.

    ``None`` is retained for callers that are intentionally outside an
    authenticated request and therefore expect the historical global list.
    Admins likewise retain global recovery visibility.
    """
    if user is None or user.get("role") == "admin":
        return db.execute(
            "SELECT * FROM locations ORDER BY sort_order, name"
        ).fetchall()

    access_sql, access_params = item_access_condition(user)
    return db.execute(
        "SELECT DISTINCT l.* FROM locations l "
        "JOIN items i ON i.location_id = l.id "
        f"WHERE {access_sql} ORDER BY l.sort_order, l.name",
        access_params,
    ).fetchall()
'''
p.write_text(s + "\n")

replace_once(
    "app/routers/items_common.py",
    '''    if user is None or user.get("role") == "admin":
        locations = db.execute(
            "SELECT * FROM locations ORDER BY sort_order, name"
        ).fetchall()
    else:
        access_sql, access_params = libraries.item_access_condition(user)
        locations = db.execute(
            "SELECT DISTINCT l.* FROM locations l "
            "JOIN items i ON i.location_id = l.id "
            f"WHERE {access_sql} ORDER BY l.sort_order, l.name",
            access_params,
        ).fetchall()
''',
    '''    locations = libraries.visible_locations(db, user)
''',
)
