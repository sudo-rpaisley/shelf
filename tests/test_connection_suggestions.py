"""Conservative evidence, stale-review safety and reversible connection forms."""

from contextlib import contextmanager
import logging
import sqlite3
from unittest.mock import Mock

import pytest

from app.services import connection_suggestions as connections
from app.services import item_write, media_groups


def _item(db, title="Dune", media_type="book", authors="Frank Herbert", **fields):
    return item_write.insert_item(db, title=title, media_type=media_type, authors=authors, **fields)


def _pair(db, **fields):
    a = _item(db, **fields)
    b = _item(db, media_type="ebook", **fields)
    return a, b, connections.candidate_for_pair(db, a, b)


def _form(proposal):
    return {key: proposal[key] for key in ("item_a_id", "item_b_id", "evidence_key")}


def _post(client, action, proposal, **kwargs):
    return client.post(f"/api/library/connections/{action}", data=_form(proposal),
                       follow_redirects=False, **kwargs)


@pytest.mark.parametrize("left,right", [
    ("三体", "三体"), ("Café", "Cafe\u0301"), ("Été", "ÉTÉ"),
])
def test_whole_unicode_titles_and_real_creator_overlap(db, left, right):
    a = _item(db, title=left, authors="刘慈欣, Another Writer")
    b = _item(db, title=right, media_type="ebook", authors="Different Writer, 刘慈欣")
    candidate = connections.candidate_for_pair(db, a, b)
    assert candidate is not None
    assert candidate["evidence"][0]["kind"] == "title_creator"
    assert "刘慈欣" in candidate["explanation"]
    assert candidate["evidence_key"] == connections.candidate_for_pair(db, b, a)["evidence_key"]
    assert connections.suggestions(db) == [candidate]


@pytest.mark.parametrize("left_title,right_title,left_author,right_author", [
    ("Dune", "Dune Messiah", "Frank Herbert", "Frank Herbert"),
    ("Café", "Cafe", "A Writer", "A Writer"),
    ("!!!", "???", "A Writer", "A Writer"),
    ("三体", "活着", "刘慈欣", "刘慈欣"),
    ("Dune", "Dune", None, None),
    ("Dune", "Dune", "Frank Herbert", None),
    ("Dune", "Dune", "Unknown", "Unknown"),
    ("Dune", "Dune", "!!!", "???"),
    ("Dune", "Dune", "John Smith", "Jane Smith"),
    ("Dune", "Dune", "Alice, Ken Liu - translator", "Bob, Ken Liu - translator"),
])
def test_never_title_alone_empty_folds_or_partial_name_overlap(
    db, left_title, right_title, left_author, right_author,
):
    a = _item(db, title=left_title, authors=left_author)
    b = _item(db, title=right_title, media_type="ebook", authors=right_author)
    assert connections.candidate_for_pair(db, a, b) is None
    assert connections.suggestions(db) == []


def test_subtitles_prevent_study_guide_guessing(db):
    a = _item(db, subtitle="A novel")
    b = _item(db, media_type="ebook", subtitle="A study guide")
    assert connections.candidate_for_pair(db, a, b) is None


@pytest.mark.parametrize("other", ["dvd", "digital_video", "tv_series", "video_game", "cd", "comic", "manga", "magazine"])
def test_never_cross_content_families_even_with_same_book_identifier(db, other):
    a = _item(db, hardcover_book_id=42, isbn="9780441172719")
    b = _item(db, media_type=other, hardcover_book_id=42, isbn="9780441172719")
    assert connections.candidate_for_pair(db, a, b) is None
    assert connections.suggestions(db) == []


def test_hardcover_identity_is_exact_evidence_without_creators_or_title_agreement(db):
    a = _item(db, title="Local title", authors=None, hardcover_book_id=42)
    b = _item(db, title="Translated title", media_type="audiobook", authors=None, hardcover_book_id=42)
    candidate = connections.candidate_for_pair(db, a, b)
    assert candidate["evidence"] == [{"kind": "hardcover_book_id", "value": "42", "label": "Same Hardcover book ID"}]
    assert "42" in candidate["explanation"]
    assert connections.suggestions(db) == [candidate]


