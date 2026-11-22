"""Pure source-guard regressions; runnable directly while app writers are active.

Only the stdlib guard module is imported. Scratch app/ files are SQL SOURCE
fixtures, never executed application/DB doubles. These explicit reviewed spans
pin policy, not application behavior; HTTP/security verification is separate.
Run: python tests/test_integrated_scoped_sql_guard.py
"""

import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "integrated_scoped_sql_guard", ROOT / "scripts/check_items_live.py"
)
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)

# Literal source placeholders matter: replacing these with expanded SQL would
# stop pinning what the normalizer actually sees in the application's f-strings.
BARCODE_COPY = (
    "SELECT {projection}, l.name AS location_name, c.id AS copy_id, "
    "c.copy_number, c.location_id AS copy_location_id, cl.name AS copy_location_name, "
    "COALESCE(i.deleted_at, c.deleted_at) AS trash_stamp, "
    "(i.deleted_at IS NULL AND c.deleted_at IS NOT NULL) AS copy_trashed "
    "FROM item_copies c JOIN items i ON i.id = c.item_id "
    "LEFT JOIN locations l ON i.location_id = l.id "
    "LEFT JOIN location_nodes cl ON c.location_id = cl.id "
    "{state_join} WHERE c.copy_barcode = ? AND ({scope}) "
    "ORDER BY i.deleted_at IS NOT NULL, c.deleted_at IS NOT NULL, c.id"
)
TRASH_COPY = (
    "SELECT c.*, i.title AS item_title, (i.deleted_at IS NULL) AS item_live, "
    "(i.deleted_at IS NULL AND ({copy_cond})) AS expired, l.name AS location_name "
    "FROM item_copies c JOIN items i ON i.id = c.item_id "
    "LEFT JOIN location_nodes l ON l.id = c.location_id "
    "WHERE c.deleted_at IS NOT NULL AND ({scope}) ORDER BY i.id, c.copy_number, c.id"
)
ARCHIVE_COPY = (
    "SELECT c.*, n.name AS location FROM item_copies c "
    "JOIN items i ON i.id = c.item_id LEFT JOIN location_nodes n ON n.id = c.location_id "
    "WHERE ({condition}) ORDER BY c.item_id, c.copy_number, c.id"
)
CSV_EXPORT = (
    "SELECT {projection}, l.name as location_name, {lists.WISHLISTED_SQL} AS wishlisted "
    "FROM items i {private_join} LEFT JOIN locations l ON i.location_id = l.id "
    "WHERE ({condition}) AND (i.deleted_at IS NULL OR ({trash_access})) ORDER BY i.title"
)
HIDDEN_SLOT = "SELECT id FROM items WHERE {field} = ? AND media_type = ? AND id != ?"
AUDIT_PARENT = "SELECT item_id FROM item_copies WHERE id = ?"
ALIAS_PARENTS = (
    "WITH RECURSIVE nodes(id) AS ( "
    "SELECT id FROM location_nodes WHERE legacy_location_id = ? UNION "
    "SELECT n.id FROM location_nodes n JOIN nodes p ON n.parent_id = p.id "
    ") SELECT id FROM items WHERE location_id = ? UNION "
    "SELECT c.item_id AS id FROM item_copies c JOIN nodes n ON n.id = c.location_id ORDER BY id"
)
NODE_BACKFILL = (
    "INSERT INTO item_copies (item_id, copy_number, location_id, is_primary) "
    "SELECT i.id, 1, n.id, 1 FROM items i "
    "LEFT JOIN location_nodes n ON n.legacy_location_id = i.location_id "
    "WHERE i.owned = 1 AND i.media_type IN "
    "('book','kids_book','vinyl','cassette','cd','music_other', "
    "'dvd','video_game','comic','manga','magazine') "
    "AND NOT EXISTS (SELECT 1 FROM item_copies c WHERE c.item_id = i.id) "
    "AND NOT EXISTS (SELECT 1 FROM settings "
    "WHERE key = 'holdings_foundation_backfill_v1' AND value = '1')"
)
RETENTION = (
    "SELECT (SELECT COUNT(*) FROM items i WHERE {item_sql} AND ({scope})) + "
    "(SELECT COUNT(*) FROM item_copies c JOIN items i "
    "ON i.id = c.item_id AND i.deleted_at IS NULL "
    "WHERE {copy_sql} AND ({scope})) AS n"
)
HARDCOVER_SYNC = (
    "SELECT i.id, i.hardcover_book_id, i.media_type, li.library_id, s.reading_status "
    "FROM items i JOIN library_items li ON li.item_id = i.id "
    "LEFT JOIN user_item_state s ON s.item_id = i.id AND s.user_id = ? "
    "WHERE i.deleted_at IS NULL AND i.hardcover_book_id IS NOT NULL "
    "AND i.media_type IN ({','.join('?' * len(family))}) AND ({condition}) "
    "{selection} ORDER BY i.id"
)
SCAN_HISTORY = (
    "((EXISTS (SELECT 1 FROM libraries history_library "
    "JOIN library_memberships history_member ON history_member.library_id = history_library.id "
    "WHERE history_library.id = {scan_alias}.security_library_id "
    "AND history_member.user_id = ? AND history_member.role IN ('viewer', 'editor'))) "
    "OR ({scan_alias}.security_library_id IS NULL AND {scan_alias}.security_user_id = ?)) "
    "AND ({scan_alias}.item_id IS NULL OR EXISTS (SELECT 1 FROM items physical "
    "WHERE physical.id = {scan_alias}.item_id AND ({current_scope})))"
)

