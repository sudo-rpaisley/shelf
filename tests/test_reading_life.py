"""Reading-life persistence, explicit count semantics and lossless lifecycle."""

import json
import secrets
import sqlite3
import zipfile
from io import BytesIO

import pytest
from starlette.datastructures import FormData, UploadFile

from app.services import reading_life as life
from tests.conftest import _create_user, _insert_item


def _write(db, operation, *args):
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    result = operation(db, *args)
    db.commit()
    return result


def session_fields(**changes):
    return {"activity": "read", "status": "finished", "started_on": "2024-12-30",
            "ended_on": "2025-01-01", "submission_key": secrets.token_hex(16), **changes}


def journal_fields(**changes):
    return {"entry_on": "2025-01-01", "body": "private reading-life note",
            "session_id": "", "submission_key": secrets.token_hex(16), **changes}


def make_session(db, user_id, item_id, **changes):
    return _write(db, life.create_session, user_id, item_id, life.parse_session(session_fields(**changes)))


def make_journal(db, user_id, item_id, **changes):
    return _write(db, life.create_journal, user_id, item_id, life.parse_journal(journal_fields(**changes)))


def make_goal(db, user_id, **changes):
    values = {"year": "2025", "activity": "all", "count_rule": "sessions", "target": "10", **changes}
    return _write(db, life.create_goal, user_id, life.parse_goal(values))


def table_rows(db, table):
    assert table in {"reading_sessions", "reading_journal", "reading_goals"}
    return [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()]


@pytest.fixture
def records(db, viewer_user):
    item_id = _insert_item(db, isbn=None)
    db.commit()
    uid = viewer_user["id"]
    sid = make_session(db, uid, item_id)
    jid = make_journal(db, uid, item_id, session_id=str(sid))
    gid = make_goal(db, uid)
    return {"item_id": item_id, "session_id": sid, "entry_id": jid, "goal_id": gid}


def test_repeat_sessions_own_dates_and_revisions_and_do_not_count_edits_twice(db, viewer_user):
    uid = viewer_user["id"]
    item = _insert_item(db, isbn=None)
    first = make_session(db, uid, item)
    original = life.session_for(db, uid, first)
    second = make_session(db, uid, item, started_on="2025-05-01", ended_on="2025-05-10")
    assert first != second
    assert life.session_for(db, uid, first) == original
    correction = life.parse_session(session_fields(version="1", ended_on="2025-01-02"), update=True)
    _write(db, life.update_session, uid, first, correction)
    current = life.session_for(db, uid, first)
    assert current["version"] == 2 and current["ended_on"] == "2025-01-02"
    history = json.loads(current["history_json"])
    assert [row["ended_on"] for row in history] == ["2025-01-01", "2025-01-02"]
    assert [row["version"] for row in history] == [1, 2]
    _write(db, life.update_session, uid, first, {**correction, "version": 2})
    assert life.session_for(db, uid, first) == current  # true no-op, including timestamps
    make_goal(db, uid)
    assert life.goals_for(db, uid, 2025)[0]["completed"] == 2


def test_goal_rules_year_activity_accounts_and_distinct_catalogue_ids(db, viewer_user):
    uid = viewer_user["id"]
    other = _create_user("other", "password123", "Other", "viewer")
    book, edition, screen = [_insert_item(db, isbn=None, title=name, media_type=mt)
                             for name, mt in (("Book", "book"), ("Edition", "ebook"), ("Screen", "dvd"))]
    make_session(db, uid, book)  # started in previous year, counts in finish year
    make_session(db, uid, book, started_on="2025-02-01", ended_on="2025-02-02")
    make_session(db, uid, book, activity="listen")  # all-distinct counts this item once overall
    make_session(db, uid, edition)
    make_session(db, uid, screen, activity="watch")
    for status in ("in_progress", "paused", "voided", "abandoned"):
        make_session(db, uid, book, status=status, ended_on="2025-01-01" if status == "abandoned" else "")
    make_session(db, uid, book, started_on="2024-01-01", ended_on="2024-12-31")
    make_session(db, other["id"], edition, activity="play")
    for activity in ("all", "read", "listen", "watch", "play"):
        for rule in ("sessions", "distinct_items"):
            make_goal(db, uid, activity=activity, count_rule=rule)
    goals = {(g["activity"], g["count_rule"]): g["completed"] for g in life.goals_for(db, uid, 2025)}
    assert goals == {("all", "sessions"): 5, ("all", "distinct_items"): 3,
                     ("read", "sessions"): 3, ("read", "distinct_items"): 2,
                     ("listen", "sessions"): 1, ("listen", "distinct_items"): 1,
                     ("watch", "sessions"): 1, ("watch", "distinct_items"): 1,
                     ("play", "sessions"): 0, ("play", "distinct_items"): 0}
    assert life.goals_for(db, other["id"], 2025) == []
    for year in ("1", "9999"):
        make_goal(db, uid, year=year)
        assert life.goals_for(db, uid, int(year))[0]["completed"] == 0