def test_validated_isbn10_and_isbn13_agree(db):
    a = _item(db, title="Local", authors=None, isbn10="0-441-17271-7")
    b = _item(db, title="Remote", media_type="ebook", authors=None, isbn="9780441172719")
    candidate = connections.candidate_for_pair(db, a, b)
    assert candidate["evidence"][0]["kind"] == "isbn"
    assert candidate["evidence"][0]["value"] == "9780441172719"


@pytest.mark.parametrize("identifier", ["9780441172710", "012345678905", "9780000000000", "url9780441172719"])
def test_invalid_isbn_or_retail_barcode_is_not_identifier_evidence(db, identifier):
    a, b, _candidate = _pair(db, authors=None)
    # Historical/imported corruption predates today's validated write funnel.
    db.execute("UPDATE items SET isbn = ? WHERE id IN (?, ?)", (identifier, a, b))
    assert connections.candidate_for_pair(db, a, b) is None
    assert connections.suggestions(db) == []


def test_music_release_group_identity_and_artist_credit(db):
    group = "f079f65c-6521-407b-8c30-6bb5c6c853c1"
    a = _item(db, title="Local album", media_type="vinyl", authors=None)
    b = _item(db, title="Remote album", media_type="digital_music", authors=None)
    for item in (a, b):
        db.execute("INSERT INTO music_releases (item_id, musicbrainz_release_group_id) VALUES (?, ?)", (item, group))
    candidate = connections.candidate_for_pair(db, a, b)
    assert candidate["evidence"][0]["kind"] == "musicbrainz_release_group"
    assert group in candidate["explanation"]
    db.execute("UPDATE music_releases SET musicbrainz_release_group_id = NULL, artist_credit = 'Björk'")
    db.execute("UPDATE items SET title = 'Debut' WHERE id IN (?, ?)", (a, b))
    candidate = connections.candidate_for_pair(db, a, b)
    assert candidate["evidence"][0]["kind"] == "title_creator"


def test_conflicting_known_work_ids_override_title_match(db):
    a = _item(db, hardcover_book_id=42)
    b = _item(db, media_type="ebook", hardcover_book_id=43)
    assert connections.candidate_for_pair(db, a, b) is None
    assert connections.suggestions(db) == []


@pytest.mark.parametrize("link_type", ["related", "adaptation", "format"])
def test_existing_direct_relationship_is_not_proposed_or_converted(db, link_type):
    a, b, _candidate = _pair(db)
    media_groups.link_items(db, a, b, link_type=link_type)
    assert connections.candidate_for_pair(db, a, b) is None
    assert connections.suggestions(db) == []
    assert db.execute("SELECT link_type FROM item_links").fetchone()[0] == link_type


@pytest.mark.parametrize("link_type", ["related", "adaptation", "format"])
def test_legacy_reversed_direct_edge_is_not_a_proposal_or_rewritten(db, link_type):
    a, b, _candidate = _pair(db)
    # Old merges could leave this orientation; bypass today's write funnel.
    db.execute("INSERT INTO item_links (item_a_id, item_b_id, link_type) VALUES (?, ?, ?)",
               (b, a, link_type))
    assert connections.candidate_for_pair(db, a, b) is None
    assert connections.candidate_for_pair(db, b, a) is None
    assert connections.suggestions(db) == []
    if link_type != "format":
        direct, _root = connections._graph(db)
        assert direct == {(a, b)}
    assert tuple(db.execute("SELECT item_a_id, item_b_id, link_type FROM item_links").fetchone()) == (b, a, link_type)


def test_live_format_component_skipped_but_trashed_bridge_cannot_connect(db, monkeypatch):
    a, b, _candidate = _pair(db)
    bridge = _item(db, title="Bridge", media_type="audiobook")
    media_groups.link_items(db, a, bridge, link_type="format")
    media_groups.link_items(db, bridge, b, link_type="format")
    monkeypatch.setattr(media_groups, "related_ids", Mock(side_effect=AssertionError("all-edge graph used")))
    assert connections.candidate_for_pair(db, a, b) is None
    assert connections.suggestions(db) == []
    item_write.trash_item(db, bridge)
    assert connections.candidate_for_pair(db, a, b) is not None
    assert [(s["item_a_id"], s["item_b_id"]) for s in connections.suggestions(db)] == [(a, b)]


