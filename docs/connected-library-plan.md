# Connected library experiment

This work lives on the experimental `new-test` branch, starting at
`100219d` (the then-current Shelf baseline plus the Related Media refresh fix).
It is not a release announcement or an upstream design commitment.

## Goal

Make Shelf a single front door to a mixed physical and digital library:
find an item, see its editions and availability, and open it in the service
that actually holds it. Keep the existing catalogue, copies, integrations,
and administrative Browse usable throughout.

## Implementation areas

The table records the original experiment's foundations and limits, not a current
gap census. Later increments are recorded in the checkpoint below; “implemented”
does not mean released or universally compatible. The canonical setup/behaviour
reference is the [connected-library user guide](user-guide/connected-library.md).

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

### Bounded implementation checkpoint (2026-11-14)

Frozen **`916a4dd6`** contains reviewed media foundations/cards, strict
`types` filters, shared full-page editor fields and multipart proof, compatibility
test migrations, Home/Music/Library page adoption, shell/shortcut wiring and
regenerated assets. Primary
Browse opens mixed individual items with reset/recent ordering; administrative
Browse remains under Manage → Catalogue management. This is source integration,
**not ME1–ME6 or full-UI acceptance**. The separate
[frozen media gates](connected-library-verification.md#parent-frozen-media-gates)
do not add up to, or transfer to, the separate fresh **1935-case / 415.19 s**
composed affected pass. That pass is not a complete unit/browser/scale gate.

The pop-out editor, account-owned recent preference persistence/CAS and appearance
UI, browser interaction/layout and F1 scale acceptance remain open. **b** now
uses the same rendered authorised primary Browse href. CSS/catalogue/cache/badge
generation converged twice; the badges record **10125 unit / 422 E2E collected**,
not passing-test receipts. The pre-increment remote checkpoint was `b9c5fbec`;
publication of a reviewed increment does not authorise deployment.

Steam/host logging, core admission and portability repairs remain isolated, with
their bounded results and unresolved RED gates in the
[verification checkpoint](connected-library-verification.md#local-follow-up-candidates-documentation-capture).
All **185** original acceptance checks remain unchecked; **F1–F8, R1–R10 and
X1–X6 remain in scope**. No programme, pinned-runtime, live-provider, household or
deployment acceptance follows from this documentation snapshot.

## Remaining library frontend work

Consolidated on **2026-11-13** from the operator's full-library-frontend goals
and the repository [roadmap](roadmap.md). This is a backlog, not authorization
to implement every item, a dated delivery promise or an upstream release plan.
The roadmap's status labels are not sufficient evidence of a gap: several of
its planned foundations already exist on `new-test`.

**Implementation authorised:** the operator subsequently requested the whole
backlog, including optional refinements, on 2026-11-13. See the
[delivery programme](connected-library-delivery.md) for ownership and acceptance
tracking. The entries below are requirements, not claims that this work is done.

### Operator media-experience refinements (2026-11-14)

These additions are authorised within the active programme. They refine F2/F5/F6
and add a Steam source; they do not replace remaining F/R/X work or constitute
implementation, visual, live-provider or release acceptance.

| ID | Required outcome | Completion boundary |
|---|---|---|
| ME1 | One professional, mixed-library Browse entry | Clicking Browse opens all authorised live media together. Its disclosure offers media families and appropriate nested formats, such as Music → Vinyl/Cassette/CD. Remove duplicate top-level family links, not their routes or capabilities. Preserve administrative catalogue organisation/bulk editing under a clearly named management destination. Hover, click/touch and keyboard must all work; retain role/feature/manual-visibility rules and comfortable desktop gutters |
| ME2 | Common pop-out entry editor | One accessible dialog and shared common-field partials across media, with type-specific sections rather than separately styled editors. Preserve the full-page/deep-link fallback, permissions, CSRF, validation, unsaved input, stale-write behavior and return/focus context |
| ME3 | Consistent artwork-led media displays | Reuse card/artwork partials across Home, Library and media views. Music, including vinyl, must show artwork instead of text-only bars. Centralise media/format-specific default proportions; do not force cassette, CD and film artwork into one shape or destructively crop saved artwork. Preserve list/bulk organisation where appropriate |
| ME4 | Single-row recently-added shelves | Each shelf scrolls horizontally without wrapping into a second row, with usable scroll controls and a terminal View more action that retains its scope and newest-first ordering. Avoid whole-page horizontal overflow; test touch, keyboard, zoom and empty/error states |
| ME5 | Account-owned recent grouping | User settings offer mixed, media-family and individual-format recent shelves, plus ordered custom combinations of families/formats. Reuse existing preference foundations, preserve account isolation and define deterministic defaults/fallbacks. Do not repurpose a shared browser's storage as another account's preferences or fetch the whole catalogue per shelf |
| ME6 | Shared presentation foundations | A common media taxonomy/presentation registry, reusable cards/shelves/navigation/editor partials and small existing-framework components should make later changes propagate. Retain server-rendered FastAPI/HTMX, Alpine CSP, escaped metadata and existing feature boundaries; no separate frontend rewrite |
| ST1 | Steam library, links and playtime | The operator explicitly chose owned-library import, Steam launch/store links and Steam-reported playtime. Keep reported playtime as separate source data, never overwrite personal status/progress/history. Use stable App IDs, encrypted credentials, bounded read-only refresh and truthful private/incomplete-library states. Preserve catalogue/copies/manual edits; never reconcile absence after a partial or inaccessible result. Validate generated launch actions narrowly rather than allowing arbitrary custom URL schemes. Account/playtime visibility and integration must be reviewed before exposure; no Steam login, DRM, downloads or automatic launch is implied |

The existing 185 acceptance checks remain unchecked. These refinements require
additional explicit implementation and verification criteria; they do not inherit
historical unit/browser passes. At this checkpoint standard PATH has no
Chromium or Python3.12 executable, so fresh visual and pinned-runtime evidence
remain unavailable. Local connector fixtures are not live Steam acceptance.

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

These retain the requirements and foundations identified when the backlog was
consolidated, not a claim that every original gap is still absent from source.
For example, the first integrated read API and reading-life additions have
separately attributed gates in the delivery/verification records. No incremental
implementation closes its remaining operational or programme acceptance by itself.

| ID | Roadmap item to retain | Existing foundation and remaining work |
|---|---|---|
| R1 | Full versioned read API and published schema | The original stats-only gap now has integrated typed/paginated item, loan, wishlist/list, series and upcoming reads plus `/api/v1/schema` in `routers/api_v1.py`. Retain the documented feature/role contracts; household and operational acceptance remain open |
| R2 | Trusted proxy-header login, then OIDC SSO | The existing trusted-proxy setting handles client IP/scheme, not identity. Define safe account linking, role mapping, trusted identity-header handling and a local-admin recovery path. Full OIDC follows the account/access design; do not trust identity headers supplied by arbitrary clients |
| R3 | Multi-household sharing and invitations | Extend F7 beyond private activity: distinct household/library access, invitations and sharing at chosen access levels, and private items/locations. Preserve the current catalogue as the initial household on upgrade. Retained legacy library/membership tables do not activate isolation |
| R4 | Borrow requests and permission-scoped owner search | Existing checkout/check-in and borrower history are not an inter-household request/approval workflow. Add requests and their lending ledger plus "who owns this?" search limited to collections the requester may see. Depends on R3 |
| R5 | Gift-buying wishlists with secret claims | Existing shared wishlists/public links do not reserve gifts secretly. Define recipient-safe claims, claim/release rules and concurrent-claim protection; prevent disclosure through pages, API responses, notifications and history. Depends on the access model, not just a hidden button |
| R6 | First-class repeat sessions, journal and yearly goals | `services/reading_life.py` now adds explicit account-local repeat sessions, journal and annual goals alongside `personal_library.py`. Preserve earlier cycles, counting rules and the separate legacy shared reading log; portability, household and operational acceptance remain open |
| R7 | Audiobook metadata/artwork lookup by exact ASIN | Audible CSV import, validated ASIN storage/editing and local duplicate matching already exist. Add permitted, marketplace-aware metadata/cover lookup and review/backfill by ASIN, retaining narrator/duration/edition identity and manual overrides. Storing or searching an ASIN locally is not external enrichment; no audio or DRM handling is implied |
| R8 | Author-follow release alerts | `services/release_radar.py` watches series, not followed authors. Add author identity/following, discovery outside existing series and deduplicated notifications/digests that respect owned/wishlisted items and provider corrections. The roadmap marks this as exploring; settle provider coverage before promising it |
| R9 | Complete interface translation | Locale machinery and Italian/Simplified Chinese catalogs are present, but `SHELF_I18N` remains gated while feature pages, errors and JavaScript are completed. Audit untranslated surfaces, plural/placeholder handling, fallback and browser/account locale behaviour before treating the interface as fully translated |
| R10 | Additional national metadata coverage | The original BnF/DNB/SBN/KB/OSZK/PORBASE/NeoDB registry now also includes BN Poland. Verify identifiers, language/encoding, coverage and rate limits against permitted real providers; do not count existing routes as new coverage or fixture passes as operational acceptance |

R1 can follow the search/query foundation. R6/R7/R8 can be scoped independently
of the media-server handoff work; per-user reading life does not need to wait
for every multi-household feature. Schedule R9/R10 around actual language and
catalogue needs. These priorities do not remove any item from the upstream
roadmap.

### Final practical refinements

These refine the existing delivery areas, rather than introduce another set
of projects. Their completion boundaries remain requirements despite later
bounded implementations. The first four belong in the relevant area's acceptance
criteria; they do not change the F1/F2-first delivery order. The operator's later
whole-backlog authorisation includes both rows originally labelled Optional later.

| Priority | Attach to | Refinement and completion boundary |
|---|---|---|
| Core safety | F6/F8 | **Understandable change history and safe undo.** Record who or which provider changed catalogue fields, when, and the before/after values; preview high-impact bulk changes and retain the snapshots needed for supported rollback. Reversible merges require snapshots of identities, child records and artwork decisions, not just a copy of the kept title. Refuse or explicitly review rollback conflicts after later edits. Define retention/asset limits and permission-scoped history without exposing credentials or another account's personal activity |
| Core reliability | F3/F4/F6 | **Guided source replacement and binding repair.** Distinguish moving the same server to a new URL from replacing it with a different server or rebuilt remote IDs. Preview old/new identity mappings, ambiguous or unmatched entries and changes to opening actions before confirmation; make repeated application safe. Allow reviewed correction of a wrong attachment without the next sync silently undoing it. Preserve local item identity, copies, activity, manual metadata and artwork decisions; retiring a source must not delete catalogue records. Never transfer bindings by title similarity alone |
| Portability | F8/R6 | **Complete, permission-scoped export/import.** Design versioned portable export/import for music details, personal state/history and saved views, with explicit choices for source bindings and metadata/artwork decisions. Publish the supported-field and exclusion contract, preview imports and test round trips. Reuse existing archive/import safeguards where applicable; do not export credentials or other users' private state, or imply that a portable export replaces the full database, artwork and key backup |
| Everyday acceptance | F2/F8 | **Keep navigation and failures usable.** Check return-to-results filters/scroll, Back/deep links, keyboard and screen-reader focus, contrast and zoom across Library, review dialogs and source handoff. Make loading, empty, stale and error states clear; protect unsaved input through recoverable errors or reauthentication and report which bulk actions succeeded. Preserve existing Browse restoration rather than rebuilding it |
| Optional later | F2 | **Cross-device preferences.** Extend existing account-owned saved views with useful shelf ordering, default views and selected display preferences where wanted. Browse currently stores view/column/sort choices in the browser; define account-versus-device precedence and shared-device privacy before moving any of those choices server-side. Do not duplicate saved views or blindly assign a shared browser's preferences to its next user |
| Optional later | F2/F7/F8 | **Offline reference browsing beyond Store Mode.** Only if useful, add an explicit, bounded opt-in catalogue/artwork cache with visible last-sync time and clear unavailable actions. Design account isolation, expiry and logout/next-contact revocation cleanup first, and document that an offline device cannot observe an immediate remote permission change. Keep credentials and private activity out of a generic cache; preserve private-page no-store defaults unless a separate reviewed offline design replaces them. This does not imply offline media downloads or playback |

The following original backlog observations explain the gaps at consolidation;
they are historical, not the current source-repair/portability/offline inventory:

- `routers/items.py::merge_items` reparents children and deletes the losing
  item. `services/item_merge.py` preserves defined history/bindings, but does
  not retain a general unmerge snapshot. Personal activity history, inventory
  audit entries, Trash and saved metadata provenance are not a general
  catalogue-edit undo facility.
- `services/sources.py::save_settings` preserves external-ID bindings on a
  root change; `attach_record` refuses moving an existing identity to another
  item. `services/source_links.py::detach` only removes manual links. Existing
  disable/re-enable, exact-ISBN adoption and configuration invalidation are
  safeguards, not a guided server-replacement or machine-binding repair flow.
  See [source configuration changes](user-guide/connected-library.md#change-configuration-disable-or-re-enable).
- [Backup and portability](user-guide/connected-library.md#backup-and-portability)
  documents the existing export exclusions. Full recovery and portable
  per-account data movement need separate acceptance tests.
- `static/js/browse.js` already restores filters and browser-local display
  preferences. Account-owned Library saved views already exist;
  `static/sw.js` explicitly supports offline **Store Mode only**. Neither
  optional refinement is a prerequisite for the first everyday Library.

The original F1/F2-first, then F3/F4 ordering remains useful. It does not remove
any F/R/X requirement or the refinements included in the subsequent whole-backlog
authorisation; additional scope still needs an explicit decision.

### Already covered: do not duplicate

- Setup profiles, feature switches and grouped navigation are implemented
  (`app/features.py`, `app/nav.py`, the setup/settings/base templates).
- API tokens, the dashboard stats document and the first paginated read API are
  implemented; **R1** still needs its remaining acceptance, not another token system.
- Personal ratings, paused/abandoned states, favourites, notes/history,
  Continue and Up Next remain; **R6** adds the separately gated reading-life surface.
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

The original experiment added eight `MIGRATION_TABLES` tables: `source_instances`,
`source_records`, `user_item_state`, `user_item_events`, `library_saved_views`,
`connection_dismissals`, `item_metadata_provenance` and `cover_sync_blocks`.
Migration 59 adds state-owned `last_event_id`, default 0 rather than an inference
from history. Those tables start empty rather than converting singleton sources
or shared status. Later source/API/reading-life increments do not reduce the
full-backup fidelity requirement. A full database backup retains database rows,
not covers or external encryption/signing keys; preserve matching assets and keys
separately. Complete restore fidelity still needs its own acceptance evidence.
Credentials remain encrypted with the existing external key and are never
rendered/exported as raw secrets.

The portable collection archive intentionally excludes all eight tables and
therefore the new connection config, credentials, bindings, personal
state/history (including ratings), saved views, connection dismissals, metadata
provenance and cover-sync suppression/opt-in decisions.
The isolated portability candidates are not primary-source or X3 acceptance and
do not change that published exclusion contract. Back up before experimenting;
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
