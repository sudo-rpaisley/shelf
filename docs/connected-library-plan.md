# Connected library experiment

This work lives on the experimental `new-test` branch, starting at
`100219d` (the current Shelf baseline plus the Related Media refresh fix).
It is not a release announcement or an upstream design commitment.

## Goal

Make Shelf a single front door to a mixed physical and digital library:
find an item, see its editions and availability, and open it in the service
that actually holds it. Keep the existing catalogue, copies, integrations,
and administrative Browse usable throughout.

## Implementation areas

The following code is present on this branch; “implemented” here does not mean
released or universally compatible. The canonical setup/behaviour reference is the
[connected-library user guide](user-guide/connected-library.md).

| Area | Implemented evidence | Limits |
|---|---|---|
| Unified Library | `routers/library.py`, `services/library_catalog.py`, `services/format_groups.py`, `templates/library.html` | Format-only stacks, same-member filters, same-holding source/availability, full-stack counts before pagination, local series and account-owned saved views. Browse stays separate; no remote search |
| Available here | `routers/availability.py`, `services/availability.py`, `templates/fragments/available_here.html` | Actual live copies plus cached legacy/new holdings, not live reachability. Adaptations and Trash cannot bridge stacks; ownership alone is not a copy |
| Read-only sources | `routers/sources.py`, `services/sources.py`, `services/source_links.py`, `services/source_connectors/{base,opds,jellyfin}.py` | Multi-instance OPDS 1/2 (safe generic/Calibre and open-access acquisitions) and selected Jellyfin types. Manual `links` has no test/sync and supports admin Remove link only. Bounded enumeration, complete-only absence reconciliation, config-change invalidation, no new scheduler/playback/progress sync. Legacy singleton health appears alongside new sources |
| Needs Attention and connection review | `routers/library_attention.py`, `services/library_attention.py`, `services/connection_suggestions.py` | Local quality/source queues, bounded evidence proposals, explicit accept/dismiss/restore with stale-evidence checks. Acceptance adds a format link only; Related Media unlink is undo |
| Personal activity | `routers/personal_library.py`, `services/personal_library.py`, `templates/activity.html` | Account/item-local status, progress, rating, favourites, notes/history, Continue and explicit My List/Planned Up Next. State-owned recency survives merges/reset history. No shared-state/Hardcover mutation, household isolation or cross-format progress conversion; shared legacy status still permits viewers |
| Metadata refresh safety | `services/metadata_policy.py`, `services/sources.py`, ABS/Komga/RomM persistence paths, `services/item_merge.py` | Validated new-source descriptive projection, bounded searchable provider IDs, instance/hashed-identity provenance and three-way refresh. Known edits/clears and unknown nonempty legacy baselines are preserved. Merged observations do not grant losing-source authority over kept manual fields |
| Saved metadata review | `services/metadata_review.py`, `routers/metadata_review.py`, `templates/fragments/metadata_provenance.html`, `routers/pages.py` | Registered/included item panel; viewer GET `/api/library/items/{item_id}/metadata`, editor POST `/api/library/items/{item_id}/metadata/resolve` with `keep_local`/`follow_source`, full saved-row fingerprint checks and read-only identity fields |
| Cover decisions | `services/cover_sync_policy.py`, `routers/cover_sync_policy.py`, `routers/items_covers.py`, ABS/Komga/RomM cover paths, `templates/item_detail.html` | Deliberate removal and explicit Allow missing-cover sync again have a separate durable decision, not queue dismissal. Source-fill guards recheck before writing, manual choice remains possible, and the existing book-cover queue remains a separate path |
| Music matching and artwork selection | `routers/music_matching.py`, `services/music_matching.py`, `services/music_review.py`, `services/shop_metadata.py`, `services/music_artwork.py`, `templates/fragments/artwork_dialog.html` | Identifier-first release search, selective signed review, public shop/release-link previews and an independent artwork chooser. Shop compatibility varies; Discogs stays optional pressing enrichment. Item detail uses the same chooser for other managed media |
| Navigation, schema and feature gates | `app/nav.py`, `app/features.py`, `app/database.py`, `app/main.py` | Standard includes `connected_library`; migration 58 preserves minimal upgrades and saved choices, migration 59 adds personal-state event order. Feature off hides/refuses without data deletion; private HTML defaults to no-store, Store remains separate |