# Hard-coded expectations, not generated from the allowlist or a violation
# census. A broad prefix replacing any entry fails this exact-key assertion.
# Counts describe source reads, not the number of runtime field iterations.
REVIEWED = (
    ("app/services/barcode_resolve.py", BARCODE_COPY, 1, 1),
    ("app/services/trash.py", TRASH_COPY, 1, 1),
    ("app/services/archive.py", ARCHIVE_COPY, 1, 1),
    ("app/routers/items_csv.py", CSV_EXPORT, 1, 0),
    ("app/services/item_write.py", HIDDEN_SLOT, 1, 0),
    ("app/services/audits.py", AUDIT_PARENT, 0, 1),
    ("app/services/locations.py", ALIAS_PARENTS, 1, 1),
    ("app/database.py", NODE_BACKFILL, 1, 1),
    ("app/services/trash.py", RETENTION, 2, 1),
    ("app/services/hardcover.py", HARDCOVER_SYNC, 1, 0),
    ("app/services/scan_history.py", SCAN_HISTORY, 1, 0),
    ("app/services/catalogue_security.py", "SELECT * FROM items WHERE id = ?", 1, 0),
    ("app/services/libraries.py", "SELECT 1 FROM items WHERE id = ?", 1, 0),
    ("app/services/user_state.py", "SELECT id FROM items WHERE id = ?", 1, 0),
)


def _source(sql):
    # Deliberate double-quoted f-string source, not a runtime SQL expansion.
    return f'db.execute(\n    f"{sql}"\n)\n'


