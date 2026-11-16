"""Real Italian/Chinese cassette previews against compiled server catalogs.

Only the existing ASGI fixture's outbound provider boundary is replaced. The
literal expectations below are transcribed from the frozen parent proposal
CASSETTE_TRANSLATIONS_COMBINED.json, never from gettext or an English fallback.
Run this file in its own pytest process, separate from the English browser band.
"""

import json
import os
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import assert_page_clean, attach_page_guard
from tests.e2e.test_cassette_no_barcode import (
    _artifact, _assert_selected_track_order, _rows, _search, live_server,
)

pytestmark = pytest.mark.e2e

# Exact canonical proposals, not translator-derived expected values. These
# eight visible messages are exercised in each journey; the other new error/
# saved-item messages require their own applicable flows and are not claimed here.
_EXPECTED = {
    "it": {
        "Search MusicBrainz and cassette catalogue": "Cerca in MusicBrainz e nel catalogo delle cassette",
        "Search MusicBrainz first. For cassettes with no matches, Shelf also checks public Discogs releases by title and artist. Compare the pressing before saving; a barcode is optional.": "Cerca prima in MusicBrainz. Per le cassette senza corrispondenze, Shelf cerca anche tra le edizioni pubbliche di Discogs per titolo e artista. Confronta l'edizione prima di salvare; il codice a barre è facoltativo.",
        "These are possible releases, not an automatic pressing identification. Check the country, year, catalogue number and track order against your copy.": "Queste sono possibili edizioni, non un'identificazione automatica dell'edizione. Confronta il paese, l'anno, il numero di catalogo e l'ordine delle tracce con la tua copia.",
        "Add by hand — pressing unknown": "Aggiungi a mano — edizione sconosciuta",
        "If none of the releases matches your copy, keep only what you know. This does not attach a provider release, barcode or track list.": "Se nessuna delle edizioni corrisponde alla tua copia, conserva solo le informazioni che conosci. Questa operazione non associa edizioni di un fornitore, codici a barre o elenchi delle tracce.",
        "Add by hand": "Aggiungi a mano",
        "View release": "Visualizza edizione",
        "Discogs details are displayed live. Applying this preview saves only the catalogue fields and artwork you select, plus the pressing reference. It does not replace existing MusicBrainz metadata or copy Discogs tracks into the catalogue.": "I dettagli Discogs vengono visualizzati in tempo reale. Applicando questa anteprima si salvano solo i campi del catalogo e la copertina selezionati, oltre al riferimento all'edizione. I metadati MusicBrainz esistenti non vengono sostituiti e le tracce Discogs non vengono copiate nel catalogo.",
    },
    "zh-Hans": {
        "Search MusicBrainz and cassette catalogue": "搜索 MusicBrainz 和磁带目录",
        "Search MusicBrainz first. For cassettes with no matches, Shelf also checks public Discogs releases by title and artist. Compare the pressing before saving; a barcode is optional.": "先搜索 MusicBrainz。对于没有匹配结果的磁带，Shelf 还会按标题和艺术家查找公开的 Discogs 发行版本。请在保存前比较具体发行版本；条形码为可选项。",
        "These are possible releases, not an automatic pressing identification. Check the country, year, catalogue number and track order against your copy.": "这些是可能的发行版本，并非对具体发行版本的自动识别。请对照您的副本核对国家或地区、年份、唱片编号和曲目顺序。",
        "Add by hand — pressing unknown": "手动添加 — 具体发行版本未知",
        "If none of the releases matches your copy, keep only what you know. This does not attach a provider release, barcode or track list.": "如果没有任何发行版本与您的副本相符，请仅保留您已知的信息。此操作不会关联数据提供方的发行版本、条形码或曲目列表。",
        "Add by hand": "手动添加",
        "View release": "查看发行版本",
        "Discogs details are displayed live. Applying this preview saves only the catalogue fields and artwork you select, plus the pressing reference. It does not replace existing MusicBrainz metadata or copy Discogs tracks into the catalogue.": "Discogs 详情为实时读取。应用此预览仅保存您选中的目录字段与封面，以及具体发行版本参考。不会替换现有 MusicBrainz 元数据，也不会将 Discogs 曲目复制到目录中。",
    },
}


