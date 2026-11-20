"""Native cards/forms and cache-only GET contracts. Source authored, not run."""

import pytest

from app import crypto, media_presentation
from app.database import set_setting
from app.routers import source_progress as routes
from app.services import continue_connectors, source_continue as svc, source_progress
from app.services.media_cards import build_media_card
from tests.conftest import _TEST_CSRF_TOKEN, _create_user
from tests.test_availability import _item, _record, _source
from tests.test_connected_layout import _Markup
from tests.test_media_cards import _environment
from tests.test_personal_library import _save
from tests.test_source_continue import _seed
from tests.test_source_handoff import f4_client  # noqa: F401 — scoped router fixture


def _entry(**fields):
    return continue_connectors._entry("7", "4", kind="game", media_type="video_game", title="Native game", **fields)


def _native_card(**fields):
    entry = {**_entry(percent=None), "title": "<b>Native 100%d & title</b>", "creator": "<i>Raw 50%s & creator</i>"}
    return {**entry, "key": "a" * 64, "provider": "RomM", "source_name": "Games source", "connection_id": 1,
            "catalogue_id": None, "open_href": "/api/source-continue/connections/1/entries/" + "a" * 64 + "/open",
            "observed_at": "2026-01-01T00:00:00+00:00", "stale": False, "position_label": None, "total_label": None,
            "art": {"title": entry["title"], "creator_text": entry["creator"],
                    "media_type": "video_game", "media_label": "Video Game", "cover_path": None,
                    "artwork": media_presentation.artwork_for("video_game")}, **fields}


@pytest.mark.parametrize("newstyle", [False, True])
def test_remote_only_cards_escape_metadata_share_cover_geometry_and_never_fabricate_item_ids(newstyle):
    env, calls = _environment(newstyle=newstyle)
    entry = _native_card()
    markup = _Markup(env.get_template("components/continue_card.html").render(entry=entry))
    article = markup.one("article")
    assert article.attrs["data-native-continue"] == "a" * 64 and "data-item-id" not in article.attrs
    assert article.attrs["data-card-presentation"] == "shelf-preview-v1"
    assert article.attrs["data-cover-size-card"] == "catalogue-v2"
    assert markup.one("h3").text == entry["art"]["title"]
    assert not markup.find("b") and not markup.find("i") and not markup.find("img")
    assert "Remote only" in article.text and "Progress not supplied" in article.text and "0%" not in article.text
    assert markup.one("a").attrs["href"] == entry["open_href"]
    assert markup.one("a").attrs["aria-label"] == "Open in Games source: " + entry["title"]
    assert not any(node.attrs.get("href", "").startswith("/item/") for node in markup.find("a"))
    assert entry["art"]["title"] not in calls and entry["art"]["creator_text"] not in calls


def test_home_combines_native_and_local_without_duplicate_local_edition_or_dropping_other_source_details():
    env, _ = _environment()
    env.globals["build_media_card"] = build_media_card
    local = {"id": 7, "title": "Local edition", "media_type": "video_game", "status": "in_progress", "progress": 81}
    planned = {**local, "id": 9, "title": "Local plan", "status": "planned"}
    native = _native_card(catalogue_id=7)
    other_source = _native_card(key="b" * 64, catalogue_id=7, source_name="Another personal source")
    html = env.get_template("fragments/home_personal_library.html").render(home_activity={
        "continue_items": [local], "up_next_items": [local, planned],
        "native_continue": {"entries": [native, other_source], "connections": [], "catalogue_ids": [7]},
    })
    markup = _Markup(html)
    cards = markup.find("article")
    assert len(cards) == 3 and [card.attrs.get("data-item-id") for card in cards] == ["7", "7", "9"]
    assert len(markup.find("article", **{"data-native-continue": "a" * 64})) == 1
    assert "81%" not in markup.one("section").text and "Another personal source" in markup.one("section").text
    assert len(markup.find("ul")) == 1 and len(markup.find("button")) == 2


