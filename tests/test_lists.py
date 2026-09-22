"""Tests for `app.services.lists`, the one write path for `list_items`.

Mirrors the shape of `tests/test_item_write.py`'s `item_copies` guards
(`test_only_item_copies_module_inserts_copies` /
`_deletes_copies` / `_updates_copies` and
`test_the_copy_module_is_exempted_by_path_not_by_basename`), applied to
`list_items` instead of `item_copies`.
"""

import re
from pathlib import Path

import pytest

from tests.conftest import _assert_ownership_partition, _insert_item

from app.services.lists import (
    WISHLIST,
    WISHLISTED_SQL,
    UnknownList,
    add,
    is_member,
    list_id,
    remove,
    reparent,
    set_membership,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / "app"

#: The one module allowed to write `list_items` rows, as a repository-relative
#: path. Matched exactly rather than by basename (G88): `path.name` would
#: exempt any file called `lists.py` anywhere under `app/`, including a future
#: `app/routers/lists.py`.
LISTS_MODULE = "app/services/lists.py"

#: `app/database.py` carries exactly one raw write to `list_items`: migration
#: 36's seed, which runs before this module is importable. See the module
#: docstring in `app/services/lists.py`.
DATABASE_MODULE = "app/database.py"


def _raw_update_hits(path: Path, needle: str, flags: int = 0) -> list[tuple[int, str]]:
    """(line_number, clause_tail) for every occurrence of `needle` in `path`.

    Copied from `tests/test_item_write.py::_raw_update_hits` (this module may
    not import from a sibling test module's private helper across a task
    boundary, so the logic is duplicated rather than shared). Comment-only
    lines are dropped first (G53), then double quotes are stripped and lines
    are joined with a single space, so a statement split across adjacent
    string literals or a triple-quoted multi-line string still reads as one
    fragment (G88 / M1).
    """
    lines = path.read_text().splitlines()
    buf_parts: list[str] = []
    offsets: list[tuple[int, int]] = []
    pos = 0
    for i, raw_line in enumerate(lines, 1):
        if raw_line.lstrip().startswith("#"):
            continue
        collapsed = re.sub(r"\s+", " ", raw_line.replace('"', "")).strip()
        if not collapsed:
            continue
        offsets.append((pos, i))
        buf_parts.append(collapsed)
        pos += len(collapsed) + 1  # +1 for the joining space
    buf = " ".join(buf_parts)

    def _line_for(offset: int) -> int:
        line_no = offsets[0][1] if offsets else 1
        for start, ln in offsets:
            if start > offset:
                break
            line_no = ln
        return line_no

    hits = []
    for m in re.finditer(re.escape(needle), buf, flags):
        hits.append((_line_for(m.start()), buf[m.end():m.end() + 300]))
    return hits


class TestSingleWritePath:
    def test_only_lists_module_inserts_list_items(self):
        offenders = []
        for path in APP_DIR.rglob("*.py"):
            rel = str(path.relative_to(REPO_ROOT))
            if rel in (LISTS_MODULE, DATABASE_MODULE):
                continue
            for line_no, _ in _raw_update_hits(path, "INSERT INTO list_items", re.I):
                offenders.append(f"{rel}:{line_no}")
        assert not offenders, (
            "list_items rows must be created through app.services.lists "
            "(add/set_membership), not raw SQL:\n  " + "\n  ".join(offenders)
        )

    def test_only_lists_module_updates_list_items(self):
        """Both spellings. The needle was `UPDATE list_items SET` alone until
        `lists.reparent` introduced the first `UPDATE OR IGNORE list_items
        SET` in the tree — a conflict-tolerant update is still an update, and
        a literal needle that cannot see it would wave the next one through
        from outside the module."""
        offenders = []
        for needle in ("UPDATE list_items SET", "UPDATE OR IGNORE list_items SET"):
            for path in APP_DIR.rglob("*.py"):
                rel = str(path.relative_to(REPO_ROOT))
                if rel in (LISTS_MODULE, DATABASE_MODULE):
                    continue
                for line_no, _ in _raw_update_hits(path, needle, re.I):
                    offenders.append(f"{rel}:{line_no}")
        assert not offenders, (
            "list_items has no update path outside the module; a raw "
            "`UPDATE [OR IGNORE] list_items SET` outside app.services.lists "
            "is a funnel bypass:\n  " + "\n  ".join(sorted(set(offenders)))
        )

    def test_only_lists_module_deletes_list_items(self):
        offenders = []
        for path in APP_DIR.rglob("*.py"):
            rel = str(path.relative_to(REPO_ROOT))
            if rel == LISTS_MODULE:
                continue
            # No migration deletes list_items rows, so unlike the insert
            # guard, app/database.py earns no exemption here.
            for line_no, _ in _raw_update_hits(path, "DELETE FROM list_items", re.I):
                offenders.append(f"{rel}:{line_no}")
        assert not offenders, (
            "list_items rows must be removed through app.services.lists "
            "(remove/set_membership), not raw SQL:\n  " + "\n  ".join(offenders)
        )

    def test_lists_module_is_exempted_by_path_not_by_basename(self):
        """M1's bypass, replayed for this table: `path.name == 'lists.py'`
        would exempt any same-named file anywhere under app/, so a future
        `app/routers/lists.py` must not be waved through by this guard."""
        assert LISTS_MODULE == "app/services/lists.py"
        assert (REPO_ROOT / LISTS_MODULE).is_file()
        assert "app/routers/lists.py" != LISTS_MODULE

    def test_insert_detection_sees_adjacent_string_literals(self, tmp_path):
        """M1's second bypass: scanning one physical line at a time misses a
        statement split across adjacent literals."""
        split = tmp_path / "split.py"
        split.write_text(
            'db.execute(\n'
            '    "INSERT INTO "\n'
            '    "list_items (list_id, item_id) VALUES (?, ?)",\n'
            ')\n'
        )
        assert _raw_update_hits(split, "INSERT INTO list_items", re.I)

        lowered = tmp_path / "lowered.py"
        lowered.write_text('db.execute("insert into list_items (list_id) VALUES (?)")\n')
        assert _raw_update_hits(lowered, "INSERT INTO list_items", re.I)

        # G53 still holds: a comment quoting the construct is not a write.
        quoted = tmp_path / "quoted.py"
        quoted.write_text("# INSERT INTO list_items is described here, not run\n")
        assert not _raw_update_hits(quoted, "INSERT INTO list_items", re.I)


class TestListId:
    def test_resolves_the_seeded_wishlist(self, db):
        lid = list_id(db, WISHLIST)
        row = db.execute("SELECT slug FROM lists WHERE id = ?", (lid,)).fetchone()
        assert row["slug"] == "wishlist"

    def test_unknown_slug_raises(self, db):
        with pytest.raises(UnknownList):
            list_id(db, "no-such-list")


class TestAddRemove:
    def test_add_is_idempotent(self, db):
        item_id = _insert_item(db, owned=0, wishlisted=True)
        add(db, WISHLIST, item_id)
        add(db, WISHLIST, item_id)
        count = db.execute(
            "SELECT COUNT(*) AS n FROM list_items WHERE item_id = ?", (item_id,)
        ).fetchone()["n"]
        assert count == 1
        assert is_member(db, WISHLIST, item_id)

    def test_remove_of_non_member_is_a_no_op(self, db):
        item_id = _insert_item(db, owned=1)
        assert not is_member(db, WISHLIST, item_id)
        remove(db, WISHLIST, item_id)  # must not raise
        assert not is_member(db, WISHLIST, item_id)

    def test_remove_removes_a_member(self, db):
        item_id = _insert_item(db, owned=0, wishlisted=True)
        add(db, WISHLIST, item_id)
        assert is_member(db, WISHLIST, item_id)
        remove(db, WISHLIST, item_id)
        assert not is_member(db, WISHLIST, item_id)

    def test_add_unknown_slug_raises(self, db):
        item_id = _insert_item(db)
        with pytest.raises(UnknownList):
            add(db, "no-such-list", item_id)

    def test_remove_unknown_slug_raises(self, db):
        item_id = _insert_item(db)
        with pytest.raises(UnknownList):
            remove(db, "no-such-list", item_id)

    def test_is_member_unknown_slug_raises(self, db):
        item_id = _insert_item(db)
        with pytest.raises(UnknownList):
            is_member(db, "no-such-list", item_id)


class TestSetMembership:
    def test_adds_three_ids_in_one_call(self, db):
        ids = [_insert_item(db, isbn=f"978000000{n:04d}") for n in range(3)]
        set_membership(db, WISHLIST, ids, True)
        for item_id in ids:
            assert is_member(db, WISHLIST, item_id)

    def test_removes_three_ids_in_one_call(self, db):
        ids = [_insert_item(db, isbn=f"978000000{n:04d}") for n in range(3)]
        set_membership(db, WISHLIST, ids, True)
        set_membership(db, WISHLIST, ids, False)
        for item_id in ids:
            assert not is_member(db, WISHLIST, item_id)

    def test_empty_iterable_writes_nothing(self, db):
        before = db.execute("SELECT COUNT(*) AS n FROM list_items").fetchone()["n"]
        set_membership(db, WISHLIST, [], True)
        set_membership(db, WISHLIST, [], False)
        after = db.execute("SELECT COUNT(*) AS n FROM list_items").fetchone()["n"]
        assert after == before

    def test_unknown_slug_raises(self, db):
        item_id = _insert_item(db)
        with pytest.raises(UnknownList):
            set_membership(db, "no-such-list", [item_id], True)

    def test_unknown_slug_raises_even_for_empty_iterable(self, db):
        """The list is resolved before the empty check, so an unknown slug is
        still an error rather than a silent no-op."""
        with pytest.raises(UnknownList):
            set_membership(db, "no-such-list", [], True)


class TestWishlistedSql:
    def test_true_for_a_wishlisted_item(self, db):
        item_id = _insert_item(db, owned=0, wishlisted=True)
        add(db, WISHLIST, item_id)
        row = db.execute(
            f"SELECT {WISHLISTED_SQL} AS w FROM items i WHERE i.id = ?",
            (item_id,),
        ).fetchone()
        assert bool(row["w"]) is True

    def test_false_for_a_non_wishlisted_item(self, db):
        item_id = _insert_item(db, owned=1)
        row = db.execute(
            f"SELECT {WISHLISTED_SQL} AS w FROM items i WHERE i.id = ?",
            (item_id,),
        ).fetchone()
        assert bool(row["w"]) is False


class TestReparent:
    """`reparent` moves a merged row's memberships onto the kept row.

    Called from `item_merge.reparent_children`; the route-level pin for the
    defect it closes lives in `tests/test_mutation_integrity.py`.
    """

    def _other_list(self, db, slug="reading-soon"):
        db.execute(
            "INSERT INTO lists (slug, name) VALUES (?, ?)", (slug, "Reading soon")
        )
        return slug

    def test_membership_moves_to_the_kept_row(self, db):
        keep = _insert_item(db, title="Keep", isbn=None, owned=0)
        other = _insert_item(db, title="Other", isbn=None, owned=0)
        add(db, WISHLIST, other)

        reparent(db, keep, other)

        assert is_member(db, WISHLIST, keep)
        assert not is_member(db, WISHLIST, other)

    def test_a_list_both_rows_are_on_collapses_to_one_row(self, db):
        keep = _insert_item(db, title="Keep", isbn=None, owned=0)
        other = _insert_item(db, title="Other", isbn=None, owned=0)
        add(db, WISHLIST, keep)
        add(db, WISHLIST, other)

        reparent(db, keep, other)

        assert db.execute(
            "SELECT COUNT(*) AS c FROM list_items WHERE item_id = ?", (keep,)
        ).fetchone()["c"] == 1
        assert not is_member(db, WISHLIST, other)

    def test_an_owned_keeper_sheds_the_moved_wishlist_membership(self, db):
        keep = _insert_item(db, title="Keep", isbn=None, owned=1)
        other = _insert_item(db, title="Other", isbn=None, owned=0)
        add(db, WISHLIST, other)

        reparent(db, keep, other)

        assert not is_member(db, WISHLIST, keep)
        _assert_ownership_partition(db)

    def test_an_unowned_keeper_keeps_it(self, db):
        keep = _insert_item(db, title="Keep", isbn=None, owned=0)
        other = _insert_item(db, title="Other", isbn=None, owned=0)
        add(db, WISHLIST, other)

        reparent(db, keep, other)

        assert is_member(db, WISHLIST, keep)

    @pytest.mark.parametrize("owned", [0, 1])
    def test_a_non_wishlist_list_moves_regardless_of_owned(self, db, owned):
        """Only the wishlist carries the ownership rule; every other list is
        an ordinary membership and moves either way."""
        slug = self._other_list(db)
        keep = _insert_item(db, title="Keep", isbn=None, owned=owned)
        other = _insert_item(db, title="Other", isbn=None, owned=owned)
        add(db, slug, other)

        reparent(db, keep, other)

        assert is_member(db, slug, keep)
        assert not is_member(db, slug, other)

    def test_a_row_with_no_memberships_is_a_no_op(self, db):
        keep = _insert_item(db, title="Keep", isbn=None, owned=0)
        other = _insert_item(db, title="Other", isbn=None, owned=0)

        reparent(db, keep, other)

        assert not is_member(db, WISHLIST, keep)