def test_reopen_void_or_correct_end_year_recalculates_without_erasing_prior_cycle(db, viewer_user, records):
    uid, sid = viewer_user["id"], records["session_id"]
    updates = [("paused", "", 0), ("finished", "2025-03-01", 1),
               ("finished", "2024-12-31", 0), ("voided", "", 0)]
    for version, (status, ended, expected) in enumerate(updates, 1):
        values = life.parse_session(session_fields(status=status, ended_on=ended, version=str(version)), update=True)
        _write(db, life.update_session, uid, sid, values)
        assert life.goals_for(db, uid, 2025)[0]["completed"] == expected
    row = life.session_for(db, uid, sid)
    assert row["version"] == 5
    assert len(json.loads(row["history_json"])) == 5
    assert db.execute("SELECT COUNT(*) FROM reading_sessions").fetchone()[0] == 1


def test_journal_preserves_text_dates_and_session_association(db, viewer_user, records):
    uid, jid = viewer_user["id"], records["entry_id"]
    before = life.journal_for(db, uid, jid)
    values = life.parse_journal(journal_fields(body="  corrected\nentry  ", entry_on="2025-02-01", version="1"), update=True)
    _write(db, life.update_journal, uid, jid, values)
    after = life.journal_for(db, uid, jid)
    assert after["session_id"] == before["session_id"]
    assert after["body"] == "  corrected\nentry  "
    history = json.loads(after["history_json"])
    assert [(r["entry_on"], r["body"]) for r in history] == [
        ("2025-01-01", before["body"]), ("2025-02-01", "  corrected\nentry  ")]
    _write(db, life.update_journal, uid, jid, {**values, "version": 2})
    assert life.journal_for(db, uid, jid) == after
    item_entry = make_journal(db, uid, records["item_id"], body="item only")
    assert life.journal_for(db, uid, item_entry)["session_id"] is None
    assert len(life.journal_page(db, uid, session_id=records["session_id"])["rows"]) == 1


@pytest.mark.parametrize("parser,base,field,values", [
    (life.parse_session, session_fields, "activity", ["book", "all", "READ", ""]),
    (life.parse_session, session_fields, "status", ["reading", "planned", "", "done"]),
    (life.parse_session, session_fields, "started_on", ["2025-02-29", "2025-1-01", "20250101", "0000-01-01", "", "9999-01-01"]),
    (life.parse_session, session_fields, "ended_on", ["2025-02-30", "2024-12-29", "", "2025-01-01T00:00:00", "2025-W01-1"]),
    (life.parse_journal, journal_fields, "body", ["", " \n ", "a" * 10001, "a\x00b"]),
    (life.parse_journal, journal_fields, "session_id", ["0", "-1", str(2**63), "9" * 5000, "1.0", "١"]),
    (life.parse_goal, lambda: {"year": "2025", "activity": "all", "count_rule": "sessions", "target": "10"},
     "target", ["", "0", "-1", "10001", "1.5", "NaN", "inf", "+1", " 1 ", "١", "1e2"]),
])
def test_strict_validation(parser, base, field, values):
    for value in values:
        with pytest.raises(ValueError):
            parser({**base(), field: value})


def test_duplicate_fields_and_files_are_rejected_not_silently_coerced():
    cases = [(life.parse_session, session_fields()), (life.parse_journal, journal_fields()),
             (life.parse_goal, {"year": "2025", "activity": "all", "count_rule": "sessions", "target": "1"})]
    for parser, fields in cases:
        for name in fields:
            with pytest.raises(ValueError, match="only once"):
                parser(FormData([*fields.items(), (name, fields[name])]))
            with pytest.raises(ValueError, match="must be text"):
                parser(FormData([(key, UploadFile(BytesIO(b"file"), filename="upload") if key == name else value)
                                 for key, value in fields.items()]))
    assert life.parse_journal(journal_fields(body="n" * 10000))["body"] == "n" * 10000
    assert life.parse_session(session_fields(started_on="2024-02-29"))["started_on"] == "2024-02-29"