## Remaining library frontend work

Consolidated on **2026-11-13** from the operator's full-library-frontend goals
and the repository [roadmap](roadmap.md). This is a backlog, not authorization
to implement every item, a dated delivery promise or an upstream release plan.
The roadmap's status labels are not sufficient evidence of a gap: several of
its planned foundations already exist on `new-test`.

### Frontend delivery backlog

| ID | Work remaining | Completion boundary |
|---|---|---|
| F1 | Fast, forgiving catalogue search | Benchmark the operator's 31,000+ record scale; add indexed/ranked search, useful typo/accent handling and bounded query/paging work without breaking same-edition filters, full format-stack counts or permission boundaries. `services/library_catalog.py::search_catalog` currently hydrates the live catalogue before filtering/grouping/pagination |
| F2 | Library-first everyday browsing | Make Home/Library a coherent media-first front door with configurable shelves, usable creator browsing and accessible keyboard/mobile flows. Surface existing Continue, Up Next and saved views rather than rebuilding them. Keep Browse for organisation and bulk editing; preserve the responsive shell and comfortable gutters |
| F3 | Reliable source refresh | Background/scheduled sync, visible job progress, bounded retries and trustworthy freshness/recovery. Do not silently truncate large collections or reconcile absence after a partial pass. New OPDS/Jellyfin sources currently require manual sync; keep legacy integrations and local edits working |
| F4 | Open and resume the right edition | Clear Read/Listen/Watch/Play actions, preferred-source selection and tested handoff to services the operator actually uses. Add explicit per-account progress integration with conflict/history rules; current personal progress is local/manual and an open link is not native playback or proof of access |
| F5 | Media-specific navigation | Browse book works/editions/copies and music artists/albums/pressings/tracks coherently; add actual TV season/episode support where sources provide it. Whole-series/album imports and existing format links are foundations, not a complete media hierarchy |
| F6 | Metadata, artwork and collections | Finish permitted source-artwork ingestion, TV artwork search, batch review/repair, curated collections and discoverable smart shelves built on saved filters. Preserve independent artwork decisions, provenance and manual edits. Grouping editions must not become automatic title-based merging |
| F7 | Access and sharing | Decide the household/library visibility model before exposing private collections to other people. Add permission-scoped invitations/sharing and private item/location rules; current catalogue visibility remains shared. The roadmap extensions below expand this area |
| F8 | Production acceptance and recovery | Real-server connector acceptance, pinned-runtime/container verification, large-library measurements and tested restore covering catalogue, music details, source bindings, personal activity and artwork, with keys securely preserved. Do not call the existing portable archive a complete export of those records |

Start with **F1/F2**, then **F3/F4**. Design F7's access model before adding
cross-household features or enabling external authentication; isolated
household browsing must not be simulated with UI-only filtering. Keep the
first usable version a catalogue and service launcher: built-in readers,
players, transcoding and DRM handling are not prerequisites.

### Roadmap additions and refinements

These are the missing pieces not fully captured by the frontend list, or
important expansions of it. Each remains **not implemented** unless its
current foundation is explicitly described below.

