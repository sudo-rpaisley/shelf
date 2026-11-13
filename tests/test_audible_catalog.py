"""Offline public-catalogue contract, exact identity and outbound safety pins."""

import ipaddress
import json
import struct
import zlib
from pathlib import Path

import httpx
import pytest

from app import config
from app.services import audible_catalog as catalog, covers, manual_cover, outbound


ASIN = "B082BHJMFF"
IMAGE = "https://m.media-amazon.com/images/I/414J3xG+7+L._SL500_.jpg"
# Selected public fields observed from ONE US catalogue response, 2026-11-13.
# This is a shape fixture, not a promise of coverage in any other marketplace.
PRODUCT = {"asin": ASIN, "title": "The Martian", "authors": [{"asin": "B00G0WYW92", "name": "Andy Weir"}],
           "narrators": [{"name": "Wil Wheaton"}], "content_type": "Product", "content_delivery_type": "SinglePartBook",
           "runtime_length_min": 659, "format_type": "unabridged", "language": "english", "release_date": "2020-01-01",
           "publisher_name": "Audible Studios", "product_images": {"500": IMAGE}}


def _png_fixture():
    """A real 8x8 RGB PNG, large enough for the shared cover size threshold."""
    def chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
    pixels = b"".join(b"\0" + bytes(channel for x in range(8) for channel in (x * 30, y * 30, (x + y) * 15)) for y in range(8))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!2I5B", 8, 8, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


PNG = _png_fixture()


def _decode_png(content):
    """Decode this fixture's actual pixels and verify all PNG chunk CRCs."""
    assert content[:8] == b"\x89PNG\r\n\x1a\n"
    offset, compressed, kinds = 8, bytearray(), []
    while offset < len(content):
        length = struct.unpack("!I", content[offset:offset + 4])[0]
        kind = content[offset + 4:offset + 8]
        data = content[offset + 8:offset + 8 + length]
        crc = struct.unpack("!I", content[offset + 8 + length:offset + 12 + length])[0]
        assert crc == zlib.crc32(kind + data)
        kinds.append(kind)
        if kind == b"IHDR":
            assert struct.unpack("!2I5B", data) == (8, 8, 8, 2, 0, 0, 0)
        if kind == b"IDAT":
            compressed.extend(data)
        offset += length + 12
    assert offset == len(content) and kinds == [b"IHDR", b"IDAT", b"IEND"]
    return zlib.decompress(compressed)


class Chunks(httpx.AsyncByteStream):
    def __init__(self, *chunks):
        self.chunks, self.read = chunks, 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.read += 1
            yield chunk


def reply(status=200, *, payload=None, content=None, headers=None, stream=None):
    if content is None:
        content = json.dumps(payload if payload is not None else {"products": [PRODUCT]}).encode()
    return httpx.Response(status, headers={"content-type": "application/json", **(headers or {})},
                          stream=stream or Chunks(content))


@pytest.fixture
def network(monkeypatch):
    calls, targets, queue = [], [], []

    async def public(url):
        targets.append(url)
        return httpx.URL(url).host, "8.8.8.8"

    def handler(request):
        calls.append(request)
        if not queue:
            raise AssertionError("Unexpected external request")
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(manual_cover, "_public_target", public)
    monkeypatch.setattr(manual_cover, "_PinnedIPTransport", lambda address: httpx.MockTransport(handler))
    return calls, targets, queue


def test_maps_real_public_shape_and_ignores_audio_account_and_arbitrary_urls():
    product = {**PRODUCT, "asset_details": {"url": "https://audio.example/never-fetch.aax"},
               "pdf_url": "https://account.example/token", "url": "https://evil.example/?password=leak"}
    candidate = catalog.normalise({"products": [product]}, ASIN.lower(), "us")
    assert candidate["fields"] == {"title": "The Martian", "authors": "Andy Weir", "narrator": "Wil Wheaton",
                                   "duration_mins": 659, "language": "en", "publish_year": 2020, "publisher": "Audible Studios"}
    assert candidate["edition"] == {"format": "unabridged", "release_date": "2020-01-01"}
    assert candidate["images"] == [IMAGE]
    assert candidate["source_url"] == f"https://www.audible.com/pd/{ASIN}"
    assert "audio.example" not in json.dumps(candidate) and "account.example" not in json.dumps(candidate)


