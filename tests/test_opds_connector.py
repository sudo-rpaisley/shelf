"""Fixture-only OPDS protocol/security tests. No real outbound requests."""

import asyncio
import base64
import json
from xml.sax.saxutils import escape

import httpx
import pytest

from app.services.source_connectors import opds
from app.services.source_connectors.base import ConnectorError, browser_link, safe_url, validate_root_url

ROOT = "http://books.lan/opds"


def atom(entries="", links="", extra=""):
    return (f'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/terms/" '
            f'xmlns:os="http://a9.com/-/spec/opensearch/1.1/">{links}{extra}{entries}</feed>').encode()


def entry(identity="opaque:1", title="A book", links=None, extra=""):
    if links is None:
        links = '<link rel="http://opds-spec.org/acquisition/open-access" href="/files/book.epub" type="application/epub+zip"/>'
    return f"<entry><id>{escape(identity)}</id><title>{escape(title)}</title>{links}{extra}</entry>"


def publication(identity="urn:uuid:1", title="A book", links=None):
    return {"metadata": {"identifier": identity, "title": title}, "links": links or []}


def install_transport(monkeypatch, handler):
    real_client = httpx.AsyncClient
    requests = []

    def dispatch(request):
        assert request.headers["accept-encoding"] == "identity"
        requests.append(request)
        return handler(request)

    def client(**kwargs):
        assert kwargs["follow_redirects"] is False
        assert kwargs["trust_env"] is False
        return real_client(transport=httpx.MockTransport(dispatch), **kwargs)

    monkeypatch.setattr(opds.httpx, "AsyncClient", client)
    return requests


async def fetch(options=None, credentials=None, **source):
    return await opds.fetch_catalogue({"base_url": ROOT, "options": options or {}, **source}, credentials or {})


async def test_atom_unicode_namespace_isbn_and_open_access(monkeypatch):
    payload = atom(entry("urn:uuid:東京/é", "日本語 — Éclair", extra=(
        "<author><name>山田 太郎</name></author><author><name>É. Writer</name></author>"
        "<dc:identifier>urn:isbn:9780306406157</dc:identifier><dc:language>ja</dc:language>"
        "<summary>Résumé</summary>")))
    install_transport(monkeypatch, lambda request: httpx.Response(200, content=payload))
    result = await fetch()
    assert result.complete and not result.warnings
    record = result.records[0]
    assert record.external_id == "urn:uuid:東京/é"
    assert record.title == "日本語 — Éclair"
    assert record.authors == "山田 太郎, É. Writer"
    assert record.isbn == "9780306406157"
    assert record.media_type == "ebook"
    assert record.open_url == "http://books.lan/files/book.epub"
    assert record.format == "application/epub+zip"
    assert record.metadata == {"language": "ja", "description": "Résumé"}


async def test_opds2_json_groups_and_relative_acquisition(monkeypatch):
    book = publication("urn:isbn:9780306406157", "Éclair", [
        {"rel": [opds.OPEN_ACCESS], "href": "files/1.epub", "type": "application/epub+zip"},
    ])
    book["metadata"].update(author=[{"name": "Alice"}, "Bob"], published="2026", description="Résumé")
    payload = {"metadata": {"numberOfItems": 1}, "groups": [{"publications": [book]}]}
    install_transport(monkeypatch, lambda request: httpx.Response(200, json=payload))
    result = await fetch()
    assert result.complete
    assert result.records[0].authors == "Alice, Bob"
    assert result.records[0].isbn == "9780306406157"
    assert result.records[0].open_url == "http://books.lan/files/1.epub"


async def test_basic_auth_is_only_sent_to_same_origin_pages(monkeypatch):
    expected = "Basic " + base64.b64encode(b"reader:private-password").decode()

    def handler(request):
        assert request.url.host == "books.lan"
        assert request.headers["authorization"] == expected
        if not request.url.query:
            return httpx.Response(200, content=atom(entry(), '<link rel="next" href="?page=2"/>'))
        return httpx.Response(200, content=atom(entry("opaque:2")))

    requests = install_transport(monkeypatch, handler)
    result = await fetch(credentials={"username": "reader", "password": "private-password"})
    assert result.complete and len(result.records) == 2
    assert len(requests) == 2


