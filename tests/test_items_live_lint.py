"""Soft-delete seam (T3/T7) — the items_live read lint runs with the unit suite.

Every read of `items` under app/ must go through the `items_live` TEMP view
(app/database.py::get_db()) rather than the physical table. T4-T6 repointed
the ~172 existing direct reads this lint used to (correctly) flag; the
census is now 0. This wraps scripts/check_items_live.py the way
tests/test_csrf_lint.py wraps scripts/check_csrf_fetch.py, and `check-deleted`
runs as part of `make checks-fast` (T7).

The first two tests below run against the real app/ tree and are the
census-zero contract — they must stay green as app/ grows. Every other test
in this file builds its own two-file `tmp_path/app/...` scratch tree and
calls `find_violations(tmp_path)` / `allowlist_mismatches(tmp_path)`
directly, pinning one matcher shape each.

G31: each shape pin below was verified against a hand-mutated scratch copy
of check_items_live.py — the docstring on each test names the mutation that
must turn it red, and __pycache__ was cleared (G97) between the mutated and
restored runs before re-testing.
"""

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_items_live.py"
_spec = importlib.util.spec_from_file_location("check_items_live", _SCRIPT)
check_items_live = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_items_live)


def _write_tree(tmp_path: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def test_every_read_goes_through_items_live():
    """The census-zero contract, run against the real app/ tree (not a
    tmp_path fixture, unlike every other test in this file). T4-T6 repointed
    all ~172 direct reads onto items_live; this is the gate that keeps the
    census at 0 going forward — a new direct read of items anywhere in app/
    reds this test (and make check-deleted) rather than shipping unnoticed.
    """
    violations = check_items_live.find_violations()
    assert not violations, "\n" + "\n".join(violations)


def test_allowlist_entries_span_exactly_their_declared_read_count():
    """Every entry in the real ALLOWLIST must span exactly the number of
    reads declared beside it — 0 is the stale case (an entry the code no
    longer produces, silently over-permissive), and more than declared is
    the ride-along case (a new direct read written inside text an existing
    entry already covers). Both must fail the suite, not sit there unused."""
    mismatches = check_items_live.allowlist_mismatches()
    assert not mismatches, "\n" + "\n".join(mismatches)


def test_catches_from_join_and_lowercase_shapes(tmp_path):
    """Pins the four base shapes: `FROM items`, `JOIN items`, `LEFT JOIN
    items i`, and lowercase `from items`.

    Mutation -> pin: verified by hand — dropping `re.I` from `_ITEMS_READ`
    reds only the lowercase case (2/4 hits survive); narrowing the
    `(FROM|JOIN)` alternation to `FROM` only reds the `JOIN` and `LEFT JOIN`
    cases (LEFT is not part of the match, only a prefix before it).
    """
    _write_tree(tmp_path, {
        "app/routers/basic.py": (
            'def f(db):\n'
            '    db.execute("SELECT * FROM items WHERE id = ?")\n'
            '    db.execute("SELECT c.id FROM checkouts c JOIN items i ON c.item_id = i.id")\n'
            '    db.execute("SELECT * FROM checkouts c LEFT JOIN items i ON c.item_id = i.id")\n'
            '    db.execute("select * from items where id = ?")\n'
        ),
    })
    violations = check_items_live.find_violations(tmp_path)
    assert len(violations) == 4
    for line in (2, 3, 4, 5):
        assert any(f"basic.py:{line}" in v for v in violations), (line, violations)


def test_catches_adjacent_literal_split(tmp_path):
    """Pins a statement split across two adjacent plain string literals:
    `"SELECT id FROM " "items WHERE id = ?"`.

    Mutation -> pin: verified by hand — joining `buf_parts` with `"".join`
    instead of `" ".join` in `_normalise_file` glues the two fragments into
    `...FROMitems...`, which breaks the `\b` boundary between `FROM` and
    `items` (both become word characters with no boundary), and this pin
    reds.
    """
    _write_tree(tmp_path, {
        "app/routers/split.py": (
            'def f(db):\n'
            '    db.execute(\n'
            '        "SELECT id FROM "\n'
            '        "items WHERE id = ?"\n'
            '    )\n'
        ),
    })
    violations = check_items_live.find_violations(tmp_path)
    assert len(violations) == 1
    assert "split.py:3" in violations[0]


def test_catches_fstring_split_where_from_opens_its_own_fragment(tmp_path):
    """Pins the exact shape of app/routers/items.py:901: an f-string split
    where `FROM` opens its own fragment — `f"{sql} AS wishlisted "
    f"FROM items i "`.

    Mutation -> pin: removing the `_STRING_PREFIX_QUOTE` strip step (leaving
    only the bare-quote strip) turns `f"FROM items i "` into `fFROM items i`,
    and `\\bFROM` no longer matches since `f` and `F` are both word
    characters with no boundary between them — the exact regression this
    task's spec calls out (169 instead of 172 violations).
    """
    _write_tree(tmp_path, {
        "app/routers/fsplit.py": (
            'def f(db, sql):\n'
            '    items = db.execute(\n'
            '        f"{sql} AS wishlisted "\n'
            '        f"FROM items i "\n'
            '        "WHERE 1=1"\n'
            '    ).fetchall()\n'
        ),
    })
    violations = check_items_live.find_violations(tmp_path)
    assert len(violations) == 1
    assert "fsplit.py:4" in violations[0]


def test_catches_fstring_split_join_twin(tmp_path):
    """The JOIN twin of the FROM f-string-fragment pin above:
    `f"{sql} AS wishlisted " f"LEFT JOIN items i ON ..."`.

    Verified by hand: removing `_STRING_PREFIX_QUOTE` entirely (the mutation
    that reds the FROM f-string-fragment pin above) leaves this test green —
    `fLEFT JOIN items i ON...` still matches `\bJOIN` fine, since the `f`
    prefix sits before `JOIN`, not glued to it. This test exists precisely
    to prove that: it is not itself reddened by the prefix-strip mutation,
    which is why the FROM-opens-its-own-fragment test above is the one that
    actually needs the prefix strip.
    """
    _write_tree(tmp_path, {
        "app/routers/fsplit_join.py": (
            'def f(db, sql):\n'
            '    items = db.execute(\n'
            '        f"{sql} AS wishlisted "\n'
            '        f"LEFT JOIN items i ON i.id = x.id "\n'
            '    ).fetchall()\n'
        ),
    })
    violations = check_items_live.find_violations(tmp_path)
    assert len(violations) == 1
    assert "fsplit_join.py:4" in violations[0]


def test_catches_rb_and_u_string_prefixes(tmp_path):
    """Pins the FROM/JOIN f-string-fragment shapes above with `rb` and `u`
    string prefixes instead of `f` — the prefix-strip regex is
    `[rRbBuUfF]{1,2}` precisely so multi-letter and non-f prefixes strip too.

    Mutation -> pin: narrowing `_STRING_PREFIX_QUOTE` to `f"` only (dropping
    the other letters from the character class) reds both statements here
    while leaving the plain `f"..."` pins above green.
    """
    _write_tree(tmp_path, {
        "app/routers/prefixes.py": (
            'def f(db, sql):\n'
            '    a = db.execute(\n'
            '        rb"{sql} AS a "\n'
            '        rb"FROM items i "\n'
            '    ).fetchall()\n'
            '    b = db.execute(\n'
            '        u"{sql} AS b "\n'
            '        u"JOIN items i "\n'
            '    ).fetchall()\n'
        ),
    })
    violations = check_items_live.find_violations(tmp_path)
    assert len(violations) == 2
    assert any("prefixes.py:4" in v for v in violations)
    assert any("prefixes.py:8" in v for v in violations)


def test_catches_triple_quoted_from_items_on_its_own_line(tmp_path):
    """Pins a triple-quoted multi-line SQL statement with `FROM items` on
    its own line — the migrations-in-database.py shape.

    Mutation -> pin: verified by hand — joining `buf_parts` with `"".join`
    instead of `" ".join` glues this statement's `FROM items` line directly
    onto the next line's `WHERE`, producing `itemsWHERE`. That erases the
    trailing `\b` boundary the pattern needs right after `items` (both `s`
    and `W` are word characters), so the match disappears entirely and this
    pin reds.
    """
    _write_tree(tmp_path, {
        "app/database_like.py": (
            'MIGRATIONS = (\n'
            '    (1, "x", """\n'
            '    SELECT *\n'
            '    FROM items\n'
            '    WHERE id = ?\n'
            '    """),\n'
            ')\n'
        ),
    })
    violations = check_items_live.find_violations(tmp_path)
    assert len(violations) == 1
    assert "database_like.py:4" in violations[0]


def test_ignores_delete_comment_items_live_item_copies_yield_and_import(tmp_path):
    """Pins every excluded shape in one file: `DELETE FROM items ...` (a
    write, stays on the table), a `#` comment quoting `FROM items` (G53),
    `FROM items_live i`, `FROM item_copies`, `yield from items`, and
    `from items import x`.

    Mutation -> pin: dropping the `(?<!DELETE )` lookbehind reds the DELETE
    line; dropping the `#`-comment-line skip reds the comment line; either
    is a real regression this test would catch. `items_live`/`item_copies`
    are protected by `\\b` not splitting on `_`, not by any lookbehind/
    lookahead, so they need no mutation to prove — they are pinned as a
    guard against a future rewrite that stops using `\\b`.
    """
    _write_tree(tmp_path, {
        "app/routers/ignores.py": (
            'def f(db):\n'
            '    db.execute("DELETE FROM items WHERE id = ?")\n'
            '    # a comment saying FROM items should never trip this lint\n'
            '    db.execute("SELECT * FROM items_live i")\n'
            '    db.execute("SELECT * FROM item_copies")\n'
            '    yield from items\n'
            'from items import x\n'
        ),
    })
    violations = check_items_live.find_violations(tmp_path)
    assert violations == []


def test_allowlist_suppresses_only_its_own_statement(tmp_path):
    """An allowlisted substring suppresses exactly the one statement it
    names, not a neighbouring statement in the same file — even one that
    also reads `items` directly and sits in a file that has an ALLOWLIST
    entry.

    Uses the real `app/database.py` allowlist entry (the items_live view's
    own CREATE) so this exercises the production ALLOWLIST dict, not a test
    stand-in. The 20 filler lines put the two statements far apart; the two
    adjacency tests below are the same shape with the filler removed, which
    is the case that actually matters (see their docstrings).

    Mutation -> pin: verified by hand — changing `_spanning` to a plain
    `sub in buf` test (checking the whole file instead of the match's own
    extent) makes the allowlisted substring, which is present anywhere in
    the file, wrongly suppress the second, non-allowlisted statement too —
    violations drops from 1 to 0 and this pin reds.
    """
    filler = "\n".join(
        f'    filler_{i} = "0123456789abcdefghijklmnopqrstuvwxyz_padding_line"'
        for i in range(20)
    )
    content = (
        'def f(db):\n'
        '    db.execute("SELECT * FROM items WHERE deleted_at IS NULL")\n'
        f'{filler}\n'
        '    db.execute("SELECT * FROM items WHERE id = ?")\n'
    )
    _write_tree(tmp_path, {"app/database.py": content})
    violations = check_items_live.find_violations(tmp_path)
    assert len(violations) == 1
    last_line = content.count("\n")  # the final statement's 1-based line number
    assert f"database.py:{last_line}:" in violations[0]
    assert "database.py:2:" not in violations[0]


def test_allowlist_suppresses_a_read_directly_after_its_own_statement(tmp_path):
    """The adjacency case the 20 filler lines above hide: a non-allowlisted
    read on the very next line after the allowlisted one.

    This is the shape the pre-fix lint got wrong. It checked a
    ±200/300-character window around each match for an allowlisted
    substring, so the second statement here — well inside that window — was
    silently excused. `_spanning` ties the exemption to the statement the
    entry actually names, and the neighbour reds.

    Mutation -> pin: verified by hand — restoring the window check
    (`sub in buf[max(0, m.start() - 200): m.end() + 300]`) drops violations
    from 1 to 0 and reds this pin. The filler-separated test above stays
    green under that same mutation, which is why this test exists.
    """
    content = (
        'def f(db):\n'
        '    db.execute("SELECT * FROM items WHERE deleted_at IS NULL")\n'
        '    db.execute("SELECT * FROM items WHERE id = ?")\n'
    )
    _write_tree(tmp_path, {"app/database.py": content})
    violations = check_items_live.find_violations(tmp_path)
    assert len(violations) == 1, violations
    assert "database.py:3:" in violations[0]


def test_allowlist_suppresses_a_read_directly_before_its_own_statement(tmp_path):
    """The reverse order of the test above — the non-allowlisted read comes
    FIRST, and the allowlisted statement follows it.

    Both orders are pinned because the pre-fix window was asymmetric
    (CONTEXT_BEFORE = 200, CONTEXT_AFTER = 300): a fixture in one order can
    pass while the other fails. `_spanning` has no direction, and this pin
    is what says so.

    Mutation -> pin: verified by hand — the restored window check reds this
    one too.
    """
    content = (
        'def f(db):\n'
        '    db.execute("SELECT * FROM items WHERE id = ?")\n'
        '    db.execute("SELECT * FROM items WHERE deleted_at IS NULL")\n'
    )
    _write_tree(tmp_path, {"app/database.py": content})
    violations = check_items_live.find_violations(tmp_path)
    assert len(violations) == 1, violations
    assert "database.py:2:" in violations[0]


def test_allowlist_mismatches_reports_a_substring_the_code_no_longer_produces(
    tmp_path,
):
    """A real ALLOWLIST substring that spans no hit in its file is reported
    by allowlist_mismatches — the
    test_raw_update_allowlist_has_no_stale_entries pattern from
    tests/test_item_write.py, applied to this lint's own ALLOWLIST.

    The fixture tree here contains no app/ files at all, so every entry in
    the real ALLOWLIST is stale against it; this test only checks that one
    specific, known entry shows up, not that the list is exhaustive.

    Mutation -> pin: verified by hand — replacing the `got != want` test
    with `False` (nothing is ever reported) reds this pin.
    """
    _write_tree(tmp_path, {"app/__init__.py": "\n"})
    mismatches = check_items_live.allowlist_mismatches(tmp_path)
    assert any(
        "app/database.py" in s and "SELECT 1 FROM items o WHERE o.upc =" in s
        for s in mismatches
    )


def test_allowlist_mismatches_reports_a_read_riding_along_inside_an_entry(
    tmp_path, monkeypatch
):
    """The other half of the count: an entry that spans MORE reads than it
    declares. That is a new direct read written inside text an existing
    entry already covers — exempted without anyone deciding it should be.

    The fixture declares a one-hit entry and then gives the file two
    statements matching it, so the count goes 1 -> 2 and the entry is
    reported. Without the count, `find_violations` stays silent on both.

    Mutation -> pin: verified by hand — narrowing the check to the stale
    case only (`if got == 0`) leaves this pin red-free and it fails.
    """
    monkeypatch.setitem(
        check_items_live.ALLOWLIST,
        "app/ride.py",
        {"SELECT id FROM items WHERE legacy = 1": 1},
    )
    _write_tree(tmp_path, {
        "app/ride.py": (
            'def f(db):\n'
            '    db.execute("SELECT id FROM items WHERE legacy = 1")\n'
            '    db.execute("SELECT id FROM items WHERE legacy = 1")\n'
        ),
    })
    assert check_items_live.find_violations(tmp_path) == []
    mismatches = check_items_live.allowlist_mismatches(tmp_path)
    assert any(
        "app/ride.py" in s and "spans 2 read(s), expected 1" in s
        for s in mismatches
    ), mismatches


def test_main_reports_a_violation_and_exits_non_zero(capsys, monkeypatch):
    """The rules above are all exercised through find_violations() and
    allowlist_mismatches() directly. This is the one that proves main()
    calls them at all — flip either `return 1` to `return 0`, or drop either
    call from main(), and every other test in this file still passes while
    `make check-deleted` goes green on any tree.

    Mirrors test_main_reports_a_load_order_violation_and_exits_non_zero in
    tests/test_alpine_csp_lint.py, for the same reason.
    """
    sentinel = "SENTINEL items_live violation"
    monkeypatch.setattr(check_items_live, "find_violations", lambda *a, **k: [sentinel])
    monkeypatch.setattr(check_items_live, "allowlist_mismatches", lambda *a, **k: [])
    assert check_items_live.main() == 1
    assert sentinel in capsys.readouterr().out


def test_main_reports_an_allowlist_mismatch_and_exits_non_zero(capsys, monkeypatch):
    """The mismatch half of main()'s contract: a clean violation census is
    not enough to exit 0 if an allowlist entry no longer spans what it
    declares. Dropping the allowlist_mismatches() call from main() reds this
    while leaving the violation test above green."""
    sentinel = "SENTINEL allowlist mismatch"
    monkeypatch.setattr(check_items_live, "find_violations", lambda *a, **k: [])
    monkeypatch.setattr(
        check_items_live, "allowlist_mismatches", lambda *a, **k: [sentinel]
    )
    assert check_items_live.main() == 1
    assert sentinel in capsys.readouterr().out


def test_both_fixture_files_appear_in_output(tmp_path):
    """G31: a single-file fixture pins nothing about per-file state. Two
    files, each with its own violation, must both be named in the output —
    proving find_violations walks the whole tree rather than, say, stopping
    at the first hit or the first file.

    Mutation -> pin: an implementation that `return`s after the first
    violating file (instead of continuing the `for path in ...` loop) reds
    this by reporting only one of the two filenames.
    """
    _write_tree(tmp_path, {
        "app/routers/one.py": 'db.execute("SELECT * FROM items WHERE id = ?")\n',
        "app/routers/two.py": 'db.execute("SELECT * FROM items WHERE id = ?")\n',
    })
    violations = check_items_live.find_violations(tmp_path)
    assert any("one.py" in v for v in violations)
    assert any("two.py" in v for v in violations)


# --- item_copies -> copies_live (T2 of the soft-delete-copies-seam plan) ---
#
# The tests below pin find_copy_violations()/copy_allowlist_mismatches(),
# the item_copies twin of find_violations()/allowlist_mismatches() above.
# They share the same _normalise_file/_spanning machinery (see
# check_items_live.py's `_find_violations`/`_allowlist_mismatches` helpers),
# so most of the mutations that would red these are the same mutations that
# red the items-side pins above — each docstring below says which.


def test_copies_census_has_no_allowlist_mismatches():
    """The copies allowlist DOES gate main() from this task onward, even
    though the copies violation census itself does not yet (see the module
    docstring). Every COPIES_ALLOWLIST entry must span exactly the read
    count declared beside it, against the real app/ tree, today.
    """
    mismatches = check_items_live.copy_allowlist_mismatches()
    assert not mismatches, "\n" + "\n".join(mismatches)


def test_catches_copies_from_join_and_lowercase_shapes(tmp_path):
    """The item_copies twin of test_catches_from_join_and_lowercase_shapes:
    `FROM item_copies`, `JOIN item_copies`, `LEFT JOIN item_copies c`, and
    lowercase `from item_copies`.

    Mutation -> pin: verified by hand — dropping `re.I` from `_COPIES_READ`
    reds only the lowercase case (2/4 hits survive); narrowing the
    `(FROM|JOIN)` alternation to `FROM` only reds the `JOIN` and `LEFT JOIN`
    cases.
    """
    _write_tree(tmp_path, {
        "app/routers/basic_copies.py": (
            'def f(db):\n'
            '    db.execute("SELECT * FROM item_copies WHERE id = ?")\n'
            '    db.execute("SELECT c.id FROM items i JOIN item_copies c ON c.item_id = i.id")\n'
            '    db.execute("SELECT * FROM items i LEFT JOIN item_copies c ON c.item_id = i.id")\n'
            '    db.execute("select * from item_copies where id = ?")\n'
        ),
    })
    violations = check_items_live.find_copy_violations(tmp_path)
    assert len(violations) == 4
    for line in (2, 3, 4, 5):
        assert any(f"basic_copies.py:{line}" in v for v in violations), (line, violations)


def test_catches_copies_adjacent_literal_split(tmp_path):
    """The item_copies twin of test_catches_adjacent_literal_split: a
    statement split across two adjacent plain string literals,
    `"SELECT id FROM " "item_copies WHERE id = ?"`.

    Mutation -> pin: verified by hand — joining `buf_parts` with `"".join`
    instead of `" ".join` in `_normalise_file` glues the two fragments into
    `...FROMitem_copies...`, breaking the `\b` boundary between `FROM` and
    `item_copies`, and this pin reds.
    """
    _write_tree(tmp_path, {
        "app/routers/split_copies.py": (
            'def f(db):\n'
            '    db.execute(\n'
            '        "SELECT id FROM "\n'
            '        "item_copies WHERE id = ?"\n'
            '    )\n'
        ),
    })
    violations = check_items_live.find_copy_violations(tmp_path)
    assert len(violations) == 1
    assert "split_copies.py:3" in violations[0]


def test_catches_copies_fstring_prefix(tmp_path):
    """Pins the f-string-fragment shape from app/routers/items.py:901,
    ported to item_copies: `f"{sql} AS x " f"FROM item_copies c "`.

    Mutation -> pin: removing the `_STRING_PREFIX_QUOTE` strip step (leaving
    only the bare-quote strip) turns `f"FROM item_copies c "` into
    `fFROM item_copies c`, and `\\bFROM` no longer matches since `f` and `F`
    are both word characters with no boundary between them — the same
    regression the items-side fstring pin catches, exercised here against
    `_COPIES_READ` instead of `_ITEMS_READ`.
    """
    _write_tree(tmp_path, {
        "app/routers/fsplit_copies.py": (
            'def f(db, sql):\n'
            '    copies = db.execute(\n'
            '        f"{sql} AS x "\n'
            '        f"FROM item_copies c "\n'
            '        "WHERE 1=1"\n'
            '    ).fetchall()\n'
        ),
    })
    violations = check_items_live.find_copy_violations(tmp_path)
    assert len(violations) == 1
    assert "fsplit_copies.py:4" in violations[0]


def test_ignores_delete_and_copies_live(tmp_path):
    """Pins the excluded shapes for the copies pattern: `DELETE FROM
    item_copies ...` (a write, stays on the physical table) and
    `FROM copies_live c` (the view itself — `\\b` does not split on `_`).

    Mutation -> pin: dropping the `(?<!DELETE )` lookbehind from
    `_COPIES_READ` reds the DELETE line; `copies_live` needs no mutation to
    prove wrong (it is pinned as a guard against a future rewrite that stops
    using `\\b`), same reasoning as the items-side ignore test.
    """
    _write_tree(tmp_path, {
        "app/routers/ignores_copies.py": (
            'def f(db):\n'
            '    db.execute("DELETE FROM item_copies WHERE id = ?")\n'
            '    db.execute("SELECT * FROM copies_live c")\n'
        ),
    })
    violations = check_items_live.find_copy_violations(tmp_path)
    assert violations == []


def test_copies_allowlist_suppresses_a_from_read_directly_before_its_own_statement(
    tmp_path,
):
    """Adjacency pin 1/4: a non-allowlisted `FROM item_copies` read on the
    line directly BEFORE the real COPIES_ALLOWLIST entry for
    app/database.py's copies_live view CREATE. The allowlist entry must not
    excuse its neighbour.

    Mutation -> pin: verified by hand — replacing `_spanning`'s match-extent
    check with a plain `sub in buf` test (the pre-fix ±200/300-char window
    equivalent) makes the allowlisted substring wrongly suppress the
    neighbour too, dropping violations from 1 to 0.
    """
    content = (
        'def f(db):\n'
        '    db.execute("SELECT id FROM item_copies WHERE id = ?")\n'
        '    conn.execute("SELECT c.* FROM item_copies c JOIN items i ON i.id = c.item_id")\n'
    )
    _write_tree(tmp_path, {"app/database.py": content})
    violations = check_items_live.find_copy_violations(tmp_path)
    assert len(violations) == 1, violations
    assert "database.py:2:" in violations[0]


def test_copies_allowlist_suppresses_a_from_read_directly_after_its_own_statement(
    tmp_path,
):
    """Adjacency pin 2/4: same shape, reversed order — the non-allowlisted
    `FROM item_copies` read comes directly AFTER the allowlisted statement.

    Mutation -> pin: verified by hand — the restored window check reds this
    one too (the pre-fix window was asymmetric, so both orders need pinning
    independently — see the items-side pin pair this mirrors).
    """
    content = (
        'def f(db):\n'
        '    conn.execute("SELECT c.* FROM item_copies c JOIN items i ON i.id = c.item_id")\n'
        '    db.execute("SELECT id FROM item_copies WHERE id = ?")\n'
    )
    _write_tree(tmp_path, {"app/database.py": content})
    violations = check_items_live.find_copy_violations(tmp_path)
    assert len(violations) == 1, violations
    assert "database.py:3:" in violations[0]


def test_copies_allowlist_suppresses_a_join_read_directly_before_its_own_statement(
    tmp_path,
):
    """Adjacency pin 3/4: the JOIN twin of pin 1/4 — a non-allowlisted
    `JOIN item_copies` read directly BEFORE the allowlisted statement.

    Mutation -> pin: same as pin 1/4 — restoring a proximity-window
    `_spanning` implementation reds this.
    """
    content = (
        'def f(db):\n'
        '    db.execute("SELECT * FROM checkouts x JOIN item_copies c ON c.id = x.copy_id")\n'
        '    conn.execute("SELECT c.* FROM item_copies c JOIN items i ON i.id = c.item_id")\n'
    )
    _write_tree(tmp_path, {"app/database.py": content})
    violations = check_items_live.find_copy_violations(tmp_path)
    assert len(violations) == 1, violations
    assert "database.py:2:" in violations[0]


def test_copies_allowlist_suppresses_a_join_read_directly_after_its_own_statement(
    tmp_path,
):
    """Adjacency pin 4/4: the JOIN twin of pin 2/4 — a non-allowlisted
    `JOIN item_copies` read directly AFTER the allowlisted statement.

    Mutation -> pin: same as pin 2/4 — restoring a proximity-window
    `_spanning` implementation reds this. This is the load-bearing
    requirement this task calls out: all four orders/keywords must be
    pinned independently, since a proximity window can pass one shape while
    failing another.
    """
    content = (
        'def f(db):\n'
        '    conn.execute("SELECT c.* FROM item_copies c JOIN items i ON i.id = c.item_id")\n'
        '    db.execute("SELECT * FROM checkouts x JOIN item_copies c ON c.id = x.copy_id")\n'
    )
    _write_tree(tmp_path, {"app/database.py": content})
    violations = check_items_live.find_copy_violations(tmp_path)
    assert len(violations) == 1, violations
    assert "database.py:3:" in violations[0]


def test_copies_allowlist_mismatches_reports_a_substring_the_code_no_longer_produces(
    tmp_path,
):
    """The item_copies twin of
    test_allowlist_mismatches_reports_a_substring_the_code_no_longer_produces:
    a real COPIES_ALLOWLIST substring that spans no hit in an empty tree is
    reported as stale.

    Mutation -> pin: verified by hand — replacing the `got != want` test
    with `False` (nothing is ever reported) reds this pin.
    """
    _write_tree(tmp_path, {"app/__init__.py": "\n"})
    mismatches = check_items_live.copy_allowlist_mismatches(tmp_path)
    assert any(
        "app/database.py" in s
        and "SELECT c.* FROM item_copies c JOIN items i ON i.id = c.item_id" in s
        for s in mismatches
    )


def test_copies_allowlist_mismatches_reports_a_read_riding_along_inside_an_entry(
    tmp_path, monkeypatch
):
    """The item_copies twin of
    test_allowlist_mismatches_reports_a_read_riding_along_inside_an_entry: an
    entry that spans MORE reads than it declares (a new direct read written
    inside text an existing entry already covers).

    Mutation -> pin: verified by hand — narrowing the check to the stale
    case only (`if got == 0`) leaves this pin red-free and it fails.
    """
    monkeypatch.setitem(
        check_items_live.COPIES_ALLOWLIST,
        "app/ride_copies.py",
        {"SELECT id FROM item_copies WHERE legacy = 1": 1},
    )
    _write_tree(tmp_path, {
        "app/ride_copies.py": (
            'def f(db):\n'
            '    db.execute("SELECT id FROM item_copies WHERE legacy = 1")\n'
            '    db.execute("SELECT id FROM item_copies WHERE legacy = 1")\n'
        ),
    })
    assert check_items_live.find_copy_violations(tmp_path) == []
    mismatches = check_items_live.copy_allowlist_mismatches(tmp_path)
    assert any(
        "app/ride_copies.py" in s and "spans 2 read(s), expected 1" in s
        for s in mismatches
    ), mismatches


def test_copies_allowlist_does_not_exempt_by_basename(tmp_path):
    """G88 pin: a fixture at app/routers/item_copies.py is NOT exempted by a
    COPIES_ALLOWLIST entry keyed on app/services/item_copies.py — the real
    allowlist has both paths as separate keys, and this proves neither
    excuses the other.

    The fixture statement here is the exact text of the real
    app/services/item_copies.py entry for sync_primary_location's next
    copy_number read (`SELECT COALESCE(MAX(copy_number), 0) + 1 AS n FROM
    item_copies WHERE item_id = ?`) — chosen deliberately so a basename-keyed
    bypass would have *something* to wrongly match against, rather than a
    fixture text that would go unreported regardless of the bug (which
    proves nothing).

    Mutation -> pin: verified by hand — changing the allowlist lookup in
    `_find_violations` from `allowlist.get(rel, {})` to `allowlist.get(path.name,
    {})` (basename instead of full repo-relative path) and re-keying
    COPIES_ALLOWLIST's app/services/item_copies.py entries onto the bare
    name `item_copies.py` makes this fixture's read at
    app/routers/item_copies.py wrongly inherit the services file's
    allowlist by basename, and the pin reds — this is exactly the issue
    #116 bypass G88 documents.
    """
    _write_tree(tmp_path, {
        "app/routers/item_copies.py": (
            'def f(db):\n'
            '    db.execute(\n'
            '        "SELECT COALESCE(MAX(copy_number), 0) + 1 AS n "\n'
            '        "FROM item_copies WHERE item_id = ?"\n'
            '    )\n'
        ),
    })
    violations = check_items_live.find_copy_violations(tmp_path)
    assert len(violations) == 1
    assert "app/routers/item_copies.py:4" in violations[0]


# --- T6: main() now fails the build on a copies violation, not just an
# allowlist mismatch — census-zero contract + main() exit-code pins ---


def test_every_copy_read_goes_through_copies_live():
    """The item_copies twin of test_every_read_goes_through_items_live — the
    census-zero contract, run against the real app/ tree (not a tmp_path
    fixture). T3-T5 of this plan repointed every non-exempt direct read of
    item_copies onto copies_live, driving the census to 0; this is the gate
    that keeps it there going forward — a new direct read of item_copies
    anywhere in app/ reds this test (and, from this task onward, main() /
    `make check-deleted`) rather than shipping unnoticed.
    """
    violations = check_items_live.find_copy_violations()
    assert not violations, "\n" + "\n".join(violations)


def test_copy_allowlist_entries_span_exactly_their_declared_read_count():
    """The item_copies twin of
    test_allowlist_entries_span_exactly_their_declared_read_count. Every
    entry in the real COPIES_ALLOWLIST must span exactly the number of reads
    declared beside it — 0 is the stale case (an entry the code no longer
    produces, silently over-permissive), and more than declared is the
    ride-along case (a new direct read written inside text an existing entry
    already covers). Both must fail the suite, not sit there unused.
    """
    mismatches = check_items_live.copy_allowlist_mismatches()
    assert not mismatches, "\n" + "\n".join(mismatches)


def test_main_reports_a_copy_violation_and_exits_non_zero(capsys, monkeypatch):
    """The item_copies twin of test_main_reports_a_violation_and_exits_non_zero.
    This is the pin that proves main() actually fails the build on a copies
    violation now, not just reports it as an informational census line (the
    pre-T6 behaviour) — flip the `copy_violations` term out of main()'s
    `if violations or mismatches or copy_violations or copy_mismatches:`
    condition, or drop the `find_copy_violations()` call entirely, and this
    reds while every other test in this file (including the census-zero
    contract above, which calls find_copy_violations() directly rather than
    through main()) stays green.
    """
    sentinel = "SENTINEL copies_live violation"
    monkeypatch.setattr(check_items_live, "find_violations", lambda *a, **k: [])
    monkeypatch.setattr(check_items_live, "allowlist_mismatches", lambda *a, **k: [])
    monkeypatch.setattr(
        check_items_live, "find_copy_violations", lambda *a, **k: [sentinel]
    )
    monkeypatch.setattr(
        check_items_live, "copy_allowlist_mismatches", lambda *a, **k: []
    )
    assert check_items_live.main() == 1
    assert sentinel in capsys.readouterr().out


def test_main_reports_a_copy_allowlist_mismatch_and_exits_non_zero(
    capsys, monkeypatch
):
    """The item_copies twin of
    test_main_reports_an_allowlist_mismatch_and_exits_non_zero — a clean
    copies violation census is not enough to exit 0 if a COPIES_ALLOWLIST
    entry no longer spans what it declares. Dropping the
    copy_allowlist_mismatches() call, or its term, from main()'s failure
    condition reds this while leaving the copy-violation pin above green.
    """
    sentinel = "SENTINEL copies allowlist mismatch"
    monkeypatch.setattr(check_items_live, "find_violations", lambda *a, **k: [])
    monkeypatch.setattr(check_items_live, "allowlist_mismatches", lambda *a, **k: [])
    monkeypatch.setattr(
        check_items_live, "find_copy_violations", lambda *a, **k: []
    )
    monkeypatch.setattr(
        check_items_live, "copy_allowlist_mismatches", lambda *a, **k: [sentinel]
    )
    assert check_items_live.main() == 1
    assert sentinel in capsys.readouterr().out