def test_exact_asin_is_selected_not_first_or_related_title():
    unrelated = {**PRODUCT, "asin": "B000000000", "title": "Wrong edition", "narrators": [{"name": "Wrong narrator"}]}
    result = catalog.normalise({"products": [unrelated, PRODUCT]}, ASIN, "us")
    assert result["fields"]["narrator"] == "Wil Wheaton" and result["asin"] == ASIN
    assert catalog.normalise({"products": [unrelated]}, ASIN, "us") is None
    assert catalog.normalise({"products": [PRODUCT, PRODUCT]}, ASIN, "us") is None


@pytest.mark.parametrize("changes", [{"asin": "bad"}, {"asin": None}, {"title": ""}, {"content_type": "Episode"},
                                     {"content_delivery_type": "Podcast"}, {"content_delivery_type": {}}, {}])
def test_sparse_or_non_book_answers_are_not_a_match(changes):
    product = {**PRODUCT, **changes} if changes else {"asin": ASIN, "asset_details": {}}
    assert catalog.normalise({"products": [product]}, ASIN, "us") is None


def test_text_names_numbers_and_series_are_bounded_normalised_and_plain():
    p = {**PRODUCT, "title": "<b>Cafe\u0301</b>\x00\x98", "subtitle": "A subtitle", "publisher_summary": "<p>A story</p><script>evil()</script>",
         "authors": [{"name": "First"}, {"name": "Second"}, {"name": "First"}, {"name": {}}],
         "narrators": "not a list", "runtime_length_min": True, "language": "german",
         "series": [{"title": "Series", "sequence": "1-3"}], "publication_datetime": "1888-01-01"}
    candidate = catalog.normalise({"products": [p]}, ASIN, "de")
    fields = candidate["fields"]
    assert fields["title"] == "Café" and fields["authors"] == "First, Second" and fields["description"] == "A story"
    assert fields["series_name"] == "Series" and fields["language"] == "de" and fields["publish_year"] == 2020
    assert not {"duration_mins", "narrator", "series_position"} & fields.keys()
    p["series"][0]["sequence"] = "2.5"
    assert catalog.normalise({"products": [p]}, ASIN, "de")["fields"]["series_position"] == 2.5


@pytest.mark.parametrize("value", [None, "", "B0D-123456", "B082BHJMFF\n", "http://127.0.0.1", [ASIN], "Ｂ082BHJMFF"])
@pytest.mark.asyncio
async def test_bad_asin_never_requests(value, network):
    with pytest.raises(ValueError):
        await catalog.lookup(value, "us")
    assert not network[0] and not network[1]


@pytest.mark.parametrize("market", ["", "US", "com", "br", "https://api.audible.com", "us@127.0.0.1", "us\n", None])
@pytest.mark.asyncio
async def test_marketplace_is_an_exact_allowlisted_enum(market, network):
    with pytest.raises(ValueError):
        await catalog.lookup(ASIN, market)
    assert not network[0] and not network[1]


@pytest.mark.parametrize("market", tuple(catalog.MARKETPLACES))
@pytest.mark.asyncio
async def test_fixed_marketplace_endpoint_pacing_and_credential_free_contract(market, network, monkeypatch):
    calls, targets, queue = network
    queue.append(reply())
    paced = []

    async def acquire(host):
        paced.append(host)
    monkeypatch.setattr(outbound, "acquire", acquire)
    result = await catalog.lookup(ASIN.lower(), market)
    host = f"api.{catalog.MARKETPLACES[market][1]}"
    assert result.found and result.payload["marketplace"] == market
    assert len(calls) == len(targets) == 1 and calls[0].url.host == host
    assert paced == [host] and config.HOST_RATE_LIMITS[host] >= 1
    assert dict(calls[0].url.params) == {"asins": ASIN, "num_results": "1", "image_sizes": "500",
                                      "response_groups": "contributors,media,product_attrs,product_desc,series"}
    assert "authorization" not in calls[0].headers and "cookie" not in calls[0].headers
    assert calls[0].headers["accept-encoding"] == "identity"