@pytest.mark.parametrize("destination", ["https://evil.example/opds", "http://books.lan:8000/opds", "https://books.lan/opds", "http://u:p@books.lan/opds", "/opds?token=private-password"])
async def test_cross_origin_or_credential_pagination_is_never_requested(monkeypatch, destination):
    requests = install_transport(monkeypatch, lambda request: httpx.Response(
        200, content=atom(entry(), f'<link rel="next" href="{escape(destination)}"/>')))
    result = await fetch(credentials={"username": "reader", "password": "private-password"})
    assert not result.complete and "unsafe_link" in result.warnings
    assert len(requests) == 1


async def test_redirect_origin_is_checked_before_authorization(monkeypatch):
    requests = install_transport(monkeypatch, lambda request: httpx.Response(
        302, headers={"location": "https://evil.example/capture"}))
    with pytest.raises(ConnectorError) as exc:
        await fetch(credentials={"username": "reader", "password": "private-password"})
    assert exc.value.code == "unsafe_redirect"
    assert len(requests) == 1
    assert "private-password" not in str(exc.value)


async def test_bounded_same_origin_redirect_and_relative_resolution(monkeypatch):
    def handler(request):
        if request.url.path == "/opds":
            return httpx.Response(301, headers={"location": "/opds/books/feed.xml"})
        return httpx.Response(200, content=atom(entry(links=f'<link rel="{opds.OPEN_ACCESS}" href="book.epub"/>')))

    requests = install_transport(monkeypatch, handler)
    result = await fetch()
    assert result.complete
    assert result.records[0].open_url == "http://books.lan/opds/books/book.epub"
    assert len(requests) == 2


@pytest.mark.parametrize("status,code", [(401, "auth_failed"), (403, "forbidden"), (429, "rate_limited"), (500, "http_error")])
async def test_error_status_has_safe_code(monkeypatch, status, code):
    install_transport(monkeypatch, lambda request: httpx.Response(status, text="private-password response"))
    with pytest.raises(ConnectorError) as exc:
        await fetch()
    assert exc.value.code == code
    assert "private-password" not in str(exc.value)


@pytest.mark.parametrize("payload", [
    b'<!DOCTYPE feed [<!ENTITY x "oops">]><feed>&x;</feed>',
    b'<!doctype feed><feed/>',
    '<!DOCTYPE feed [<!ENTITY x "oops">]><feed>&x;</feed>'.encode("utf-16"),
    b"<feed><broken>", b"<not-a-feed/>", b'{"publications":null}', b"[]",
])
async def test_hostile_or_malformed_payload_refused(monkeypatch, payload):
    install_transport(monkeypatch, lambda request: httpx.Response(200, content=payload))
    with pytest.raises(ConnectorError) as exc:
        await fetch()
    assert exc.value.code == "malformed_response"


@pytest.mark.parametrize("relation", ["http://opds-spec.org/acquisition/buy", "http://opds-spec.org/acquisition/borrow", "http://opds-spec.org/acquisition/sample", "http://opds-spec.org/acquisition/subscribe"])
async def test_purchase_borrow_sample_and_ambiguous_acquisition_are_not_open_links(monkeypatch, relation):
    install_transport(monkeypatch, lambda request: httpx.Response(
        200, content=atom(entry(links=f'<link rel="{relation}" href="/not-open.epub"/>'))))
    result = await fetch()
    assert result.records[0].open_url is None


@pytest.mark.parametrize("href", ["javascript:alert(1)", "https://u:p@public.example/book", "/book?token=secret", "/book?api_key=secret", "/devkey/secret/book", "/book?unknown=secret"])
async def test_unsafe_acquisition_links_not_renderable(monkeypatch, href):
    install_transport(monkeypatch, lambda request: httpx.Response(
        200, content=atom(entry(links=f'<link rel="{opds.OPEN_ACCESS}" href="{escape(href)}"/>'))))
    assert (await fetch()).records[0].open_url is None


async def test_cross_origin_public_acquisition_is_not_fetched(monkeypatch):
    requests = install_transport(monkeypatch, lambda request: httpx.Response(
        200, content=atom(entry(links=f'<link rel="{opds.OPEN_ACCESS}" href="https://public.example/book.epub"/>'))))
    result = await fetch(credentials={"username": "reader", "password": "private-password"})
    assert result.records[0].open_url == "https://public.example/book.epub"
    assert len(requests) == 1