| ID | Roadmap item to retain | Existing foundation and remaining work |
|---|---|---|
| R1 | Full versioned read API and published schema | `routers/api_v1.py` exposes the token/session-readable `/api/v1/stats` contract, not paginated item, loan, wishlist, series and upcoming-release APIs. Add those documented, stable read contracts under the same feature/role rules and any future visibility policy |
| R2 | Trusted proxy-header login, then OIDC SSO | The existing trusted-proxy setting handles client IP/scheme, not identity. Define safe account linking, role mapping, trusted identity-header handling and a local-admin recovery path. Full OIDC follows the account/access design; do not trust identity headers supplied by arbitrary clients |
| R3 | Multi-household sharing and invitations | Extend F7 beyond private activity: distinct household/library access, invitations and sharing at chosen access levels, and private items/locations. Preserve the current catalogue as the initial household on upgrade. Retained legacy library/membership tables do not activate isolation |
| R4 | Borrow requests and permission-scoped owner search | Existing checkout/check-in and borrower history are not an inter-household request/approval workflow. Add requests and their lending ledger plus "who owns this?" search limited to collections the requester may see. Depends on R3 |
| R5 | Gift-buying wishlists with secret claims | Existing shared wishlists/public links do not reserve gifts secretly. Define recipient-safe claims, claim/release rules and concurrent-claim protection; prevent disclosure through pages, API responses, notifications and history. Depends on the access model, not just a hidden button |
| R6 | First-class repeat sessions, journal and yearly goals | Personal ratings, Paused/Abandoned, notes, history and Up Next already exist in `services/personal_library.py`. Remaining work is account-local re-read/re-listen sessions with their own dates, a dedicated journal and annual goals with explicit counting rules. Preserve earlier cycles and the separate legacy shared reading log |
| R7 | Audiobook metadata/artwork lookup by exact ASIN | Audible CSV import, validated ASIN storage/editing and local duplicate matching already exist. Add permitted, marketplace-aware metadata/cover lookup and review/backfill by ASIN, retaining narrator/duration/edition identity and manual overrides. Storing or searching an ASIN locally is not external enrichment; no audio or DRM handling is implied |
| R8 | Author-follow release alerts | `services/release_radar.py` watches series, not followed authors. Add author identity/following, discovery outside existing series and deduplicated notifications/digests that respect owned/wishlisted items and provider corrections. The roadmap marks this as exploring; settle provider coverage before promising it |
| R9 | Complete interface translation | Locale machinery and Italian/Simplified Chinese catalogs are present, but `SHELF_I18N` remains gated while feature pages, errors and JavaScript are completed. Audit untranslated surfaces, plural/placeholder handling, fallback and browser/account locale behaviour before treating the interface as fully translated |
| R10 | Additional national metadata coverage | The registry in `services/national.py` already includes BnF, DNB, SBN, KB, OSZK, PORBASE and NeoDB routes. Choose additional countries/providers from actual collection gaps, then verify identifiers, language/encoding, coverage and rate limits; do not re-add the existing providers as new work |

R1 can follow the search/query foundation. R6/R7/R8 can be scoped independently
of the media-server handoff work; per-user reading life does not need to wait
for every multi-household feature. Schedule R9/R10 around actual language and
catalogue needs. These priorities do not remove any item from the upstream
roadmap.

### Already covered: do not duplicate

- Setup profiles, feature switches and grouped navigation are implemented
  (`app/features.py`, `app/nav.py`, the setup/settings/base templates).
- API tokens and the dashboard stats document are implemented; **R1** is the
  rest of the read API, not another token system.
- Personal ratings, paused/abandoned states, favourites, notes/history,
  Continue and Up Next are implemented; **R6** extends them.
- Saved views, local series/format grouping, physical copies, basic lending,
  shared wishlists and series release watching are implemented; the corresponding
  backlog items add richer browsing or new cross-account workflows.
- Audible library imports and ASIN handling are implemented; **R7** is a
  separate external metadata lookup/backfill capability.
- Vinyl/shop matching and the shared artwork chooser are implemented; **F6**
  addresses remaining provider coverage, batch workflows and collections.

### Acceptance rules for future work

- Confirm provider capabilities and permission/account semantics before
  selecting an implementation. A mocked connector pass is not live-server
  acceptance, and an external link is not a playback/progress integration.