def test_sessions_and_journal_submit_keys_are_per_user_and_idempotent_after_corrections(db, viewer_user, records):
    uid, item = viewer_user["id"], records["item_id"]
    other = _create_user("other", "password123", "Other", "viewer")
    fields = session_fields()
    values = life.parse_session(fields)
    sid = _write(db, life.create_session, uid, item, values)
    assert _write(db, life.create_session, uid, item, values) == sid
    _write(db, life.update_session, uid, sid, life.parse_session({**fields, "status": "paused", "ended_on": "", "version": "1"}, update=True))
    assert _write(db, life.create_session, uid, item, values) == sid
    assert make_session(db, other["id"], item, **fields) != sid
    with pytest.raises(life.Conflict):
        _write(db, life.create_session, uid, item, {**values, "activity": "listen"})
    db.rollback()
    note = life.parse_journal(journal_fields())
    jid = _write(db, life.create_journal, uid, item, note)
    _write(db, life.update_journal, uid, jid, {"body": "corrected", "entry_on": "2025-01-01", "version": 1})
    assert _write(db, life.create_journal, uid, item, note) == jid


def test_lists_and_revision_history_are_paginated_with_deterministic_ties(db, viewer_user, records):
    uid, item = viewer_user["id"], records["item_id"]
    session_ids, journal_ids = [records["session_id"]], [records["entry_id"]]
    for number in range(life.PAGE_SIZE + 2):
        session_ids.append(make_session(db, uid, item))
        journal_ids.append(make_journal(db, uid, item, body=f"entry {number}"))
    for reader, ids in ((life.sessions_page, session_ids), (life.journal_page, journal_ids)):
        first = reader(db, uid, item_id=item)
        assert first["has_next"] and len(first["rows"]) == life.PAGE_SIZE
        assert "history_json" not in first["rows"][0]
        second = reader(db, uid, item_id=item, page=2)
        assert not second["has_next"]
        assert [r["id"] for r in first["rows"] + second["rows"]] == ids[::-1]
        assert reader(db, uid, page=100000)["rows"] == []
    sid = records["session_id"]
    for version in range(1, life.PAGE_SIZE + 3):
        _write(db, life.update_session, uid, sid, {
            "activity": "read", "status": "paused" if version % 2 else "in_progress",
            "started_on": "2024-12-30", "ended_on": None, "version": version,
        })
    row = life.session_for(db, uid, sid)
    first, second = life.history_page(row), life.history_page(row, 2)
    assert first["has_next"] and len(first["rows"]) == 20
    assert [r["version"] for r in first["rows"] + second["rows"]] == list(range(23, 0, -1))


def test_corrupt_history_is_not_overwritten_on_correction(db, viewer_user, records):
    uid = viewer_user["id"]
    for table, key, reader, updater, values in (
        ("reading_sessions", "session_id", life.session_for, life.update_session,
         {"activity": "play", "status": "paused", "started_on": "2024-12-30", "ended_on": None, "version": 1}),
        ("reading_journal", "entry_id", life.journal_for, life.update_journal,
         {"body": "new", "entry_on": "2025-01-01", "version": 1}),
    ):
        db.execute(f"UPDATE {table} SET history_json = 'invalid' WHERE id = ?", (records[key],))
        db.commit()
        row = reader(db, uid, records[key])
        assert life.history_page(row)["error"]
        with pytest.raises(life.Conflict, match="No changes"):
            _write(db, updater, uid, records[key], values)
        db.rollback()
        assert reader(db, uid, records[key]) == row


def test_item_merge_moves_all_cycles_and_journal_preserving_ids_revisions_and_goals(admin_client, db, viewer_user, records):
    uid, keep = viewer_user["id"], records["item_id"]
    other = _insert_item(db, title="Merged away", isbn=None)
    sid = make_session(db, uid, other)
    make_journal(db, uid, other, session_id=str(sid), body="linked to losing cycle")
    make_journal(db, uid, other, body="item-level losing note")
    other_user = _create_user("other", "password123", "Other", "viewer")
    make_session(db, other_user["id"], other)
    make_goal(db, uid, count_rule="distinct_items")
    before = {t: table_rows(db, t) for t in ("reading_sessions", "reading_journal", "reading_goals")}
    assert {g["count_rule"]: g["completed"] for g in life.goals_for(db, uid, 2025)} == {"sessions": 2, "distinct_items": 2}
    result = admin_client.post("/api/items/merge", json={"keep_id": keep, "merge_ids": [other]})
    assert result.status_code == 200 and result.json() == {"ok": True, "merged": 1}
    for table in ("reading_sessions", "reading_journal"):
        assert table_rows(db, table) == [{**r, "item_id": keep} for r in before[table]]
    assert table_rows(db, "reading_goals") == before["reading_goals"]
    assert {g["count_rule"]: g["completed"] for g in life.goals_for(db, uid, 2025)} == {"sessions": 2, "distinct_items": 1}
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_foreign_keys_enforce_session_owner_and_item_and_cascade_purge_or_user_delete(db, viewer_user, records):
    uid = viewer_user["id"]
    other = _create_user("other", "password123", "Other", "viewer")
    other_item = _insert_item(db, isbn=None)
    other_sid = make_session(db, other["id"], other_item)
    other_jid = make_journal(db, other["id"], other_item, session_id=str(other_sid))
    make_goal(db, other["id"])
    for user_id, item_id in ((uid, other_item), (other["id"], records["item_id"])):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE reading_journal SET user_id = ?, item_id = ? WHERE id = ?", (user_id, item_id, records["entry_id"]))
        db.rollback()
    db.execute("DELETE FROM items WHERE id = ?", (records["item_id"],))
    db.commit()
    assert table_rows(db, "reading_sessions")[0]["id"] == other_sid
    assert table_rows(db, "reading_journal")[0]["id"] == other_jid
    assert len(table_rows(db, "reading_goals")) == 2  # goals do not depend on an item
    db.execute("DELETE FROM users WHERE id = ?", (other["id"],))
    db.commit()
    assert table_rows(db, "reading_sessions") == table_rows(db, "reading_journal") == []
    assert [g["user_id"] for g in table_rows(db, "reading_goals")] == [uid]