async def test_cycle_preserves_records_but_is_partial(monkeypatch):
    requests = install_transport(monkeypatch, lambda request: httpx.Response(
        200, content=atom(entry(), '<link rel="next" href="/opds"/>')))
    result = await fetch()
    assert len(result.records) == 1 and not result.complete
    assert result.warnings == ["cycle"]
    assert len(requests) == 1


async def test_page_limit_is_partial(monkeypatch):
    requests = install_transport(monkeypatch, lambda request: httpx.Response(
        200, content=atom(entry(), '<link rel="next" href="?page=2"/>')))
    result = await fetch({"max_pages": 1})
    assert not result.complete and result.warnings == ["limit_reached"]
    assert len(requests) == 1


async def test_entry_limit_preserves_prefix_and_disallows_reconciliation(monkeypatch):
    install_transport(monkeypatch, lambda request: httpx.Response(200, content=atom(entry("one") + entry("two"))))
    result = await fetch({"max_entries": 1})
    assert not result.complete and [r.external_id for r in result.records] == ["one"]


async def test_streaming_response_bound_stops_before_reading_entire_body(monkeypatch):
    monkeypatch.setattr(opds, "MAX_PAGE_BYTES", 20)
    chunks = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(100):
                chunks.append(1)
                yield b"x" * 65536

    install_transport(monkeypatch, lambda request: httpx.Response(200, stream=Body()))
    with pytest.raises(ConnectorError) as exc:
        await fetch()
    assert exc.value.code == "too_large"
    assert len(chunks) < 100


async def test_total_byte_budget_applies_across_pages(monkeypatch):
    first = atom(entry(), '<link rel="next" href="?page=2"/>')
    second = atom(entry("two"))
    monkeypatch.setattr(opds, "MAX_TOTAL_BYTES", len(first) + len(second) - 1)
    install_transport(monkeypatch, lambda request: httpx.Response(200, content=second if request.url.query else first))
    result = await fetch()
    assert not result.complete and result.warnings == ["too_large"]


@pytest.mark.parametrize("failure", ["timeout", "401", "429", "malformed"])
async def test_failed_later_page_is_partial_not_missing(monkeypatch, failure):
    def handler(request):
        if not request.url.query:
            return httpx.Response(200, content=atom(entry(), '<link rel="next" href="?page=2"/>'))
        if failure == "timeout":
            raise httpx.ReadTimeout("secret-in-exception", request=request)
        if failure == "malformed":
            return httpx.Response(200, content=b"broken")
        return httpx.Response(int(failure))

    install_transport(monkeypatch, handler)
    result = await fetch()
    assert len(result.records) == 1 and not result.complete
    assert "secret-in-exception" not in json.dumps(result.warnings)


async def test_scoped_navigation_supported_but_external_and_up_not_followed(monkeypatch):
    def handler(request):
        if request.url.path == "/opds":
            return httpx.Response(200, json={"navigation": [{"href": "/opds/books", "type": "application/opds+json"}],
                                              "links": [{"rel": "up", "href": "/"}]})
        assert request.url.path == "/opds/books"
        return httpx.Response(200, json={"publications": [publication()]})

    requests = install_transport(monkeypatch, handler)
    result = await fetch()
    assert result.complete and len(result.records) == 1 and len(requests) == 2


async def test_atom_navigation_entries_are_not_imported_as_books(monkeypatch):
    def handler(request):
        if request.url.path == "/opds":
            return httpx.Response(200, content=atom(entry("nav", "Books", links=(
                '<link rel="subsection" type="application/atom+xml;profile=opds-catalog" href="/opds/books"/>'))))
        return httpx.Response(200, content=atom(entry()))

    install_transport(monkeypatch, handler)
    result = await fetch()
    assert result.complete and [r.external_id for r in result.records] == ["opaque:1"]


@pytest.mark.parametrize("href", ["/elsewhere", "https://other.example/opds", "/opds/../private"])
async def test_navigation_scope_rejects_escape(monkeypatch, href):
    requests = install_transport(monkeypatch, lambda request: httpx.Response(200, json={"navigation": [{"href": href}]}))
    result = await fetch()
    assert not result.complete and result.warnings == ["unsafe_link"]
    assert len(requests) == 1


async def test_reported_total_without_enough_entries_is_partial(monkeypatch):
    install_transport(monkeypatch, lambda request: httpx.Response(
        200, content=atom(entry(), extra="<os:totalResults>10</os:totalResults>")))
    assert (await fetch()).warnings == ["incomplete"]