@pytest.fixture(scope="session", autouse=True)
def _locale_server_environment():
    """Session autouse runs before the imported session live_server starts.

    The child captures this flag at process startup. Function-scoped unit DB
    isolation may clear the parent's flag later, but cannot change that child.
    No production setting or account language preference is rewritten.
    """
    previous = os.environ.get("SHELF_I18N")
    os.environ["SHELF_I18N"] = "on"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("SHELF_I18N", None)
        else:
            os.environ["SHELF_I18N"] = previous


def _physical_snapshot(server):
    return {table: _rows(server, f"SELECT * FROM {table} ORDER BY 1") for table in (
        "items", "item_copies", "music_releases", "music_media", "music_tracks", "music_identifiers",
    )}


def _provider_audit(server):
    path = server["audit"]
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _assert_all_page_guards(page, console_errors, csp_violations, unexpected_hosts):
    assert_page_clean(page)
    assert not console_errors, f"Console errors (unfiltered): {console_errors!r}"
    assert not csp_violations, f"CSP violations (all navigations): {csp_violations!r}"
    assert not unexpected_hosts, f"Unexpected browser hosts: {unexpected_hosts!r}"


@pytest.mark.parametrize("accept_language,html_lang,width,release_id,country", [
    pytest.param("it-IT", "it", 320, 18670711, "Canada", id="it-IT-320-Canada"),
    pytest.param("zh-CN", "zh-Hans", 1280, 8297774, "Europe", id="zh-CN-1280-Europe"),
])
def test_real_cassette_preview_uses_compiled_locale_without_translating_metadata(
    live_server, browser, setup_admin, tmp_path, accept_language, html_lang, width, release_id, country,
):
    # A fresh language-specific context per case; setup_admin is the real UI
    # wizard and this login uses its credentials, not fabricated auth cookies.
    context = browser.new_context(
        locale=accept_language, extra_http_headers={"Accept-Language": accept_language},
        viewport={"width": width, "height": 900}, service_workers="block",
    )
    console_errors, csp_violations, unexpected_hosts, api_posts = [], [], [], []
    context.expose_binding("__cassetteLocaleCspViolation", lambda source, value: csp_violations.append(value))
    context.add_init_script("""
        document.addEventListener('securitypolicyviolation', function(event) {
            window.__cassetteLocaleCspViolation({
                directive: event.violatedDirective,
                blocked: event.blockedURI,
                source: event.sourceFile,
                document: event.documentURI
            });
        });
        localStorage.setItem('shelf-welcome-dismissed', '1');
        localStorage.setItem('shelf-shortcuts-seen', '1');
    """)

    def local_only(route):
        if urlsplit(route.request.url).hostname in {"127.0.0.1", "localhost"}:
            route.continue_()
        else:
            unexpected_hosts.append(route.request.url)
            route.abort()

    context.route("**/*", local_only)
    page = attach_page_guard(context.new_page())
    # Retain every console error, with no message-based exclusions or clearing.
    page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
    page.on("request", lambda request: api_posts.append(urlsplit(request.url).path)
            if request.method == "POST" and urlsplit(request.url).path.startswith("/api/") else None)
    try:
        login = page.goto(live_server["url"] + "/login")
        assert login.ok
        csp = login.headers["content-security-policy"]
        assert "script-src 'self'" in csp
        script_policy = csp.split("script-src ", 1)[1].split(";", 1)[0]
        assert "'unsafe-inline'" not in script_policy and "'unsafe-eval'" not in script_policy
        expect(page.locator("html")).to_have_attribute("lang", html_lang)
        page.locator('[name="username"]').fill(setup_admin["username"])
        page.locator('[name="password"]').fill(setup_admin["password"])
        with page.expect_navigation():
            page.locator('button[type="submit"]').click()
        expect(page.locator("html")).to_have_attribute("lang", html_lang)
        _assert_all_page_guards(page, console_errors, csp_violations, unexpected_hosts)

        before = _physical_snapshot(live_server)
        preferences = _rows(live_server, "SELECT username, language FROM users ORDER BY id")
        audit_offset = len(_provider_audit(live_server))
        _search(page, live_server)  # Actual /music GET form; cassette + blank barcode.
        expect(page.locator("html")).to_have_attribute("lang", html_lang)
        search = page.locator('form[action="/music"][method="get"]')
        expect(search.locator('[name="q"]')).to_have_value("Hits Out of Hell")
        expect(search.locator('[name="artist"]')).to_have_value("Meat Loaf")
        expect(search.locator('[name="barcode"]')).to_have_value("")
        expect(search.locator('[name="format"]')).to_have_value("cassette")
        candidates = page.locator('form[action="/api/music/preview"]').filter(
            has=page.locator('input[name="provider"][value="discogs"]'))
        expect(candidates).to_have_count(3)
        for candidate_id, candidate_country in ((3935124, "UK"), (8297774, "Europe"), (18670711, "Canada")):
            candidate = candidates.filter(has=page.locator(f'input[name="release_id"][value="{candidate_id}"]'))
            expect(candidate).to_have_count(1)
            expect(candidate.locator('[name="format"]')).to_have_value("cassette")
            article = page.locator("article").filter(has=candidate)
            expect(article.locator("h3")).to_have_text("Meat Loaf - Hits Out Of Hell")
            expect(article).to_contain_text(candidate_country)
            expect(article).to_contain_text(f"#{candidate_id}")
            expect(article.locator(f'a[href="https://www.discogs.com/release/{candidate_id}"]')).to_have_count(1)

        chosen = candidates.filter(has=page.locator(f'input[name="release_id"][value="{release_id}"]'))
        chosen_article = page.locator("article").filter(has=chosen)
        manual_form = page.locator('form[action="/api/items/manual"]')
        manual = manual_form.locator("xpath=ancestor::details[1]")
        expect(manual).to_have_count(1)
        # Capture text structurally, not by an English/translated fallback
        # locator, so the RED baseline still reaches the actual preview path.
        observed = {
            "Search MusicBrainz and cassette catalogue": search.locator('button[type="submit"]').inner_text(),
            "Search MusicBrainz first. For cassettes with no matches, Shelf also checks public Discogs releases by title and artist. Compare the pressing before saving; a barcode is optional.": page.locator("h1").locator("..").locator("p").last.inner_text(),
            "These are possible releases, not an automatic pressing identification. Check the country, year, catalogue number and track order against your copy.": chosen.locator("xpath=ancestor::section[1]").locator(":scope > p").inner_text(),
            "Add by hand — pressing unknown": manual.locator("summary").inner_text(),
            "View release": chosen_article.locator(f'a[href="https://www.discogs.com/release/{release_id}"]').inner_text(),
        }
        manual.locator("summary").click()
        expect(manual_form).to_be_visible()
        observed["If none of the releases matches your copy, keep only what you know. This does not attach a provider release, barcode or track list."] = manual.locator(":scope > p").inner_text()
        observed["Add by hand"] = manual_form.locator('button[type="submit"]').inner_text()
        expect(manual_form.locator('[name="title"]')).to_have_value("Hits Out of Hell")
        expect(manual_form.locator('[name="authors"]')).to_have_value("Meat Loaf")
        expect(manual_form.locator('[name="media_type"]')).to_have_value("cassette")
        assert _physical_snapshot(live_server) == before
        _assert_all_page_guards(page, console_errors, csp_violations, unexpected_hosts)
        _artifact(page, tmp_path, "locale-search-and-manual-help")

        with page.expect_response(lambda response: urlsplit(response.url).path == "/api/music/preview"
                                  and response.request.method == "POST" and response.ok) as preview_response:
            chosen.locator('button[type="submit"]').click()
        response = preview_response.value
        controls = parse_qs(response.request.post_data)
        assert controls["provider"] == ["discogs"] and controls["release_id"] == [str(release_id)]
        assert controls["format"] == ["cassette"] and controls["_csrf"][0]
        assert response.request.headers["accept-language"] == accept_language
        review = page.locator('form[action="/api/music/apply-match"]')
        expect(review).to_be_visible()
        expect(review.locator('input[name="ticket"]')).not_to_have_value("")
        expect(review.locator('select[name="media_type"]')).to_have_value("cassette")
        preview = page.locator("article").filter(has=review)
        expect(preview.locator("h3")).to_have_text("Hits Out Of Hell")
        expect(preview.locator(f'a[href="https://www.discogs.com/release/{release_id}"]')).to_be_visible()
        assert "Meat Loaf" in preview.locator("dd").all_text_contents()
        assert country in preview.locator("dd").all_text_contents()
        preview.locator("details").filter(has=page.locator("ol")).locator("summary").click()
        expect(preview.locator("ol")).to_be_visible()
        _assert_selected_track_order(preview, release_id)
        observed["Discogs details are displayed live. Applying this preview saves only the catalogue fields and artwork you select, plus the pressing reference. It does not replace existing MusicBrainz metadata or copy Discogs tracks into the catalogue."] = review.locator(":scope > p").inner_text()
        expect(page.locator("html")).to_have_attribute("lang", html_lang)
        _artifact(page, tmp_path, "locale-exact-pressing-preview")
        _assert_all_page_guards(page, console_errors, csp_violations, unexpected_hosts)

        after = _physical_snapshot(live_server)
        assert after == before
        assert _rows(live_server, "SELECT username, language FROM users ORDER BY id") == preferences
        assert api_posts.count("/api/music/preview") == 1
        assert "/api/music/apply-match" not in api_posts and "/api/items/manual" not in api_posts
        audit = _provider_audit(live_server)[audit_offset:]
        assert [call["url"] for call in audit] == [
            "https://musicbrainz.org/ws/2/release/", "https://api.discogs.com/database/search",
            f"https://api.discogs.com/releases/{release_id}",
        ]
        assert all(call["method"] == "GET" and not call["authorization_present"] and not call.get("unexpected") for call in audit)
        assert "barcode" not in audit[1]["params"] and 'barcode:""' not in audit[0]["params"]["query"]

        # Reach and record auth, exact provider preview, untouched metadata,
        # guards and read-only state BEFORE the expected catalog-fallback RED.
        expected = _EXPECTED[html_lang]
        proof = {
            "accept_language": accept_language, "html_lang": page.locator("html").get_attribute("lang"),
            "width": width, "three_distinct_candidates": True, "selected_release_id": release_id,
            "country_unchanged": country, "metadata_tracks_unchanged": preview.locator("ol > li").all_text_contents(),
            "real_signed_preview_reached": bool(review.locator('input[name="ticket"]').input_value()),
            "csrf_form_present": bool(controls["_csrf"][0]), "no_catalogue_or_copy_writes": after == before,
            "physical_table_counts": {table: len(rows) for table, rows in after.items()},
            "provider_calls": audit, "console_errors": console_errors, "csp_violations": csp_violations,
            "unexpected_browser_hosts": unexpected_hosts, "observed_ui": observed, "canonical_literal_ui": expected,
        }
        (tmp_path / "locale-flow-proof.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({key: proof[key] for key in (
            "accept_language", "html_lang", "selected_release_id", "real_signed_preview_reached",
            "no_catalogue_or_copy_writes", "physical_table_counts", "console_errors", "csp_violations",
        )}, ensure_ascii=False))
        assert set(observed) == set(expected)
        mismatches = {message: {"expected": value, "actual": observed[message]}
                      for message, value in expected.items() if observed[message] != value}
        assert not mismatches, f"Compiled cassette catalog mismatch for {accept_language}: {mismatches!r}"
    finally:
        context.close()
