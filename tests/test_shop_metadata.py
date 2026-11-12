"""Shop metadata is inert, bounded and constrained to public pinned HTTPS."""

import ipaddress
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services import manual_cover, shop_metadata

URL = "https://records.example.test/album"


def _page(value):
    return '<script type="application/ld+json">' + json.dumps(value) + '</script>'


def test_product_album_metadata_images_and_tracks_are_extracted_not_executed():
    page = _page({"@context": "https://schema.org", "@type": ["Product", "MusicAlbum"], "name": "Album",
                  "byArtist": {"name": "An Artist"}, "brand": {"name": "A Label"}, "datePublished": "2026-10-01",
                  "gtin13": "0012345678905", "mpn": "CAT-123", "description": "<b>A record</b> &amp; notes",
                  "image": ["/front.jpg", {"url": "https://images.example.test/back.jpg"}, "http://private.test/x", "javascript:alert(1)"],
                  "track": {"itemListElement": [{"item": {"name": "Track A", "duration": "PT4M10S"}}]},
                  "offers": {"price": "1000", "priceCurrency": "USD"}})
    candidate = shop_metadata.parse(page, URL)[0]
    assert candidate["fields"] == {"title": "Album", "authors": "An Artist", "publisher": "A Label", "publish_year": 2026,
                                    "upc": "0012345678905", "description": "A record & notes"}
    assert candidate["images"] == ["https://records.example.test/front.jpg", "https://images.example.test/back.jpg"]
    assert candidate["release"]["catalog_number"] == "CAT-123"
    assert candidate["release"]["media"][0]["tracks"][0]["duration_ms"] == 250000
    assert "manual_value" not in candidate["fields"]


def test_multiple_products_are_not_silently_merged_or_matched():
    page = _page({"@graph": [{"@type": "Product", "name": "Blue pressing", "image": "/blue.jpg"},
                            {"@type": "Product", "name": "Red pressing", "image": "/red.jpg"}]})
    result = shop_metadata.parse(page, URL)
    assert len(result) == 2
    assert result[0]["images"] != result[1]["images"]


def test_same_title_variants_remain_separate_and_malformed_script_is_inert():
    text = '<script type>do_not_execute()</script>' + _page({"@type": "ProductGroup", "hasVariant": [
        {"@type": "Product", "name": "Album", "mpn": "CAT-BLUE", "image": "/blue.jpg"},
        {"@type": "Product", "name": "Album", "mpn": "CAT-RED", "image": "/red.jpg"},
    ]})
    candidates = shop_metadata.parse(text, URL)
    assert [candidate["release"]["catalog_number"] for candidate in candidates] == ["CAT-BLUE", "CAT-RED"]


def test_opengraph_fallback_is_never_mislabeled_musicbrainz():
    candidate = shop_metadata.parse('<meta property="og:title" content="Shop album">', URL)[0]
    assert candidate["release"]["source"] == "shop"


def test_bandcamp_style_published_dates_are_retained():
    candidate = shop_metadata.parse(_page({'@type': 'MusicAlbum', 'name': 'Album', 'datePublished': '29 Apr 2016 00:00:00 GMT'}), URL)[0]
    assert candidate['fields']['publish_year'] == 2016
    assert candidate['release']['release_date'] == '2016-04-29'


def test_opengraph_is_a_fallback_and_no_page_scripts_or_iframes_are_used():
    result = shop_metadata.parse('<meta property="og:title" content="A &amp; B"><meta property="og:image" content="/cover.jpg">'
                                 '<script>run_remote_code()</script><iframe src="/account"></iframe>', URL)
    assert result[0]["fields"] == {"title": "A & B"}
    assert result[0]["images"] == ["https://records.example.test/cover.jpg"]


@pytest.mark.parametrize("url", ["http://shop.test/x", "file:///etc/passwd", "javascript:alert(1)",
    "https://user:secret@shop.test/x", "https://shop.test:8443/x", "https://shop.test/x?token=secret",
    "https://shop.test/x?X-Amz-Signature=secret", "https://shop.test/\nsecret", "https://shop.test\\@private/x"])
def test_unsafe_shop_links_are_rejected_syntactically(url):
    assert shop_metadata.public_url(url, page=True) is None


def test_variant_selectors_are_kept_but_tracking_is_not_sent():
    assert shop_metadata.public_url(URL + "?variant=123&utm_campaign=sale#details", page=True) == URL + "?variant=123"


@pytest.mark.asyncio
@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["::1"], ["169.254.169.254"], ["93.184.216.34", "10.0.0.1"]])
async def test_private_and_mixed_dns_answers_never_open_a_connection(monkeypatch, addresses):
    monkeypatch.setattr(manual_cover, "_resolve_addresses", AsyncMock(return_value=[ipaddress.ip_address(a) for a in addresses]))
    def forbidden(*args, **kwargs):
        raise AssertionError("Private target reached the transport")
    monkeypatch.setattr(manual_cover, "_PinnedIPTransport", forbidden)
    with pytest.raises(shop_metadata.ShopError):
        await shop_metadata.preview(URL)


class _Body(httpx.AsyncByteStream):
    def __init__(self, content):
        self.content = content

    async def __aiter__(self):
        yield self.content


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["ok", "too_large", "compressed", "non_html", "private_redirect", "redirect_loop"])
async def test_fetch_is_pinned_bounded_and_revalidates_redirects(monkeypatch, case):
    opened = []
    async def target(url):
        return None if "127.0.0.1" in url else ("records.example.test", "93.184.216.34")
    monkeypatch.setattr(manual_cover, "_public_target", target)
    monkeypatch.setattr(shop_metadata.outbound, "acquire", AsyncMock())
    def handler(request):
        opened.append(str(request.url))
        assert "cookie" not in request.headers and "authorization" not in request.headers
        if case in {"private_redirect", "redirect_loop"}:
            return httpx.Response(302, headers={"Location": "https://127.0.0.1/secret" if case == "private_redirect" else URL})
        content = _page({"@type": "Product", "name": "Album"}).encode()
        if case == "too_large":
            content = b"x" * (shop_metadata.MAX_PAGE_BYTES + 1)
        headers = {"content-type": "audio/mpeg" if case == "non_html" else "text/html"}
        if case == "compressed":
            headers["content-encoding"] = "gzip"
        return httpx.Response(200, headers=headers, stream=_Body(content))
    monkeypatch.setattr(manual_cover, "_PinnedIPTransport", lambda address: httpx.MockTransport(handler))
    if case == "ok":
        assert (await shop_metadata.preview(URL))[0]["fields"]["title"] == "Album"
    else:
        with pytest.raises(shop_metadata.ShopError):
            await shop_metadata.preview(URL)
    assert len(opened) == (6 if case == "redirect_loop" else 1)