async def test_missing_opaque_identity_and_duplicates_are_partial(monkeypatch):
    install_transport(monkeypatch, lambda request: httpx.Response(200, content=atom(entry("") + entry() + entry())))
    result = await fetch()
    assert not result.complete and len(result.records) == 1
    assert set(result.warnings) == {"invalid_record", "duplicate_record"}


def test_browser_prefix_mapping_is_boundary_aware():
    source = {"base_url": "http://internal/opds", "browser_url": "https://public.example/library"}
    assert browser_link("http://internal/opds/books/1", source) == "https://public.example/library/books/1"
    assert browser_link("http://internal/opds-other/1", source) == "http://internal/opds-other/1"
    assert browser_link("http://elsewhere/opds/1", source) == "http://elsewhere/opds/1"


@pytest.mark.parametrize("url", ["http://localhost:8080/opds", "http://192.168.1.4/opds", "https://[::1]:8096/root"])
def test_lan_roots_are_supported(url):
    assert validate_root_url(url) == url


@pytest.mark.parametrize("url", ["file:///books", "http://u:p@host/opds", "https://host/opds?token=x", "https://host/#x", "http://host:bad", "http://host:65536", "http:///opds"])
def test_unsafe_roots_are_refused(url):
    with pytest.raises(ConnectorError):
        validate_root_url(url)


def test_jellyfin_browser_fragment_is_allowed_without_a_token():
    assert safe_url("https://server/web/index.html#!/details?id=book-1&serverId=server-1")
    assert safe_url("https://server/web/index.html#!/details?id=book-1&api_key=secret") is None


async def test_subsection_truncated_total_is_partial(monkeypatch):
    def handler(request):
        if request.url.path == "/opds":
            return httpx.Response(200, json={"navigation": [{"href": "/opds/books"}]})
        return httpx.Response(200, json={"metadata": {"numberOfItems": 10}, "publications": [publication()]})

    install_transport(monkeypatch, handler)
    result = await fetch()
    assert len(result.records) == 1 and result.warnings == ["incomplete"]


async def test_total_is_counted_across_all_pages_of_subsection(monkeypatch):
    def handler(request):
        if request.url.path == "/opds":
            return httpx.Response(200, json={"navigation": [{"href": "/opds/books"}]})
        data = {"metadata": {"numberOfItems": 2}, "publications": [publication("two" if request.url.query else "one")]}
        if not request.url.query:
            data["links"] = [{"rel": "next", "href": "?page=2"}]
        return httpx.Response(200, json=data)

    install_transport(monkeypatch, handler)
    result = await fetch()
    assert result.complete and len(result.records) == 2


async def test_actual_credentials_cannot_be_echoed_into_pagination_query(monkeypatch):
    requests = install_transport(monkeypatch, lambda request: httpx.Response(
        200, content=atom(entry(), '<link rel="next" href="?cursor=private-password"/>')))
    result = await fetch(credentials={"username": "reader", "password": "private-password"})
    assert not result.complete and result.warnings == ["unsafe_link"]
    assert len(requests) == 1


async def test_overall_deadline_is_bounded(monkeypatch):
    monkeypatch.setattr(opds, "FETCH_SECONDS", 0)

    async def hang(request):
        await asyncio.Event().wait()

    requests = install_transport(monkeypatch, hang)
    with pytest.raises(ConnectorError, match="timeout"):
        await fetch()
    assert len(requests) <= 1


async def test_username_matching_hostname_does_not_block_authentication(monkeypatch):
    install_transport(monkeypatch, lambda request: httpx.Response(200, content=atom(entry())))
    result = await fetch(credentials={"username": "books", "password": "private-password"})
    assert result.complete and len(result.records) == 1


@pytest.mark.parametrize("raw", [
    "https://example.test/books/9780306406157", "urn:uuid:9780306406157",
    "opaque9780306406157", "book:9780306406157", "9780306406157.epub",
    "ISBN: https://example.test/9780306406157", "urn:isbn:9780306406157/trailer",
])
async def test_opaque_or_url_identifiers_are_not_isbn_evidence(monkeypatch, raw):
    install_transport(monkeypatch, lambda request: httpx.Response(200, content=atom(entry(raw))))
    record = (await fetch()).records[0]
    assert record.external_id == raw and record.isbn is None