def test_home_activity_and_account_gets_are_cache_only_and_scoped_to_the_current_user(f4_client, viewer_user, db, monkeypatch):
    connection = _seed(db, viewer_user["id"], entries=[_entry()])
    db.commit()
    other = _create_user("other-native-display", "synthetic-password", "Other", "viewer")
    def forbidden(*args, **kwargs):
        raise AssertionError("GET cannot decrypt or contact a configured integration")
    monkeypatch.setattr(svc, "_client", forbidden)
    monkeypatch.setattr(source_progress, "_client", forbidden)
    monkeypatch.setattr(crypto, "decrypt_value", forbidden)
    before = {table: tuple(tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid"))
              for table in ("user_continue_connections", "items", "user_item_state", "user_item_events")}
    for path in ("/", "/activity", "/account/source-progress"):
        response = f4_client.get(path)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert "not-a-usable-token" not in response.text
        if path != "/account/source-progress":
            assert 'data-native-continue="' in response.text and "Native game" in response.text
    assert svc.context(db, other["id"])["entries"] == []
    entry = svc.context(db, viewer_user["id"])["entries"][0]
    result = f4_client.get(entry["open_href"] + "?url=https://attacker.invalid", follow_redirects=False)
    assert result.status_code == 303 and result.headers["location"] == "https://games.native.invalid/rom/7"
    assert result.headers["referrer-policy"] == "no-referrer" and result.headers["cache-control"] == "no-store"
    assert f4_client.get(f"/api/source-continue/connections/{connection}/entries/" + "0" * 64 + "/open").status_code == 404
    after = {table: tuple(tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")) for table in before}
    assert after == before


def test_existing_manual_plans_survive_an_empty_native_snapshot(f4_client, viewer_user, db):
    item = _item(db, "Manual remains", "book")
    db.commit()
    _save(db, viewer_user["id"], item, status="in_progress", progress="23", on_list="1")
    _seed(db, viewer_user["id"])
    db.commit()
    markup = _Markup(f4_client.get("/").text)
    personal = markup.testid("home-personal-library")
    assert "Manual remains" in personal.text and "23%" in personal.text
    assert not markup.find("article", within=personal, **{"data-native-continue": "a" * 64})


def test_settings_explain_opds_limitations_and_keep_cache_consent_separate(f4_client, viewer_user, db):
    _seed(db, viewer_user["id"])
    db.commit()
    response = f4_client.get("/account/source-progress")
    markup = _Markup(response.text)
    settings = markup.testid("native-continue-settings")
    assert "OPDS" in settings.text and "no standard personal Continue feed" in settings.text
    assert "RomM" in settings.text and "no completion percentage" in settings.text
    forms = markup.find("form", within=settings)
    assert len(forms) == 4
    assert all(markup.find("input", within=form, name="_csrf", value=_TEST_CSRF_TOKEN) for form in forms)
    link = markup.one("form", within=settings, action="/api/source-continue/connections/link")
    assert markup.one("input", within=link, name="consent").attrs["value"] == svc.CONSENT_VERSION
    assert markup.one("input", within=link, name="token").attrs["type"] == "password"
    schedule = next(form for form in forms if form.attrs["action"].endswith("/schedule"))
    assert markup.one("input", within=schedule, name="consent").attrs["value"] == svc.SCHEDULE_CONSENT_VERSION
    assert "selected" in markup.one("option", within=schedule, value="0").attrs


@pytest.mark.parametrize("path", ["/api/source-continue/connections/link/", "/api/source-continue/connections/bad/refresh",
    "/api/source-continue/connections/9223372036854775808/schedule/", "/api/source-continue/connections/é/disconnect"])
def test_native_continue_forms_receive_the_same_pre_csrf_body_bound(path):
    assert routes.form_limit(path) == routes.FORM_BYTES


@pytest.mark.parametrize("path", ["/api/source-continue/connections", "/api/source-continue/connections//refresh",
    "/api/source-continue/connections/1/entries/x/open", "/account/source-progress", "/prefix/api/source-continue/connections/link"])
def test_continue_form_bound_does_not_claim_gets_or_other_routes(path):
    assert routes.form_limit(path) is None


def test_native_forms_enforce_csrf_and_reject_duplicate_fields_before_remote_work(f4_client, viewer_user, db, monkeypatch):
    _seed(db, viewer_user["id"])
    db.commit()
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid form must not perform HTTP")
    monkeypatch.setattr(svc, "_client", forbidden)
    url = "/api/source-continue/connections/link"
    assert f4_client.post(url, data={"source_key": "romm"}, headers={"X-CSRF-Token": ""}).status_code == 403
    result = f4_client.post(url, content="source_key=romm&source_key=abs", headers={
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    assert result.status_code == 422 and result.json()["code"] == "invalid_form"
    result = f4_client.post(url, content="x=" + "a" * routes.FORM_BYTES, headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert result.status_code == 413


def test_episode_and_track_caches_do_not_link_to_parent_series_album_or_whole_audiobook(db, viewer_user):
    series = _item(db, "Catalogue series", "tv_series")
    source_id = _source(db, provider="jellyfin", base_url="https://jelly.native.invalid", browser_url="")
    _record(db, source_id, series, external_id="series", media_type="tv_series")
    source = svc._source(db, f"source:{source_id}")
    episode = continue_connectors._entry("series", None, kind="episode", media_type="tv_series", title="Episode", native_type="Episode")
    track = continue_connectors._entry("series", None, kind="track", media_type="digital_music", title="Track", native_type="Audio")
    assert svc._match(db, source, episode) == (None, False)
    assert svc._match(db, source, track) == (None, False)
    audio = _item(db, "Whole audio", "audiobook", abs_id="audio", abs_library_id="lib")
    set_setting(db, "abs_url", "https://abs.native.invalid")
    podcast = continue_connectors._entry("audio", "lib", episode_id="episode", kind="episode", media_type="audiobook", title="Podcast episode")
    assert svc._match(db, svc._source(db, "abs"), podcast) == (None, False)
    db.execute("UPDATE items SET deleted_at = '2026-01-01' WHERE id = ?", (audio,))
    assert svc._match(db, svc._source(db, "abs"), podcast) == (None, True)


def test_native_endpoints_are_owner_only_and_feature_gated_before_remote_work(f4_client, viewer_user, db, monkeypatch):
    from tests.test_personal_library import _login
    connection = _seed(db, viewer_user["id"], entries=[_entry()])
    db.commit()
    other = _create_user("native-route-other", "synthetic-password", "Other", "viewer")
    _login(f4_client, other)
    def forbidden(*args, **kwargs):
        raise AssertionError("Refused account/feature must not make HTTP requests")
    monkeypatch.setattr(svc, "_client", forbidden)
    for action in ("refresh", "schedule", "disconnect"):
        result = f4_client.post(f"/api/source-continue/connections/{connection}/{action}",
                               data={"interval_minutes": "0", "user_id": str(viewer_user["id"])}, headers={"Accept": "application/json"})
        assert result.status_code == 404
    entry_key = svc._hash(_entry())
    handoff = f"/api/source-continue/connections/{connection}/entries/{entry_key}/open"
    assert f4_client.get(handoff, follow_redirects=False).status_code == 404
    set_setting(db, "feature.connected_library", "0")
    db.commit()
    assert f4_client.post(f"/api/source-continue/connections/{connection}/refresh", data={}).status_code == 403
    assert f4_client.get(handoff, follow_redirects=False).status_code == 403
