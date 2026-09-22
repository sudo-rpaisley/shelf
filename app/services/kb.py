"""Koninklijke Bibliotheek (KB) Nederlandse Bibliografie Totaal client.

Queries the Dutch National Bibliography (NBT) linked-data SPARQL endpoint for
Dutch ISBNs. Older NBT records may contain only their ISBN-10, so both the
ISBN-13 and, where available, its ISBN-10 equivalent are queried.

Failures are non-fatal: a missing record, malformed response or HTTP failure
returns a ProviderResult outcome so Shelf's normal metadata cascade can
continue.
"""

import logging

import httpx

from app.services import bib_normalize, outbound, provider_result
from app.services.isbn import canonical_isbn_pair

logger = logging.getLogger(__name__)

SPARQL_URL = "https://data.bibliotheken.nl/sparql"


async def _rate_limit():
    await outbound.acquire("data.bibliotheken.nl")


def _query(isbn13: str) -> str:
    pair = canonical_isbn_pair(isbn13)
    values = [isbn13]

    if pair is not None and pair[1]:
        values.append(pair[1])

    isbn_values = " ".join(f'"{isbn}"' for isbn in values)

    return f"""
PREFIX schema: <http://schema.org/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX dct: <http://purl.org/dc/terms#>

SELECT ?book ?title ?author ?year ?language ?pages ?publication ?isbn
WHERE {{
  VALUES ?isbn {{ {isbn_values} }}

  ?book schema:isbn ?isbn ;
        schema:name ?title .

  OPTIONAL {{
    ?book schema:author ?authorId .
    ?authorId rdfs:label ?author .
  }}
  OPTIONAL {{ ?book dct:issued ?year . }}
  OPTIONAL {{ ?book schema:inLanguage ?language . }}
  OPTIONAL {{ ?book schema:numberOfPages ?pages . }}
  OPTIONAL {{
    ?book schema:publication ?publicationId .
    ?publicationId rdfs:label ?publication .
  }}
}}
"""


def _parse_binding(binding: dict) -> dict | None:
    def value(key: str) -> str:
        return (binding.get(key) or {}).get("value", "")

    title = bib_normalize.nfc(value("title"))
    if not title:
        return None

    result: dict = {"title": title}

    author = bib_normalize.invert_name(value("author"))
    if author:
        result["authors"] = author

    publisher, publication_year = bib_normalize.split_publication(
        value("publication")
    )
    if publisher:
        result["publisher"] = publisher

    year = bib_normalize.first_year(value("year"))
    if year is None:
        year = publication_year
    if year is not None:
        result["publish_year"] = year

    language = bib_normalize.to_iso639_1(value("language"))
    if language:
        result["language"] = language

    pages = bib_normalize.leading_int(value("pages"))
    if pages is not None:
        result["page_count"] = pages

    pair = canonical_isbn_pair(value("isbn"))
    if pair is not None and pair[1]:
        result["isbn10"] = pair[1]

    return result


async def lookup(
    isbn13: str, client: httpx.AsyncClient
) -> provider_result.ProviderResult:
    """Look up a Dutch book by ISBN-13 in the KB NBT.

    Never raises. A usable first result returns ``found``; an empty or
    malformed result falls through as ``no_match``. Transport and rate-limit
    outcomes use the same ProviderResult semantics as the other national
    providers.
    """
    await _rate_limit()

    try:
        resp = await client.get(
            SPARQL_URL,
            params={"query": _query(isbn13)},
            headers={
                "Accept": "application/sparql-results+json",
                "User-Agent": "Shelf/1.0 (home library catalog)",
            },
        )
    except httpx.HTTPError as exc:
        logger.debug("KB lookup failed for ISBN %s: %s", isbn13, exc)
        return provider_result.transport_failed("kb")

    classified = provider_result.classify_response("kb", resp)
    if classified is not None:
        logger.debug(
            "KB lookup failed for ISBN %s: HTTP %d",
            isbn13,
            resp.status_code,
        )
        return classified

    try:
        payload = resp.json()
        bindings = payload.get("results", {}).get("bindings", [])

        for binding in bindings:
            parsed = _parse_binding(binding)
            if parsed:
                return provider_result.found(
                    "kb", parsed, status=resp.status_code
                )

        return provider_result.no_match("kb", status=resp.status_code)
    except Exception:
        logger.debug(
            "KB lookup: malformed response for ISBN %s",
            isbn13,
            exc_info=True,
        )
        return provider_result.no_match("kb", status=resp.status_code)
