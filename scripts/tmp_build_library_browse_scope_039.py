from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    s = p.read_text()
    if old not in s:
        raise SystemExit(f"anchor not found in {path}: {old[:80]!r}")
    p.write_text(s.replace(old, new, 1))


# Central bound SQL predicate.  It deliberately lives with the existing
# membership helpers so every read surface can reuse one policy definition.
replace_once(
    "app/services/libraries.py",
    "from __future__ import annotations\n\n\nLIBRARY_ROLE_LEVELS",
    "from __future__ import annotations\n\nimport re\n\n\nLIBRARY_ROLE_LEVELS",
)

p = Path("app/services/libraries.py")
s = p.read_text()
append = r'''

_SQL_ALIAS = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def item_access_condition(
    user: dict,
    *,
    item_alias: str = "i",
    minimum_role: str = "viewer",
) -> tuple[str, list]:
    """Return a bound SQL predicate for catalogue rows visible to ``user``.

    Admins retain global recovery access.  Non-admins must have a membership
    for the item's non-archived library.  The item alias is validated because
    SQL identifiers cannot be passed as SQLite bound parameters.
    """
    if minimum_role not in LIBRARY_ROLE_LEVELS:
        raise ValueError("Unknown library role")
    if not _SQL_ALIAS.fullmatch(item_alias):
        raise ValueError("Invalid SQL item alias")
    if user.get("role") == "admin":
        return "1 = 1", []
    if minimum_role == "admin":
        return "1 = 0", []

    roles = "('editor')" if minimum_role == "editor" else "('viewer','editor')"
    return (
        "EXISTS (SELECT 1 FROM library_items li "
        "JOIN libraries l ON l.id = li.library_id "
        "JOIN library_memberships lm ON lm.library_id = li.library_id "
        f"WHERE li.item_id = {item_alias}.id "
        "AND l.is_archived = 0 AND lm.user_id = ? "
        f"AND lm.role IN {roles})",
        [int(user["id"])],
    )


def scope_where(
    where: str,
    params: list,
    user: dict,
    *,
    item_alias: str = "i",
    minimum_role: str = "viewer",
) -> tuple[str, list]:
    """AND library visibility onto a Browse/search WHERE clause."""
    access_sql, access_params = item_access_condition(
        user, item_alias=item_alias, minimum_role=minimum_role
    )
    if where:
        return f"{where} AND {access_sql}", [*params, *access_params]
    return f"WHERE {access_sql}", [*access_params]
'''
if "def item_access_condition(" in s:
    raise SystemExit("library access predicate already exists")
p.write_text(s.rstrip() + append + "\n")

# Cross-filter counts must use the same access set as the result query.
replace_once(
    "app/routers/items_common.py",
    "from app.services import cover_queue\n",
    "from app.services import cover_queue\nfrom app.services import libraries\n",
)
replace_once(
    "app/routers/items_common.py",
    "def filter_counts(db, values: dict, total: int) -> dict:\n",
    "def filter_counts(db, values: dict, total: int, *, user: dict | None = None) -> dict:\n",
)
replace_once(
    "app/routers/items_common.py",
    """    def _count_where(exclude):
        return browse_filters.build_where(values, exclude=exclude)
""",
    """    def _count_where(exclude):
        where, params = browse_filters.build_where(values, exclude=exclude)
        if user is not None:
            where, params = libraries.scope_where(where, params, user)
        return where, params
""",
)
replace_once(
    "app/routers/items_common.py",
    """    locations = db.execute(
        "SELECT * FROM locations ORDER BY sort_order, name"
    ).fetchall()
""",
    """    if user is None or user.get("role") == "admin":
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
""",
)

