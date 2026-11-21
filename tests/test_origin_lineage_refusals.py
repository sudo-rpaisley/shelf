"""D1 negative controls: refuse non-fork or ambiguous histories before writes."""

import pytest

from app.database import SchemaLineageError, _run_migrations, inspect_schema_lineage
from tests.lineage import captured_fork_database, captured_upstream_database


@pytest.mark.parametrize("history, message", [
    ("upstream", "upstream or mixed migration history"),
    ("mixed-effect", "Unsupported or mixed columns"),
    ("unknown-version", "Unknown, upstream or mixed migration history"),
    ("missing-private-seed", "Ambiguous preexisting personal domain without complete seed history"),
])
def test_refusal_preserves_exact_schema_history_and_data(tmp_path, history, message):
    factory = captured_upstream_database if history == "upstream" else captured_fork_database
    conn = factory(tmp_path / "unadmitted.db")
    try:
        if history == "mixed-effect":
            conn.execute("ALTER TABLE items ADD COLUMN deleted_at TEXT DEFAULT NULL")
        elif history == "unknown-version":
            conn.execute("INSERT INTO schema_version (version, description) VALUES (999999, 'Unknown')")
        elif history == "missing-private-seed":
            conn.execute("INSERT INTO users (id, username, password, role) VALUES (1, 'private', 'x', 'viewer')")
            conn.execute("INSERT INTO items (id, title) VALUES (1, 'Private history control')")
            conn.execute("INSERT INTO user_item_state (user_id, item_id, wishlist) VALUES (1, 1, 1)")
            conn.execute("DELETE FROM schema_version WHERE version = 55")
        conn.commit()
        before = "\n".join(conn.iterdump())
        with pytest.raises(SchemaLineageError, match=message):
            _run_migrations(conn)
        assert "\n".join(conn.iterdump()) == before
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'schema_lineage'").fetchone() is None
    finally:
        conn.close()


def test_captured_fork_history_is_positive_evidence_and_never_reseeded(tmp_path):
    conn = captured_fork_database(tmp_path / "fork.db")
    try:
        assert inspect_schema_lineage(conn) == "fork"
        before = [tuple(r) for r in conn.execute("SELECT * FROM schema_version ORDER BY version")]
        _run_migrations(conn)
        assert inspect_schema_lineage(conn) == "integrated"
        assert [tuple(r) for r in conn.execute("SELECT * FROM schema_version WHERE version <= 66 ORDER BY version")] == before
        assert conn.execute("SELECT 1 FROM schema_version WHERE version = 47").fetchone() is None
        conn.execute("INSERT INTO users (username, password, role) VALUES ('later', 'x', 'viewer')")
        conn.execute("INSERT INTO items (title, owned, reading_status) VALUES ('Later neither', 0, 'read')")
        conn.commit()
        assert _run_migrations(conn) == []
        assert conn.execute("SELECT COUNT(*) FROM user_item_state").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM user_reading_log").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM library_memberships").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM list_items").fetchone()[0] == 0
    finally:
        conn.close()