@pytest.mark.parametrize("raw", ["9780306406157", "0-306-40615-2", "ISBN: 978-0-306-40615-7", "urn:isbn:9780306406157", "ISBN-10: 0306406152"])
def test_explicit_and_bare_legal_isbn_identifiers(raw):
    assert opds._isbn(raw) == "9780306406157"


async def test_calibre_style_generic_acquisition_and_browser_action(monkeypatch):
    payload = b'''<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/terms/">
      <id>urn:calibre:library</id><title>Calibre library</title>
      <entry><id>urn:uuid:5077e056-4e31-4b72-b168-54c14425477e</id>
        <title>Le livre de poche</title><author><name>Une autrice</name></author>
        <dc:identifier>urn:isbn:9780306406157</dc:identifier>
        <link rel="http://opds-spec.org/acquisition" type="application/epub+zip"
              href="/calibre/get/EPUB/42/Main_library"/>
        <link rel="http://opds-spec.org/image/thumbnail" href="/calibre/thumb/42"/>
      </entry>
    </feed>'''
    requests = install_transport(monkeypatch, lambda request: httpx.Response(200, content=payload))
    result = await fetch(base_url="http://books.lan/calibre", browser_url="https://public.example/library")
    assert result.complete and len(result.records) == 1
    record = result.records[0]
    assert record.open_url == "https://public.example/library/get/EPUB/42/Main_library"
    assert record.format == "application/epub+zip" and record.isbn == "9780306406157"
    assert len(requests) == 1  # No acquisition or thumbnail request.


@pytest.mark.parametrize("paid", ["buy", "borrow", "sample", "subscribe"])
@pytest.mark.parametrize("separate", [False, True])
async def test_generic_relation_cannot_launder_paid_or_ambiguous_links(monkeypatch, paid, separate):
    paid_relation = f"{opds.ACQUISITION}/{paid}"
    links = ([{"rel": opds.ACQUISITION, "href": "/book.epub"}, {"rel": paid_relation, "href": "/book.epub"}]
             if separate else [{"rel": [opds.ACQUISITION, paid_relation], "href": "/book.epub"}])
    install_transport(monkeypatch, lambda request: httpx.Response(200, json={"publications": [publication(links=links)]}))
    assert (await fetch()).records[0].open_url is None


async def test_explicit_free_link_can_coexist_with_distinct_paid_offer(monkeypatch):
    links = [{"rel": opds.ACQUISITION + "/buy", "href": "/buy"},
             {"rel": opds.OPEN_ACCESS, "href": "/public/book.epub"}]
    requests = install_transport(monkeypatch, lambda request: httpx.Response(200, json={"publications": [publication(links=links)]}))
    assert (await fetch()).records[0].open_url == "http://books.lan/public/book.epub"
    assert len(requests) == 1


async def test_generic_relation_with_price_is_not_a_free_action(monkeypatch):
    link = f'<link rel="{opds.ACQUISITION}" href="/paid"><price xmlns="http://opds-spec.org/2010/catalog" currencycode="USD">9.99</price></link>'
    install_transport(monkeypatch, lambda request: httpx.Response(200, content=atom(entry(links=link))))
    assert (await fetch()).records[0].open_url is None


@pytest.mark.parametrize("escape_mode,expected_requests", [("redirect", 2), ("pagination", 2), ("pagination_redirect", 3)])
async def test_navigation_scope_survives_redirects_and_descendant_pagination(monkeypatch, escape_mode, expected_requests):
    def handler(request):
        path = request.url.path
        assert path != "/opds/private", "A private path must never receive an authenticated request"
        if path == "/opds/public":
            return httpx.Response(200, json={"publications": [publication("A")], "navigation": [{"href": "/opds/public/books"}]})
        assert path == "/opds/public/books"
        if escape_mode == "redirect" or request.url.query:
            return httpx.Response(302, headers={"location": "/opds/private"})
        href = "/opds/private" if escape_mode == "pagination" else "?page=2"
        return httpx.Response(200, json={"publications": [publication("B")], "links": [{"rel": "next", "href": href}]})

    requests = install_transport(monkeypatch, handler)
    result = await fetch(base_url="http://books.lan/opds/public", credentials={"username": "reader", "password": "private-password"})
    assert not result.complete
    assert set(result.warnings) & {"unsafe_redirect", "unsafe_link"}
    assert len(requests) == expected_requests