# First-paint Browse: result rows, cross-filter counts and every catalogue-
# derived option must come from the same library access set.
replace_once(
    "app/routers/pages.py",
    "from app.services import item_copies\n",
    "from app.services import item_copies, libraries\n",
)
replace_once(
    "app/routers/pages.py",
    """    values = browse_filters.values_from(request.query_params)
    # Truncate search query to prevent slow LIKE scans (parity with /api/search)
""",
    """    user = dict(request.state.user)
    values = browse_filters.values_from(request.query_params)
    # Truncate search query to prevent slow LIKE scans (parity with /api/search)
""",
)
replace_once(
    "app/routers/pages.py",
    """    where, params = browse_filters.build_where(values)

    with get_db() as db:
""",
    """    where, params = browse_filters.build_where(values)
    where, params = libraries.scope_where(where, params, user)

    with get_db() as db:
""",
)
replace_once(
    "app/routers/pages.py",
    """        series_names = [
            row["series_name"]
            for row in db.execute(
                "SELECT DISTINCT series_name FROM items "
                "WHERE series_name IS NOT NULL AND TRIM(series_name) != '' "
                "ORDER BY series_name COLLATE NOCASE"
            ).fetchall()
        ]
""",
    """        access_sql, access_params = libraries.item_access_condition(user)
        series_names = [
            row["series_name"]
            for row in db.execute(
                "SELECT DISTINCT i.series_name FROM items i "
                "WHERE i.series_name IS NOT NULL AND TRIM(i.series_name) != '' "
                f"AND {access_sql} ORDER BY i.series_name COLLATE NOCASE",
                access_params,
            ).fetchall()
        ]
""",
)
replace_once(
    "app/routers/pages.py",
    "counts = items_common.filter_counts(db, values, total_filtered)",
    "counts = items_common.filter_counts(db, values, total_filtered, user=user)",
)
replace_once(
    "app/routers/pages.py",
    """        lent_out_count = db.execute(
            "SELECT COUNT(DISTINCT item_id) as c FROM checkouts WHERE checked_in IS NULL"
        ).fetchone()["c"]

        from app.routers.tags import get_all_tags
        all_tags = get_all_tags(db)

        # Languages present in the library — the filter only renders/offers
        # what actually exists.
        item_languages = [
            row["language"]
            for row in db.execute(
                "SELECT DISTINCT language FROM items "
                "WHERE language IS NOT NULL AND language != '' ORDER BY language"
            ).fetchall()
        ]
""",
    """        lent_out_count = db.execute(
            "SELECT COUNT(DISTINCT c.item_id) as c "
            "FROM checkouts c JOIN items i ON i.id = c.item_id "
            f"WHERE c.checked_in IS NULL AND {access_sql}",
            access_params,
        ).fetchone()["c"]

        all_tags = db.execute(
            "SELECT t.id, t.name, COUNT(it.item_id) AS count "
            "FROM tags t JOIN item_tags it ON it.tag_id = t.id "
            "JOIN items i ON i.id = it.item_id "
            f"WHERE {access_sql} GROUP BY t.id ORDER BY t.name COLLATE NOCASE",
            access_params,
        ).fetchall()

        # Languages present in the visible libraries only.
        item_languages = [
            row["language"]
            for row in db.execute(
                "SELECT DISTINCT i.language FROM items i "
                "WHERE i.language IS NOT NULL AND i.language != '' "
                f"AND {access_sql} ORDER BY i.language",
                access_params,
            ).fetchall()
        ]
""",
)

# HTMX search must use the identical access predicate and counts contract.
replace_once(
    "app/routers/items.py",
    "from app.services import isbn as isbn_svc\n",
    "from app.services import isbn as isbn_svc\nfrom app.services import libraries\n",
)
replace_once(
    "app/routers/items.py",
    """    templates = request.app.state.templates

    values = browse_filters.values_from(request.query_params)
""",
    """    templates = request.app.state.templates
    user = dict(request.state.user)

    values = browse_filters.values_from(request.query_params)
""",
)
replace_once(
    "app/routers/items.py",
    """    where, params = browse_filters.build_where(values)
    _, order_clause = SORT_OPTIONS.get(sort, SORT_OPTIONS["newest"])
""",
    """    where, params = browse_filters.build_where(values)
    where, params = libraries.scope_where(where, params, user)
    _, order_clause = SORT_OPTIONS.get(sort, SORT_OPTIONS["newest"])
""",
)
replace_once(
    "app/routers/items.py",
    "counts = items_common.filter_counts(db, values, total) if page <= 1 else None",
    "counts = items_common.filter_counts(db, values, total, user=user) if page <= 1 else None",
)