- Preserve catalogue records, physical copies, locations, personal history,
  existing integrations and the Portainer data/key configuration.
- Add feature/role/CSRF/privacy and stale-write coverage as appropriate;
  background work must stay bounded, observable, retryable and non-destructive.
- Benchmark search and grouped counts at collection scale. Check navigation,
  empty/error/disabled states and phone/tablet/laptop/desktop layouts, not just
  one screenshot.
- Verify migrations and full recovery on disposable copies before any operator
  rollout. Do not silently extend the portable archive to include credentials
  or private state without an explicit export/privacy design.

## Current persistence and recovery boundary

`MIGRATION_TABLES` owns eight additive tables: `source_instances`,
`source_records`, `user_item_state`, `user_item_events`, `library_saved_views`,
`connection_dismissals`, `item_metadata_provenance` and `cover_sync_blocks`.
Migration 59 adds state-owned `last_event_id`, default 0 rather than an inference
from history. The tables start empty rather than converting singleton sources
or shared status. Full database backup retains
them; covers and matching external encryption/signing keys must be preserved
separately. Credentials are encrypted with the existing external key and are
never rendered/exported as raw secrets.

The portable collection archive intentionally excludes all eight tables and
therefore the new connection config, credentials, bindings, personal
state/history (including ratings), saved views, connection dismissals, metadata
provenance and cover-sync suppression/opt-in decisions.
It is not a personal/source portability mechanism. Back up before experimenting;
disable Connected library in Settings → Features to hide it without deleting
data. All imported catalogue entries remain visible to all Shelf users in the
existing shared library.

## Invariants

- A format link is not an adaptation. Do not collapse all Related Media edges
  into equivalent-format groups.
- Never automatically merge records from title similarity. Uncertain matches
  require review; actions must be reversible.
- Only complete successful source enumeration may mark an unseen remote
  record unavailable. A timeout must not delete catalogue entries.
- Remote metadata is provenance, not permission to overwrite local edits.
- Credentials are encrypted at rest, never rendered or exported as raw secrets,
  and are not included in browser-facing links. A database backup retains the
  ciphertext, not its external key.
- All catalogue and copy reads use the existing live views. Background sync
  must not resurrect Trash.
- Roles, CSRF, optional-feature gates, CSP, and local-network integrations
  keep their existing guarantees.
- Preserve the single-container FastAPI/HTMX/SQLite architecture. No new
  mandatory external service or frontend rewrite.

## Verification and limits

The original experiment's local verification passed **7515 unit/integration
tests**, **384 browser tests**, `make checks-fast`, dependency consistency and staged whitespace
checks. The implementation stayed frozen throughout both full suites. See the
[verification record](connected-library-verification.md) for subsequent music/layout
passes, commands, earlier failures/fixes and intentional exclusions. Backlog
entries above are not covered by those passes and are not claims of completed
features. The private roadmap map was not available. Local Alpine Python 3.14 /
fallback Playwright 1.65.1 / Chromium 155
is not the pinned Python 3.12 / Playwright 1.52.0 CI runtime.

Metadata-review routes are registered in `app/main.py`/the feature registry and
their context/fragment is included on the item page. Sources' page/API routers
are registered explicitly, not through a combined `sources.router`. Registration
is also covered by the local route, feature and browser checks.

Source adapters can be checked against representative local fixtures;
compatibility with a particular live server cannot be claimed without testing
that server. The guide records the implemented adapter limits; native playback,
remote progress and commercial-service compatibility were not inferred from
these tests.

This branch does not promise every commercial provider, DRM handling,
playback/transcoding, or cross-format position conversion. Digital Video and
TV Series are distinct catalogue types, not physical DVDs. Supported sources
and intentional limits are documented in the guide; none is a universal
commercial-library connector or an OPDS-serving/hosted-reader implementation.