@pytest.mark.parametrize("status,outcome", [(404, "no_match"), (410, "no_match"), (403, "rejected"), (401, "rejected"),
                                            (429, "rate_limited"), (500, "transport_failed"), (503, "transport_failed"), (302, "transport_failed")])
@pytest.mark.asyncio
async def test_failure_outcomes_do_not_retry_or_follow_redirects(status, outcome, network):
    network[2].append(reply(status, headers={"location": "https://127.0.0.1/private", "retry-after": "999"}))
    result = await catalog.lookup(ASIN, "us")
    assert result.outcome == outcome and len(network[0]) == 1 and not network[2]


@pytest.mark.parametrize("response", [
    lambda: reply(content=b"<html>challenge</html>", headers={"content-type": "text/html"}),
    lambda: reply(content=b"not json"),
    lambda: reply(content=b'{"products": [], "products": []}'),
    lambda: reply(content=b'{"products": [], "x": NaN}'),
    lambda: reply(headers={"content-encoding": "gzip"}),
    lambda: reply(headers={"content-length": str(catalog.MAX_RESPONSE_BYTES + 1)}),
    lambda: reply(content=b"x" * (catalog.MAX_RESPONSE_BYTES + 1)),
    lambda: httpx.ReadTimeout("timeout"),
    lambda: httpx.ConnectError("no connection"),
])
@pytest.mark.asyncio
async def test_invalid_compressed_oversized_and_transport_answers_fail_closed(response, network):
    network[2].append(response())
    assert (await catalog.lookup(ASIN, "us")).outcome == "transport_failed"
    assert len(network[0]) == 1


@pytest.mark.parametrize("url", [
    "http://m.media-amazon.com/images/I/a.jpg", "https://127.0.0.1/images/I/a.jpg", "file:///images/I/a.jpg",
    "https://m.media-amazon.com.evil.test/images/I/a.jpg", "https://evil@m.media-amazon.com/images/I/a.jpg",
    "https://m.media-amazon.com:444/images/I/a.jpg", "https://m.media-amazon.com/images/I/a.jpg?token=secret",
    "https://m.media-amazon.com/images/I/a.jpg#secret", "https://m.media-amazon.com/images/I/a.svg",
    "https://m.media-amazon.com/audio/a.aax", "https://m.media-amazon.com/images/I/../a.jpg",
    "https://m.media-amazon.com/images/I/%2e%2e%2fa.jpg", "https://m.media-amazon.com/images/I/a.jpg\n",
    "https://m.media-amazon.com\\@evil.test/images/I/a.jpg", "//m.media-amazon.com/images/I/a.jpg",
    "https://images-na.ssl-images-amazon.com/devkey/secret/a.jpg", None, [],
])
@pytest.mark.asyncio
async def test_attacker_artwork_urls_are_dropped_before_dns_or_http(url, network):
    assert catalog.artwork_url(url) is None
    assert await catalog.download_artwork(1, url) is None
    assert not network[0] and not network[1]
    candidate = catalog.normalise({"products": [{**PRODUCT, "product_images": {"500": url}}]}, ASIN, "us")
    assert candidate["images"] == []


@pytest.mark.asyncio
async def test_every_artwork_redirect_is_checked_before_next_request(network):
    calls, targets, queue = network
    queue.append(reply(302, headers={"location": "https://127.0.0.1/private.jpg"}))
    assert await catalog.download_artwork(5, IMAGE) is None
    assert len(calls) == len(targets) == 1
    queue.append(reply(302, headers={"location": "https://public.example/images/I/a.jpg"}))
    assert await catalog.download_artwork(5, IMAGE) is None
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_safe_redirect_and_shared_image_validation_save_versioned_artwork(network):
    network[2].extend([reply(302, headers={"location": "https://images-na.ssl-images-amazon.com/images/I/a.jpg"}),
                       reply(content=PNG, headers={"content-type": "image/png"})])
    path = await catalog.download_artwork(5, IMAGE)
    assert path.startswith("covers/5-") and path.endswith(".jpg")
    assert (config.DATA_DIR / path).read_bytes() == PNG
    pixels = _decode_png((config.DATA_DIR / path).read_bytes())
    assert len(pixels) == 8 * (1 + 8 * 3) and pixels[:4] == b"\0\0\0\0"


