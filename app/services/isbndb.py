"""ISBNdb API client for book price lookups. Ported from tools/valuate.py."""

import json
import time

import httpx

from app.config import DATA_DIR
from app.services import outbound

ISBNDB_API_URL = "https://api2.isbndb.com/book/{isbn}"
CACHE_FILE = DATA_DIR / ".isbn_price_cache.json"
CACHE_MAX_AGE_DAYS = 365
_CACHEABLE_STATUSES = (200, 404)


class LookupFailed(Exception):
    """ISBNdb gave no answer about the book. `status` is the HTTP status, or
    None when no response arrived (timeout, connection error)."""

    def __init__(self, status: int | None):
        super().__init__(f"ISBNdb lookup failed ({status if status is not None else 'no response'})")
        self.status = status


def _load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        raw = json.loads(CACHE_FILE.read_text())
        migrated = {}
        for isbn, value in raw.items():
            if isinstance(value, dict) and "fetched_at" in value:
                migrated[isbn] = value
            else:
                migrated[isbn] = {"data": value, "fetched_at": 0}
        return migrated
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_FILE.write_text(json.dumps(cache, indent=2))


def _cache_is_fresh(entry: dict) -> bool:
    age_days = (time.time() - entry.get("fetched_at", 0)) / 86400
    return age_days < CACHE_MAX_AGE_DAYS


def parse_price(data: dict | None) -> float | None:
    """Extract a float price from ISBNdb response, trying msrp then list_price."""
    if not data:
        return None
    for field in ("msrp", "list_price"):
        raw = data.get(field)
        if raw:
            try:
                return float(str(raw).replace("$", "").strip())
            except ValueError:
                continue
    return None


async def lookup_price(isbn13: str, api_key: str, client: httpx.AsyncClient, cache: dict,
                       *, use_cache: bool = True, raise_on_failure: bool = False) -> dict | None:
    """Look up price for an ISBN. Returns {title, author, msrp, list_price} or None.

    `use_cache=False` skips the cache read and always asks ISBNdb, but still
    writes a fresh answer into `cache`. The price-alert pass needs a current
    price, not the year-old one valuation is happy with. A failed request
    (anything but 200 or 404) leaves `cache` untouched, and returns None
    unless `raise_on_failure`, which raises `LookupFailed` instead so a caller
    can tell "no price" from "no answer".
    """
    if use_cache:
        entry = cache.get(isbn13)
        if entry and _cache_is_fresh(entry):
            return entry["data"]

    status = None
    try:
        await outbound.acquire("api2.isbndb.com")
        resp = await client.get(
            ISBNDB_API_URL.format(isbn=isbn13),
            headers={"Authorization": api_key},
            timeout=10,
        )
        status = resp.status_code
        if status == 200:
            book = resp.json().get("book", {})
            data = {
                "title": book.get("title", ""),
                "author": ", ".join(book.get("authors", [])),
                "msrp": book.get("msrp"),
                "list_price": book.get("list_price"),
            }
        else:
            data = None
    except Exception:
        status = None
        data = None

    # Cache only an answer: a book (200) or ISBNdb saying it has none (404).
    # A refused key, a rate limit, a 5xx or a timeout says nothing about the
    # book, and an entry written for it would be trusted for CACHE_MAX_AGE_DAYS
    # — overwriting a good price the valuation report had cached.
    if status in _CACHEABLE_STATUSES:
        cache[isbn13] = {"data": data, "fetched_at": time.time()}
    elif raise_on_failure:
        raise LookupFailed(status)
    return data