async def test_initial_canonical_redirect_does_not_broaden_derived_navigation_scope(monkeypatch):
    def handler(request):
        assert request.url.path != "/opds/private"
        if request.url.path == "/opds/public":
            return httpx.Response(301, headers={"location": "/opds/public/canonical/feed.xml"})
        assert request.url.path == "/opds/public/canonical/feed.xml"
        return httpx.Response(200, json={"publications": [publication()], "navigation": [{"href": "/opds/private"}]})

    requests = install_transport(monkeypatch, handler)
    result = await fetch(base_url="http://books.lan/opds/public")
    assert not result.complete and result.warnings == ["unsafe_link"]
    assert len(requests) == 2


async def test_initial_redirect_outside_configured_scope_requires_reconfiguration(monkeypatch):
    requests = install_transport(monkeypatch, lambda request: httpx.Response(302, headers={"location": "/opds/private"}))
    with pytest.raises(ConnectorError, match="unsafe_redirect"):
        await fetch(base_url="http://books.lan/opds/public")
    assert len(requests) == 1


@pytest.mark.parametrize("payload", [
    atom(entry("A"), '<link rel="next"/>'), atom(entry("A"), '<link rel="subsection"/>'),
    {"publications": [publication("A")], "links": [{"rel": "next", "href": None}]},
    {"publications": [publication("A")], "links": [{"rel": "subsection", "href": 4}]},
])
async def test_malformed_traversal_links_are_partial_not_silently_complete(monkeypatch, payload):
    requests = install_transport(monkeypatch, lambda request: httpx.Response(
        200, **({"content": payload} if isinstance(payload, bytes) else {"json": payload})))
    result = await fetch()
    assert not result.complete and result.warnings == ["malformed_response"]
    assert len(result.records) == 1 and len(requests) == 1


@pytest.mark.parametrize("excluded", [
    {"rel": opds.ACQUISITION, "type": "application/epub+zip"},
    {"rel": opds.ACQUISITION}, {"rel": "search", "type": "application/opds+json"},
    {"rel": "http://opds-spec.org/image"}, {"type": "image/jpeg"},
    {"type": "application/epub+zip"}, {"rel": "subsection", "type": "application/pdf"},
])
async def test_json_navigation_never_downloads_acquisitions_searches_or_images(monkeypatch, excluded):
    def handler(request):
        assert request.url.path != "/opds/excluded", "Excluded navigation must never be requested"
        if request.url.path == "/opds":
            return httpx.Response(200, json={"publications": [publication("A")], "navigation": [
                {**excluded, "href": "/opds/excluded"}, {"href": "/opds/books"},
            ]})
        assert request.url.path == "/opds/books"
        return httpx.Response(200, json={"publications": [publication("B")]})

    requests = install_transport(monkeypatch, handler)
    result = await fetch(credentials={"username": "reader", "password": "private-password"})
    assert not result.complete and result.warnings == ["unsafe_link"]
    assert [r.external_id for r in result.records] == ["A", "B"]
    assert len(requests) == 2


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "zstd", "gzip, identity"])
async def test_nonidentity_encoding_is_refused_without_consuming_compressed_body(monkeypatch, encoding):
    reads, closed = [], []

    class CompressedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            reads.append(True)
            raise AssertionError("No decoder or body read is permitted for compressed responses")
            yield b"unreachable"

        async def aclose(self):
            closed.append(True)

    install_transport(monkeypatch, lambda request: httpx.Response(
        200, headers={"Content-Encoding": encoding}, stream=CompressedBody()))
    with pytest.raises(ConnectorError) as exc:
        await fetch()
    assert exc.value.code == "unsupported_encoding"
    assert reads == [] and closed == [True]


async def test_later_compressed_page_is_partial_and_never_read(monkeypatch):
    class CompressedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("Compressed page body was consumed")
            yield b"unreachable"

    def handler(request):
        if not request.url.query:
            return httpx.Response(200, content=atom(entry("A"), '<link rel="next" href="?page=2"/>'))
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=CompressedBody())

    requests = install_transport(monkeypatch, handler)
    result = await fetch()
    assert not result.complete and result.warnings == ["unsupported_encoding"]
    assert [r.external_id for r in result.records] == ["A"] and len(requests) == 2