class IntegratedScopedSqlGuard(unittest.TestCase):
    def setUp(self):
        # Keep standalone checks inside this worktree; every fixture is removed.
        self.scratch = tempfile.TemporaryDirectory(prefix=".scoped-sql-", dir=ROOT)
        self.addCleanup(self.scratch.cleanup)
        self.serial = 0

    def tree(self, rel, source):
        self.serial += 1
        root = Path(self.scratch.name) / str(self.serial)
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
        return root

    def selected_mismatches(self, root, rel, sql, items, copies):
        # Isolate only the hard-coded entries under test: other real entries
        # are naturally absent from a scratch tree, not stale production code.
        result = []
        for pattern, count in ((guard._ITEMS_READ, items), (guard._COPIES_READ, copies)):
            if count:
                result.extend(guard._allowlist_mismatches(root, pattern, {rel: {sql: count}}))
        return result

    def assert_main_fails(self, root, mismatches=(), *, copy_mismatches=()):
        with (
            patch.object(guard, "find_violations", return_value=guard.find_violations(root)),
            patch.object(guard, "find_copy_violations", return_value=guard.find_copy_violations(root)),
            patch.object(guard, "allowlist_mismatches", return_value=list(mismatches)),
            patch.object(guard, "copy_allowlist_mismatches", return_value=list(copy_mismatches)),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(guard.main(), 1)
        self.assertNotIn("every read goes through the view", output.getvalue())

    def test_reviewed_full_spans_have_exact_source_counts(self):
        for rel, sql, items, copies in REVIEWED:
            with self.subTest(path=rel, sql=sql):
                if items:
                    self.assertEqual(guard.ALLOWLIST[rel][sql], items)
                if copies:
                    self.assertEqual(guard.COPIES_ALLOWLIST[rel][sql], copies)
                root = self.tree(rel, _source(sql))
                self.assertEqual(guard.find_violations(root), [])
                self.assertEqual(guard.find_copy_violations(root), [])
                self.assertEqual(self.selected_mismatches(root, rel, sql, items, copies), [])

    def test_new_exception_does_not_excuse_adjacent_reads_in_either_order(self):
        for rel, sql, items, copies in REVIEWED:
            unsafe = ('db.execute("SELECT * FROM items WHERE id = ?")\n' if items else "")
            unsafe += ('db.execute("SELECT * FROM item_copies WHERE id = ?")\n' if copies else "")
            # The security identity primitive legitimately uses exactly this
            # items SQL. Use a different SELECT there, not a disguised duplicate.
            if rel == "app/services/catalogue_security.py":
                unsafe = 'db.execute("SELECT title FROM items WHERE id = ?")\n'
            for before in (True, False):
                with self.subTest(path=rel, before=before):
                    source = unsafe + _source(sql) if before else _source(sql) + unsafe
                    root = self.tree(rel, source)
                    self.assertEqual(len(guard.find_violations(root)), int(bool(items)))
                    self.assertEqual(len(guard.find_copy_violations(root)), int(bool(copies)))
                    self.assert_main_fails(root)

    def test_exact_sql_in_another_path_is_still_forbidden(self):
        for rel, sql, items, copies in REVIEWED:
            with self.subTest(path=rel):
                # Same basename, different repository-relative path.
                other = "app/future/" + Path(rel).name
                root = self.tree(other, _source(sql))
                self.assertEqual(len(guard.find_violations(root)), items)
                self.assertEqual(len(guard.find_copy_violations(root)), copies)
                self.assert_main_fails(root)

    def test_wrong_flat_copy_location_joins_are_not_blessed(self):
        for rel, sql, alias in (
            ("app/services/barcode_resolve.py", BARCODE_COPY, "cl"),
            ("app/services/trash.py", TRASH_COPY, "l"),
            ("app/services/archive.py", ARCHIVE_COPY, "n"),
        ):
            with self.subTest(path=rel):
                wrong = sql.replace(f"LEFT JOIN location_nodes {alias}", f"LEFT JOIN locations {alias}")
                self.assertNotEqual(wrong, sql)
                root = self.tree(rel, _source(wrong))
                self.assertEqual(len(guard.find_violations(root)), 1)
                self.assertEqual(len(guard.find_copy_violations(root)), 1)
                self.assert_main_fails(root)

    def test_node_backfill_cannot_revert_to_coincident_flat_ids(self):
        wrong = NODE_BACKFILL.replace("SELECT i.id, 1, n.id, 1", "SELECT i.id, 1, i.location_id, 1")
        root = self.tree("app/database.py", _source(wrong))
        self.assertEqual(len(guard.find_violations(root)), 1)
        self.assertEqual(len(guard.find_copy_violations(root)), 1)
        self.assert_main_fails(root)

    def test_scope_private_projection_and_snapshot_clauses_are_load_bearing(self):
        mutations = (
            ("app/services/barcode_resolve.py", BARCODE_COPY, "AND ({scope})", "AND (1 = 1)", 1, 1),
            ("app/services/barcode_resolve.py", BARCODE_COPY, "{projection}", "i.*", 1, 1),
            ("app/services/barcode_resolve.py", BARCODE_COPY, "{state_join}", "", 1, 1),
            ("app/services/trash.py", TRASH_COPY, "AND ({scope})", "", 1, 1),
            ("app/services/archive.py", ARCHIVE_COPY, "WHERE ({condition})", "WHERE 1 = 1", 1, 1),
            ("app/routers/items_csv.py", CSV_EXPORT, "{private_join}", "", 1, 0),
            ("app/routers/items_csv.py", CSV_EXPORT, "{projection}", "i.*", 1, 0),
            ("app/routers/items_csv.py", CSV_EXPORT, "({trash_access})", "1 = 1", 1, 0),
            ("app/services/hardcover.py", HARDCOVER_SYNC, "AND ({condition})", "", 1, 0),
            ("app/services/hardcover.py", HARDCOVER_SYNC, "s.user_id = ?", "1 = 1", 1, 0),
            ("app/services/scan_history.py", SCAN_HISTORY, "({current_scope})", "1 = 1", 1, 0),
        )
        for rel, sql, old, new, items, copies in mutations:
            with self.subTest(path=rel, removed=old):
                mutated = sql.replace(old, new)
                self.assertNotEqual(mutated, sql)
                root = self.tree(rel, _source(mutated))
                self.assertEqual(len(guard.find_violations(root)), items)
                self.assertEqual(len(guard.find_copy_violations(root)), copies)
                self.assert_main_fails(root)

    def test_future_third_item_read_beside_two_read_retention_span_fails(self):
        root = self.tree(
            "app/services/trash.py",
            _source(RETENTION) + 'db.execute("SELECT id FROM items WHERE id = ?")\n',
        )
        self.assertEqual(len(guard.find_violations(root)), 1)
        self.assertEqual(self.selected_mismatches(root, "app/services/trash.py", RETENTION, 2, 1), [])
        self.assert_main_fails(root)

    def test_duplicate_two_read_statement_is_a_count_mismatch_not_a_budget(self):
        rel = "app/services/trash.py"
        root = self.tree(rel, _source(RETENTION) * 2)
        self.assertEqual(guard.find_violations(root), [])
        self.assertEqual(guard.find_copy_violations(root), [])
        mismatches = self.selected_mismatches(root, rel, RETENTION, 2, 1)
        self.assertEqual(len(mismatches), 2)
        self.assertTrue(any("spans 4 read(s), expected 2" in message for message in mismatches))
        self.assertTrue(any("spans 2 read(s), expected 1" in message for message in mismatches))
        self.assert_main_fails(root, mismatches[:1], copy_mismatches=mismatches[1:])

    def test_second_and_third_identical_identity_reads_fail_declared_counts(self):
        for rel, sql, items, copies in (
            ("app/services/item_write.py", HIDDEN_SLOT, 1, 0),
            ("app/services/audits.py", AUDIT_PARENT, 0, 1),
        ):
            for repeat in (2, 3):
                with self.subTest(path=rel, repeat=repeat):
                    root = self.tree(rel, _source(sql) * repeat)
                    # _spanning correctly recognizes both exact occurrences;
                    # the count gate is what refuses an unreviewed extra read.
                    self.assertEqual(guard.find_violations(root), [])
                    self.assertEqual(guard.find_copy_violations(root), [])
                    mismatches = self.selected_mismatches(root, rel, sql, items, copies)
                    self.assertEqual(len(mismatches), 1)
                    self.assertIn(f"spans {repeat} read(s), expected 1", mismatches[0])
                    self.assert_main_fails(
                        root, mismatches if items else (),
                        copy_mismatches=mismatches if copies else (),
                    )

    def test_database_admission_all_rows_span_does_not_hide_where_reads(self):
        rel = "app/database.py"
        root = self.tree(
            rel, 'db.execute("SELECT * FROM items")\ndb.execute("SELECT * FROM items WHERE id = ?")\n'
        )
        self.assertEqual(guard.ALLOWLIST[rel]["db.execute(SELECT * FROM items)"], 1)
        violations = guard.find_violations(root)
        self.assertEqual(len(violations), 1)
        self.assertIn("app/database.py:2:", violations[0])
        self.assert_main_fails(root)

    def test_new_spans_go_stale_if_the_reviewed_statement_disappears(self):
        for rel, sql, items, copies in REVIEWED:
            with self.subTest(path=rel):
                root = self.tree(rel, "# No physical query remains.\n")
                mismatches = self.selected_mismatches(root, rel, sql, items, copies)
                self.assertEqual(len(mismatches), int(bool(items)) + int(bool(copies)))
                self.assertTrue(all("spans 0 read(s)" in message for message in mismatches))
                self.assert_main_fails(
                    root, mismatches[:int(bool(items))],
                    copy_mismatches=mismatches[int(bool(items)):],
                )

    def test_removed_retirement_and_deleted_module_exceptions_do_not_return(self):
        source = (
            'db.execute("SELECT id FROM items WHERE media_type = \'kids_book\'")\n'
            'db.execute("SELECT id FROM items WHERE media_type = \'book\' AND isbn = ?")\n'
        )
        root = self.tree("app/database.py", source)
        self.assertEqual(len(guard.find_violations(root)), 2)
        for rel in (
            "app/routers/library_item_guard.py", "app/routers/library_item_access.py",
            "app/routers/upstream_034_compat.py", "app/routers/user_state.py",
        ):
            with self.subTest(path=rel):
                self.assertNotIn(rel, guard.ALLOWLIST)
                self.assertNotIn(rel, guard.COPIES_ALLOWLIST)
                root = self.tree(rel, _source(BARCODE_COPY))
                self.assertEqual(len(guard.find_violations(root)), 1)
                self.assertEqual(len(guard.find_copy_violations(root)), 1)


if __name__ == "__main__":
    unittest.main()