@pytest.mark.parametrize("failure", ["partial_raise", "partial_short", "rename", "rename_after_install"])
@pytest.mark.asyncio
async def test_artwork_write_failures_leave_no_file_or_backfill_suppression(db, network, monkeypatch, failure):
    from app.services import audiobook_review, item_write
    item_id = item_write.insert_item(db, {"title": "Sparse audiobook", "media_type": "audiobook", "asin": ASIN})
    db.commit()
    before = audiobook_review.fingerprint(db, item_id)
    assert audiobook_review.backfill_artwork_allowed(db, item_id)
    network[2].append(reply(content=PNG, headers={"content-type": "image/png"}))
    write, replace = Path.write_bytes, Path.replace

    def partial(path, content):
        written = write(path, content[:32])
        if failure == "partial_raise":
            raise OSError("Simulated disk-full after a partial write")
        return written

    def failed_rename(path, target):
        if failure == "rename_after_install":
            replace(path, target)
        raise OSError("Simulated installation failure")

    if failure.startswith("partial"):
        monkeypatch.setattr(Path, "write_bytes", partial)
    else:
        monkeypatch.setattr(Path, "replace", failed_rename)
    assert await catalog.download_artwork(item_id, IMAGE) is None
    assert list(config.COVERS_DIR.iterdir()) == []
    assert audiobook_review.fingerprint(db, item_id) == before
    assert audiobook_review.backfill_artwork_allowed(db, item_id)


@pytest.mark.parametrize("content,mime,encoding", [(b"<svg>" + b"x" * 200, "image/svg+xml", "identity"),
    (b"<html>" + b"x" * 200, "image/jpeg", "identity"), (b"RIFF0000WAVE" + b"x" * 200, "image/webp", "identity"),
    (b"\xff\xd8\xff", "image/jpeg", "identity"), (PNG, "text/html", "identity"), (PNG, "image/png", "gzip")])
@pytest.mark.asyncio
async def test_invalid_images_cannot_be_saved(content, mime, encoding, network):
    network[2].append(reply(content=content, headers={"content-type": mime, "content-encoding": encoding}))
    assert await catalog.download_artwork(5, IMAGE) is None
    assert list(config.COVERS_DIR.iterdir()) == []


@pytest.mark.parametrize("image", [False, True])
@pytest.mark.asyncio
async def test_streaming_limit_stops_before_unbounded_tail(image, network, monkeypatch):
    stream = Chunks(b"x" * 300, b"tail must not be read")
    monkeypatch.setattr(catalog, "MAX_RESPONSE_BYTES", 256)
    monkeypatch.setattr(covers, "MAX_COVER_SIZE", 256)
    network[2].append(reply(stream=stream, headers={"content-type": "image/png" if image else "application/json"}))
    result = await catalog.download_artwork(5, IMAGE) if image else await catalog.lookup(ASIN, "us")
    assert result is None if image else result.outcome == "transport_failed"
    assert stream.read == 1 and list(config.COVERS_DIR.iterdir()) == []


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["10.0.0.1"], ["::1"], ["8.8.8.8", "192.168.1.2"]])
@pytest.mark.asyncio
async def test_shared_public_dns_boundary_rejects_private_and_mixed_answers(addresses, monkeypatch):
    async def resolve(*args):
        return [ipaddress.ip_address(address) for address in addresses]
    monkeypatch.setattr(manual_cover, "_resolve_addresses", resolve)
    monkeypatch.setattr(manual_cover, "_PinnedIPTransport", lambda *args: pytest.fail("Private target connected"))
    assert (await catalog.lookup(ASIN, "us")).outcome == "transport_failed"
    assert await catalog.download_artwork(5, IMAGE) is None


@pytest.mark.asyncio
async def test_artwork_redirects_are_bounded(network):
    network[2].extend(reply(302, headers={"location": IMAGE}) for _ in range(4))
    assert await catalog.download_artwork(5, IMAGE) is None and len(network[0]) == 4