# Current-upstream tests: explicit assignments/memberships make the security
# contract independent of legacy recovery fixtures.
test = Path("tests/test_library_browse_access.py")
test.write_text(r'''"""Security regressions for per-library Browse and HTMX search visibility."""

import pytest

from app.services import libraries
from tests.conftest import _insert_item


def _assigned_item(db, title: str, isbn: str, library_id: int, **fields) -> int:
    item_id = _insert_item(db, title=title, isbn=isbn, **fields)
    libraries.assign_item(db, item_id, library_id)
    return item_id


def test_browse_and_search_hide_inaccessible_library_items(viewer_client, viewer_user, db):
    libraries.set_membership(db, 1, viewer_user["id"], "viewer")
    visible_id = _assigned_item(db, "Visible Shelf Book", "9780000008805", 1)
    secret = libraries.create_library(db, "Private Library")
    secret_id = _assigned_item(db, "Hidden Shelf Book", "9780000008812", secret["id"])
    assert libraries.has_item_role(db, viewer_user, visible_id)
    assert not libraries.has_item_role(db, viewer_user, secret_id)
    db.commit()

    browse = viewer_client.get("/browse")
    assert browse.status_code == 200
    assert "Visible Shelf Book" in browse.text
    assert "Hidden Shelf Book" not in browse.text

    search = viewer_client.get("/api/search?q=Shelf+Book")
    assert search.status_code == 200
    assert "Visible Shelf Book" in search.text
    assert "Hidden Shelf Book" not in search.text


def test_filter_options_do_not_leak_hidden_catalogue_metadata(viewer_client, viewer_user, db):
    libraries.set_membership(db, 1, viewer_user["id"], "viewer")
    _assigned_item(
        db, "Public Series Book", "9780000008829", 1,
        series_name="Public Saga", language="en",
    )
    secret = libraries.create_library(db, "Secret")
    secret_id = _assigned_item(
        db, "Secret Series Book", "9780000008836", secret["id"],
        series_name="Secret Saga", language="zz-secret",
    )
    loc_id = db.execute("INSERT INTO locations (name) VALUES ('Secret Vault')").lastrowid
    db.execute("UPDATE items SET location_id = ? WHERE id = ?", (loc_id, secret_id))
    tag_id = db.execute("INSERT INTO tags (name) VALUES ('Classified Tag')").lastrowid
    db.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (secret_id, tag_id))
    db.commit()

    html = viewer_client.get("/browse").text
    assert "Public Saga" in html
    assert "Secret Saga" not in html
    assert "Classified Tag" not in html
    assert "zz-secret" not in html
    assert "Secret Vault" not in html


def test_filter_counts_exclude_inaccessible_items(viewer_client, viewer_user, db):
    libraries.set_membership(db, 1, viewer_user["id"], "viewer")
    _assigned_item(db, "Visible Book", "9780000008843", 1, owned=1)
    secret = libraries.create_library(db, "Hidden Counts")
    _assigned_item(db, "Hidden Wishlist", "9780000008850", secret["id"], owned=0)
    db.commit()

    html = viewer_client.get("/browse").text
    assert "Hidden Wishlist" not in html
    assert "Wishlist (0)" in html or ">Wishlist<" not in html


def test_admin_retains_global_recovery_visibility(admin_client, db):
    secret = libraries.create_library(db, "Admin Recovery")
    _assigned_item(db, "Admin Can See This", "9780000008928", secret["id"])
    db.commit()
    response = admin_client.get("/browse")
    assert response.status_code == 200
    assert "Admin Can See This" in response.text


def test_access_predicate_is_bound_and_role_aware(viewer_user):
    sql, params = libraries.item_access_condition(viewer_user, item_alias="catalogue_item")
    assert "catalogue_item.id" in sql
    assert "lm.user_id = ?" in sql
    assert f"lm.user_id = {viewer_user['id']}" not in sql
    assert sql.count("?") == 1
    assert params == [viewer_user["id"]]

    editor_sql, editor_params = libraries.item_access_condition(
        viewer_user, item_alias="i", minimum_role="editor"
    )
    assert "('editor')" in editor_sql
    assert editor_sql.count("?") == 1
    assert editor_params == [viewer_user["id"]]


def test_access_predicate_rejects_unknown_roles_and_aliases(viewer_user):
    with pytest.raises(ValueError, match="Unknown library role"):
        libraries.item_access_condition(viewer_user, minimum_role="owner")
    with pytest.raises(ValueError, match="Invalid SQL item alias"):
        libraries.item_access_condition(viewer_user, item_alias="i; DROP TABLE items")
''')