def test_indirect_related_edges_do_not_hide_format_evidence(db):
    a, b, _candidate = _pair(db)
    bridge = _item(db, title="Related work")
    media_groups.link_items(db, a, bridge, link_type="related")
    media_groups.link_items(db, bridge, b, link_type="adaptation")
    assert connections.candidate_for_pair(db, a, b) is not None
    assert len(connections.suggestions(db)) == 1


def test_trashed_endpoints_and_their_dismissals_never_render(db):
    a, b, candidate = _pair(db)
    db.execute("INSERT INTO connection_dismissals (item_a_id, item_b_id, evidence_key) VALUES (?, ?, ?)",
               (a, b, candidate["evidence_key"]))
    item_write.trash_item(db, b)
    assert connections.candidate_for_pair(db, a, b) is None
    assert connections.review(db) == {"suggestions": [], "dismissed": [], "scan_limited": False}


def test_dismissal_hides_only_same_metadata_and_note_edits_are_irrelevant(db):
    a, b, candidate = _pair(db, hardcover_book_id=42)
    db.execute("INSERT INTO connection_dismissals (item_a_id, item_b_id, evidence_key) VALUES (?, ?, ?)",
               (a, b, candidate["evidence_key"]))
    item_write.update_item_fields(db, a, {"notes": "My note"})
    assert connections.suggestions(db) == []
    assert connections.review(db)["dismissed"] == [candidate]
    item_write.update_item_fields(db, a, {"title": "Corrected title"})
    resurfaced = connections.suggestions(db)
    assert len(resurfaced) == 1
    assert resurfaced[0]["evidence_key"] != candidate["evidence_key"]
    assert connections.review(db)["dismissed"] == []


def test_generation_is_bounded_deterministic_and_not_query_per_pair(db, monkeypatch):
    for _ in range(130):
        _item(db)
    original = connections._candidate
    checked = Mock(wraps=original)
    monkeypatch.setattr(connections, "_candidate", checked)
    statements = []
    db.set_trace_callback(statements.append)
    first = connections.suggestions(db, limit=1000)
    db.set_trace_callback(None)
    assert len(first) == connections.MAX_SUGGESTIONS
    assert checked.call_count <= connections.MAX_PAIR_CHECKS
    assert len(statements) < 15
    assert first == connections.suggestions(db, limit=1000)
    assert connections.review(db)["scan_limited"]
    assert connections.suggestions(db, limit=0) == []


def test_unique_titles_do_not_compare_entire_catalogue_quadratically(db, monkeypatch):
    for index in range(350):
        _item(db, title=f"Distinct title {index}")
    checked = Mock(wraps=connections._candidate)
    monkeypatch.setattr(connections, "_candidate", checked)
    assert connections.suggestions(db) == []
    assert checked.call_count == 0


def test_accept_is_idempotent_links_only_format_and_never_merges(editor_client, db, monkeypatch):
    a, b, candidate = _pair(db)
    db.commit()
    linked = Mock(wraps=media_groups.link_items)
    monkeypatch.setattr(media_groups, "link_items", linked)
    first = _post(editor_client, "accept", candidate)
    assert first.status_code == 303
    assert first.headers["location"].startswith("/needs-attention?")
    assert _post(editor_client, "accept", candidate).status_code == 303
    linked.assert_called_once()
    assert linked.call_args.kwargs == {"link_type": "format"}
    assert tuple(db.execute("SELECT item_a_id, item_b_id, link_type FROM item_links").fetchone()) == (a, b, "format")
    assert db.execute("SELECT COUNT(*) FROM items_live").fetchone()[0] == 2
    result = editor_client.get(first.headers["location"])
    assert result.status_code == 200
    assert "No records were merged" in result.text
    assert f'/item/{a}#related-media-panel' in result.text
    assert "Unlink under Related Media" in result.text


def test_htmx_accept_returns_self_contained_fragment_with_undo_links(editor_client, db):
    a, b, candidate = _pair(db)
    db.commit()
    result = _post(editor_client, "accept", candidate, headers={"HX-Request": "true"})
    assert result.status_code == 200
    assert 'id="connection-suggestions"' in result.text
    assert 'data-connection-result="accepted"' in result.text
    assert f'/item/{b}#related-media-panel' in result.text
    assert "<html" not in result.text