def test_upgrade_adds_empty_tables_even_feature_off_and_reboot_and_backup_retain_rows(db, viewer_user, records, tmp_path):
    from app.database import init_db, set_setting

    uid = viewer_user["id"]
    expected_schema = [tuple(r) for r in db.execute(
        "SELECT name, sql FROM sqlite_master WHERE name LIKE '%reading_session%' "
        "OR name LIKE '%reading_journal%' OR name LIKE '%reading_goals%' ORDER BY name")]
    for table in ("reading_journal", "reading_sessions", "reading_goals"):
        db.execute(f"DROP TABLE {table}")
    set_setting(db, "feature.connected_library", "0")
    db.execute("INSERT INTO reading_log (item_id, status, notes) VALUES (?, 'read', 'legacy shared')", (records["item_id"],))
    db.commit()
    assert db.execute("SELECT name FROM sqlite_master WHERE name = 'reading_sessions'").fetchone() is None
    init_db()
    assert [tuple(r) for r in db.execute(
        "SELECT name, sql FROM sqlite_master WHERE name LIKE '%reading_session%' "
        "OR name LIKE '%reading_journal%' OR name LIKE '%reading_goals%' ORDER BY name")] == expected_schema
    assert table_rows(db, "reading_sessions") == table_rows(db, "reading_journal") == table_rows(db, "reading_goals") == []
    sid = make_session(db, uid, records["item_id"])
    make_journal(db, uid, records["item_id"], session_id=str(sid))
    make_goal(db, uid)
    before = {t: table_rows(db, t) for t in ("reading_sessions", "reading_journal", "reading_goals")}
    init_db()
    backup = tmp_path / "reading-life-backup.db"
    db.execute("VACUUM INTO ?", (str(backup),))
    with sqlite3.connect(backup) as copied:
        copied.row_factory = sqlite3.Row
        for table in before:
            assert table_rows(copied, table) == table_rows(db, table) == before[table]
        assert copied.execute("PRAGMA foreign_key_check").fetchall() == []
        assert copied.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'view'").fetchone()[0] == 0
    assert db.execute("SELECT notes FROM reading_log WHERE item_id = ?", (records["item_id"],)).fetchone()[0] == "legacy shared"


def test_legacy_shared_archive_does_not_export_private_reading_life(db, records):
    from app.services import archive

    with zipfile.ZipFile(archive.build_archive(db)) as exported:
        payload = exported.read("library.json").decode()
        assert "private reading-life note" not in payload
        library = json.loads(payload)
        assert not {"reading_sessions", "reading_journal", "reading_goals"} & library.keys()


def test_services_participate_in_callers_transaction_without_committing(db, viewer_user, records):
    uid, item = viewer_user["id"], records["item_id"]
    before = {t: table_rows(db, t) for t in ("reading_sessions", "reading_journal", "reading_goals")}
    db.execute("BEGIN IMMEDIATE")
    sid = life.create_session(db, uid, item, life.parse_session(session_fields()))
    life.create_journal(db, uid, item, life.parse_journal(journal_fields(session_id=str(sid))))
    life.create_goal(db, uid, life.parse_goal({"year": "2024", "activity": "read", "count_rule": "sessions", "target": "1"}))
    db.rollback()
    for table in before:
        assert table_rows(db, table) == before[table]
