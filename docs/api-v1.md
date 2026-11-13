# Versioned read API

`/api/v1` is Shelf's stable, read-only integration surface. The response models,
not the HTML endpoints or internal service dictionaries, are its contract.
Fields can be added within v1; existing fields are not renamed or removed.

## Authentication and schema

Use a session cookie or `Authorization: Bearer shelf_…`. The existing
[token rules](homelab-integration.md#api-tokens) apply unchanged: tokens work
only for **GET/HEAD under `/api/v1/`**, always at viewer, even for an admin's
token. An Authorization header takes precedence over a cookie; a bad header
does not fall back to the session. Token requests never refresh a cookie.

**`GET /api/v1/schema` publishes the OpenAPI 3.1 document**, including response
schemas, query constraints and authentication alternatives. It uses the same
credential/feature checks and includes only v1 routes. The document describes
GET once; every listed route also accepts HEAD with the same checks/status and
no response body. Shelf's ordinary `/openapi.json` is not the token endpoint.

All routes require viewer and the **API tokens** feature. Missing/invalid/
revoked credentials answer 401 with `WWW-Authenticate: Bearer`, not a redirect.
Disabled features answer the existing feature-disabled 403 JSON to API clients
(browser navigation/HTMX retain their existing feature-refusal behaviour).
Unknown, hidden and trashed resources share the same 404. Invalid queries/path
IDs answer 422; unknown query keys are rejected, not silently ignored.
Responses use `Cache-Control: no-store`. Tokens retain their existing rate limit.

```bash
curl -k -H 'Authorization: Bearer shelf_…' \
  'https://shelf.lan:18888/api/v1/items?page=1&page_size=25&owned=true&sort=title'
```

## Endpoints

| GET/HEAD path | Response | Additional feature |
|---|---|---|
| `/stats` | Existing dashboard document, unchanged | None; disabled optional sections are null |
| `/items` | `ItemPage` | None |
| `/items/{item_id}` | `ItemDetailResponse` | None; optional sections below |
| `/loans` | `LoanPage` | Lending |
| `/loans/{loan_id}` | `Loan` | Lending |
| `/wishlist` | `ItemPage`, alias of `/lists/wishlist/items` | None |
| `/lists` | `ListPage` | None |
| `/lists/{slug}` | `ListSummary` | None |
| `/lists/{slug}/items` | `ItemPage` | None |
| `/series` | `SeriesPage` | Series |
| `/series/{name}` | `SeriesDetailResponse` with an `items` page | Series |
| `/upcoming` | `UpcomingPage` | Release radar **and** Hardcover |
| `/schema` | OpenAPI document | None |

Paths in the table are relative to `/api/v1`. Encode series names as URL path
components; names containing `/` are supported. List slugs are exact,
case-sensitive identifiers; series identity uses SQLite's ASCII `NOCASE`,
matching stored series metadata. Case variants use the binary-smallest visible
spelling as the deterministic display name. Only series with visible items
are listed; orphaned metadata cannot create a series.

### Pagination and sorting

Every page has exactly this envelope (data objects vary by endpoint):

```json
{"data": [], "total": 0, "page": 1, "page_size": 50, "pages": 0, "next_page": null}
```

`page` is one-based, 1–10,000. `page_size` defaults to 50, range 1–100. Totals
are after visibility/filtering and before pagination. Pages beyond the result
return empty data without clamping the requested page. `next_page` is null on
the last page or at the page bound; `pages` still reports the actual total.
Use narrower filters if a result exceeds the paging bound.

Ordered endpoints accept `order=asc|desc` (default asc). Every ordering ends in
a unique ID (or unique list slug/series name) tie-breaker; null sort values
always come last. Pages and counts share one SQLite read snapshot **within a
request**. Offset pagination is not a durable cross-request snapshot: concurrent
adds/edits/removals can shift later pages. No endpoint hydrates the complete
catalogue to apply paging, and reads make no remote requests.

| Endpoint | Filters | Sort keys (first is default) |
|---|---|---|
| Items, wishlist, list items | `q`, `media_type`, `owned`, `wishlisted` | `title`, `authors`, `created_at`, `id` |
| Loans | `status=open|returned|overdue|all`, `item_id`, `borrower_id` | `checked_out`, `due_date`, `title`, `id` |
| Lists | `q` on name | `name`, `created_at`, `item_count` |
| Series index | `q` on name | `name`, `item_count`, `owned_count`, `wishlist_count` |
| Series detail's items | None | `position`, `title`, `id` |
| Upcoming | `series_name`, `dated`, `release_from`, `release_to` | `release_date`, `title`, `series_name` |

`q` is a literal substring, maximum 200 characters, using SQLite LIKE's ASCII
case handling. `%`, `_`, backslash and SQL-looking text are literal input, not
wildcards or SQL. Item `q` matches title, creators, ISBN, ISBN-10, UPC or ASIN.
Media types use stored values (`ebook`, `vinyl`, `digital_video`, etc.), not UI
labels. `owned=false` does **not** mean wishlisted: wishlist membership is an
independent state. Loan `status` defaults to open; overdue uses the same due-date
and configured no-due-date fallback rule as the existing dashboard.
Catalogue/loan timestamps retain their stored SQLite UTC or imported ISO string
representation; date fields are ISO date strings, nullable where unspecified.

Upcoming bounds are inclusive ISO dates, require `release_from <= release_to`
and exclude undated records when present. Past releases remain until normal
radar pruning. Dismissed or already-owned/wishlisted releases are excluded;
title matching follows the radar's casefolded/trimmed rule. Watches with no
visible local series item are excluded, including orphaned watches. This last
rule is intentionally stricter than the legacy stats/Upcoming-page orphan-watch
behaviour; it prevents the new feed from enumerating hidden collections.

### Detail sections and privacy

An item response contains `item`, `value` and `sources`; keys are never omitted.
`value` is null with Valuation off, otherwise `{amount, currency}`, with manual
value preferred over the estimate, and an unpriced amount null. `sources` is
null with Connected library off, otherwise a page of cached digital holdings
from new sources and legacy Audiobookshelf/Komga/RomM. On item detail,
`page`/`page_size` page **only the sources section**, ordered by source name,
source key and record ID. Integration-specific flags exclude those holdings.
Disabled/failed sources retain records but have unknown state (an explicit
unavailable state stays unavailable); disabled sources have no open URL.

An item's `series` is null when Series is off or unassigned. A dedicated
disabled-feature route returns 403, rather than an empty collection. The
existing stats document keeps its published null-section semantics.

All item reads and dependent counts/memberships/loans use `items_live`; there
is no alternate unscoped catalogue query. Lists expose visible-member counts;
non-wishlist lists with no visible members are not enumerated or addressable.
The seeded wishlist remains visible when empty.

The API deliberately omits shared free-text item/copy/loan notes, shared reading
status/logs, all accounts' personal state/history/journals, saved views, gift
claims, encrypted credentials, raw settings, provider payloads, provenance and
source errors. It is not an archive/export or source-configuration API. Loan
responses include borrower ID/name but no free-text notes.

Source URLs are validated HTTP(S) browser links, never userinfo or recognized
credential-bearing query/fragment URLs. Relative links resolve only against a
safe configured root; unsafe links become null. LAN/loopback sources remain
valid for self-hosting. Legacy IDs are path-encoded. Source roots, raw external-ID fields
and credentials are not returned. Cover URLs are validated local `/covers/`
filenames; unsafe stored paths become null. Upcoming provider covers receive
the same credential/scheme checks. A returned URL is a launcher/reference, not
an access grant or evidence that a remote server is reachable. It does not
extend Shelf tokens to non-v1 routes or remote services.