def test_dismiss_restore_and_metadata_change_reappearance(editor_client, db):
    a, b, candidate = _pair(db, hardcover_book_id=42)
    db.commit()
    assert _post(editor_client, "dismiss", candidate).status_code == 303
    assert _post(editor_client, "dismiss", candidate).status_code == 303
    assert connections.suggestions(db) == []
    assert db.execute("SELECT COUNT(*) FROM connection_dismissals").fetchone()[0] == 1
    restored = _post(editor_client, "restore", candidate, headers={"HX-Request": "true"})
    assert restored.status_code == 200
    assert 'data-connection-result="restored"' in restored.text
    assert connections.suggestions(db)
    assert _post(editor_client, "dismiss", candidate).status_code == 303
    item_write.update_item_fields(db, a, {"title": "New metadata title"})
    db.commit()
    assert connections.suggestions(db)[0]["evidence_key"] != candidate["evidence_key"]
    assert _post(editor_client, "restore", candidate).status_code == 409
    assert db.execute("SELECT COUNT(*) FROM connection_dismissals").fetchone()[0] == 1


@pytest.mark.parametrize("action", ["accept", "dismiss", "restore"])
def test_metadata_snapshot_is_rechecked_on_all_writes(editor_client, db, action):
    a, b, candidate = _pair(db, hardcover_book_id=42)
    item_write.update_item_fields(db, b, {"authors": "Corrected credit"})
    db.commit()
    assert _post(editor_client, action, candidate).status_code == 409
    assert db.execute("SELECT COUNT(*) FROM item_links").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM connection_dismissals").fetchone()[0] == 0


@pytest.mark.parametrize("action", ["accept", "dismiss", "restore"])
def test_live_endpoints_rechecked_on_all_writes(editor_client, db, action):
    a, b, candidate = _pair(db)
    item_write.trash_item(db, b)
    db.commit()
    assert _post(editor_client, action, candidate).status_code == 404
    assert db.execute("SELECT COUNT(*) FROM item_links").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM connection_dismissals").fetchone()[0] == 0


@pytest.mark.parametrize("link_type", ["adaptation", "related"])
@pytest.mark.parametrize("action", ["accept", "dismiss", "restore"])
def test_new_nonformat_relationship_is_never_converted(editor_client, db, link_type, action):
    a, b, candidate = _pair(db)
    media_groups.link_items(db, a, b, link_type=link_type)
    db.commit()
    assert _post(editor_client, action, candidate).status_code == 409
    assert db.execute("SELECT link_type FROM item_links").fetchone()[0] == link_type


@pytest.mark.parametrize("link_type", ["adaptation", "related"])
@pytest.mark.parametrize("layout", ["reversed_only", "reversed_nonformat", "canonical_nonformat"])
@pytest.mark.parametrize("action", ["accept", "dismiss", "restore"])
def test_stale_review_cannot_bypass_reversed_or_mixed_type_edges(
    editor_client, db, monkeypatch, link_type, layout, action,
):
    a, b, candidate = _pair(db)
    db.execute("INSERT INTO connection_dismissals (item_a_id, item_b_id, evidence_key) VALUES (?, ?, ?)",
               (a, b, candidate["evidence_key"]))
    nonformat_pair = (a, b) if layout == "canonical_nonformat" else (b, a)
    db.execute("INSERT INTO item_links (item_a_id, item_b_id, link_type) VALUES (?, ?, ?)",
               (*nonformat_pair, link_type))
    if layout != "reversed_only":
        db.execute("INSERT INTO item_links (item_a_id, item_b_id, link_type) VALUES (?, ?, 'format')",
                   tuple(reversed(nonformat_pair)))
    before = [tuple(row) for row in db.execute(
        "SELECT item_a_id, item_b_id, link_type FROM item_links ORDER BY item_a_id, item_b_id"
    )]
    # The fingerprint is still valid metadata evidence, but its graph snapshot
    # predates the conflicting edge. A central link guard is not a substitute
    # for refusing the review itself, including an idempotent format retry.
    assert connections.evidence_for_pair(db, a, b)["evidence_key"] == candidate["evidence_key"]
    assert connections.candidate_for_pair(db, a, b) is None
    assert connections.suggestions(db) == []
    db.commit()
    linked = Mock(wraps=media_groups.link_items)
    monkeypatch.setattr(media_groups, "link_items", linked)
    response = _post(editor_client, action, candidate)
    assert response.status_code == 409
    assert "different relationship" in response.text
    linked.assert_not_called()
    assert [tuple(row) for row in db.execute(
        "SELECT item_a_id, item_b_id, link_type FROM item_links ORDER BY item_a_id, item_b_id"
    )] == before
    assert db.execute("SELECT evidence_key FROM connection_dismissals").fetchone()[0] == candidate["evidence_key"]


@pytest.mark.parametrize("action", ["accept", "dismiss", "restore"])
def test_editor_role_and_feature_gate_on_every_write(viewer_client, db, action):
    from app.database import set_setting

    _a, _b, candidate = _pair(db)
    set_setting(db, "feature.connected_library", "0")
    db.commit()
    response = _post(viewer_client, action, candidate)
    assert response.status_code == 403
    assert "disabled" not in response.text.lower()
    assert db.execute("SELECT COUNT(*) FROM item_links").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM connection_dismissals").fetchone()[0] == 0


@pytest.mark.parametrize("action", ["accept", "dismiss", "restore"])
def test_feature_off_refuses_editor_writes(editor_client, db, action):
    from app.database import set_setting

    _a, _b, candidate = _pair(db)
    set_setting(db, "feature.connected_library", "0")
    db.commit()
    assert _post(editor_client, action, candidate).status_code == 403
    assert db.execute("SELECT COUNT(*) FROM item_links").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM connection_dismissals").fetchone()[0] == 0


@pytest.mark.parametrize("action", ["accept", "dismiss", "restore"])
def test_mutating_forms_require_csrf(editor_client, db, action):
    _a, _b, candidate = _pair(db)
    db.commit()
    editor_client.headers.pop("X-CSRF-Token")
    assert _post(editor_client, action, candidate).status_code == 403
    assert db.execute("SELECT COUNT(*) FROM item_links").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM connection_dismissals").fetchone()[0] == 0


def test_plain_form_hidden_csrf_and_fixed_local_redirect(editor_client, db):
    _a, _b, candidate = _pair(db)
    db.commit()
    token = editor_client.headers.pop("X-CSRF-Token")
    response = editor_client.post("/api/library/connections/accept", data={
        **_form(candidate), "_csrf": token, "next": "https://attacker.invalid/",
    }, headers={"Referer": "https://attacker.invalid/", "HX-Current-URL": "https://attacker.invalid/"},
        follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/needs-attention?")
    assert "attacker" not in response.headers["location"]


@pytest.mark.parametrize("action", ["accept", "dismiss", "restore"])
def test_write_lock_precedes_live_evidence_and_render_is_after_release(editor_client, db, monkeypatch, action):
    from app.database import get_db
    from app.routers import library_attention as routes

    _a, _b, candidate = _pair(db)
    db.commit()
    statements = []
    locked = {"value": False}

    class RecordingConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, *args):
            statements.append(sql)
            result = self.connection.execute(sql, *args)
            if sql == "BEGIN IMMEDIATE":
                locked["value"] = True
            elif sql.startswith("SELECT") and locked["value"]:
                # A second writer cannot slip in between the guard and act.
                with get_db() as other:
                    other.execute("PRAGMA busy_timeout=0")
                    with pytest.raises(sqlite3.OperationalError, match="locked"):
                        other.execute("BEGIN IMMEDIATE")
            return result

        def __getattr__(self, name):
            return getattr(self.connection, name)

    @contextmanager
    def recording_db():
        try:
            with get_db() as connection:
                yield RecordingConnection(connection)
        finally:
            locked["value"] = False

    monkeypatch.setattr(routes, "get_db", recording_db)
    original_log = logging.Logger._log

    def log(self, *args, **kwargs):
        assert not locked["value"], "Logging under BEGIN IMMEDIATE can deadlock the SQLite handler"
        return original_log(self, *args, **kwargs)

    monkeypatch.setattr(logging.Logger, "_log", log)
    original_saved = routes._saved

    def saved(*args, **kwargs):
        assert not locked["value"], "Rendering before transaction release"
        return original_saved(*args, **kwargs)

    monkeypatch.setattr(routes, "_saved", saved)
    assert _post(editor_client, action, candidate).status_code == 303
    assert statements[0] == "BEGIN IMMEDIATE"
