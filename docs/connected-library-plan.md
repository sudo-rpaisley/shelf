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
tracking. That earlier decision does not automatically authorise implementation
of the later [research candidates](#research-additions-2026-11-14). The entries
below are requirements or labelled proposals, not claims that this work is done.

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
| ME7 | Adjustable media-cover size | Add a labelled, keyboard-accessible slider to adjust cover/card size with live feedback across Library and shared media-card views. Preserve each format's aspect ratio and contained artwork; resizing must not crop images, overlap text or cause whole-page horizontal overflow. Include a reset to the default size. Resolve account-versus-device persistence with the existing F2 preference work rather than creating a second preference store |
| ST1 | Steam library, links and playtime | The operator explicitly chose owned-library import, Steam launch/store links and Steam-reported playtime. Keep reported playtime as separate source data, never overwrite personal status/progress/history. Use stable App IDs, encrypted credentials, bounded read-only refresh and truthful private/incomplete-library states. Preserve catalogue/copies/manual edits; never reconcile absence after a partial or inaccessible result. Validate generated launch actions narrowly rather than allowing arbitrary custom URL schemes. Account/playtime visibility and integration must be reviewed before exposure; no Steam login, DRM, downloads or automatic launch is implied |

ME7 is a newly requested F2 presentation task, **not yet implemented**. Earlier
media and navigation receipts do not establish cover-size-slider acceptance.

The existing 185 acceptance checks remain unchecked. These refinements require
additional explicit implementation and verification criteria; they do not inherit
historical unit/browser passes. At this checkpoint standard PATH has no
Chromium or Python3.12 executable, so fresh visual and pinned-runtime evidence
remain unavailable. Local connector fixtures are not live Steam acceptance.

### Frontend delivery backlog

| ID | Work remaining | Completion boundary |
|---|---|---|
| F1 | Fast, forgiving catalogue search | Benchmark the operator's 31,000+ record scale; add indexed/ranked search, useful typo/accent handling and bounded query/paging work without breaking same-edition filters, full format-stack counts or permission boundaries. `services/library_catalog.py::search_catalog` currently hydrates the live catalogue before filtering/grouping/pagination |
| F2 | Library-first everyday browsing | Make Home/Library a coherent media-first front door with configurable shelves, a cover-size slider (ME7), usable creator browsing and accessible keyboard/mobile flows. Surface existing Continue, Up Next and saved views rather than rebuilding them. Keep Browse for organisation and bulk editing; preserve the responsive shell and comfortable gutters |
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

### Research additions (2026-11-14)

The operator requested research into further functional, integration and visual
improvements and their addition to this list. **This intake authorises research
and backlog recording only**, not implementation, provider registration/account
connections, production access, push or deployment. None of the following is
implemented or accepted by this research. Existing F/R/X requirements, ME7 and
their safety blockers remain intact.

The initial source comparison used **`f637f475`**, including the feature registry,
schema, services, routes, templates and browser assets, alongside the official
public documentation linked below, accessed **2026-11-14**. **Extend** means a
missing subcapability of an existing feature; **Gap** means no implementation
was found in the checked surfaces, not proof of absence from every experimental
branch. Independent source inventory and public integration research were
read-only; no app tests, provider authentication or live-server API calls were
part of this survey.

Priorities are recommendations, not delivery dates: **P1** alongside the existing
F1/F2 work, **P2** after the relevant foundations, **P3** optional/later. They do
not outrank unresolved safety/recovery gates or authorise a new implementation
lane. Reuse the existing server-rendered architecture and account/device
preference design; do not create another saved-view, alert or import platform.

The operator's follow-up “anything else?” prompted a second source pass against
**`36fc4423`**. It adds RF11–RF13 and sharpens RF8/ME2 below, rather than
rediscovering existing locations, scanning, missing/found tracking or CSV reports.
The same research/list-only authority applies; no further connector support,
implementation or operational acceptance is implied.

#### Functional and visual candidates

| ID / priority / existing area | Useful addition and proposed completion boundary | Source-grounded distinction and research |
|---|---|---|
| RF1 · P1 · F2/ME6/X5 · Extend | **Appearance and card-display preferences:** light/dark/system themes, comfortable/compact card density, readable text sizing, and selectable card metadata such as creator, year, format, availability and own progress. Retain today's default, spacious gutters and the 80px control-width rule. Coordinate with ME7 rather than add another cover slider; preserve proportions, readable titles, contrast and shared-device preference isolation | [Base](../app/templates/base.html) and [theme tokens](../static/css/input.css) are explicitly dark; [MediaCard](../app/services/media_cards.py) already has badges and [Browse](../app/browse_columns.py) has selectable columns. The gap is configurable shared-card/theme presentation, not cards themselves. Compare [Komga themes](https://komga.org/docs/guides/theme) and [calibre cover-grid fields](https://manual.calibre-ebook.com/gui.html#cover-grid) |
| RF2 · P1 · F2/X4 · Gap | **Searchable command palette:** a visible button and optional Cmd/Ctrl+K entry for permitted destinations, catalogue search and common actions. Make keyboard selection, Escape/focus return and shortcut discovery work without capturing text entry/IME or bypassing confirmation for destructive actions. Normal links and existing shortcuts remain usable; do not turn navigation into a frontend rewrite | [Global shortcuts](../static/js/app.js) already implement `/`, `b`, `s` and `?`; no searchable command-menu surface was found. [Linear's command menu](https://linear.app/enablement/guide/navigating-linear) is an interaction reference, not a dependency |
| RF3 · P1 · F1/F6 · Extend | **Advanced query builder and richer smart shelves:** understandable ANY/ALL/NOT groups, exclusions, date/number ranges and missing-field filters; save them in existing account-owned views. Bound expression depth/work, parameterise evaluation and report invalid expressions rather than silently broadening results. Preserve same-edition/same-holding matches, authorised facet counts and F1's 31k-scale paging guarantees | [Browse filters](../app/browse_filters.py) and [Library matching/saved views](../app/services/library_catalog.py) already support conjunctive filters, multi-format selection and wishlist negation. The gap is composable grouped expressions and their UI, not saved searches. Compare [calibre search](https://manual.calibre-ebook.com/gui.html#the-search-interface) and [virtual libraries](https://manual.calibre-ebook.com/virtual_libraries.html) |
| RF4 · P2 · F2/F6/F7/R3 · Extend | **Ordered mixed-media plans/collections:** reading, watching and playing orders across books, comics, films and games, with descriptions, move-up/down controls and progress derived from the current account. Keep personal ordering/progress distinct from shared membership; removing a list must not delete items. Collaboration/sharing waits for F7/R3, not a public-link shortcut | [My List/Up Next](../app/services/personal_library.py) and [public collection/wishlist links](../app/routers/share.py) already exist; `list_items` in [the schema](../app/database.py) has no curated position. Compare [Komga read lists](https://komga.org/docs/guides/readlists/) and [Audiobookshelf collections/private playlists](https://www.audiobookshelf.org/guides/collections); neither establishes Shelf support |
| RF5 · P2 · F5/F6/F8/X3 · Gap | **Typed custom metadata and reusable templates:** bounded text, numbers, dates, booleans and choice fields for collection-specific facts without misusing notes/tags. Decide definition/edit permissions and item/copy/private-value scope first. Keep stable field IDs through renames, validate and escape values, define safe type changes and include search/API/export mappings. No executable templates, arbitrary SQL or replacement of standard identifiers | [Fixed schema/tags](../app/database.py) and [common editor fields](../app/templates/fragments/item_editor_common_fields.html) are foundations, not a configurable field-definition/value model. [Koillection fields](https://github.com/benjaminjonard/koillection/wiki/Fields) and [templates](https://github.com/benjaminjonard/koillection/wiki/Templates) demonstrate the pattern; these older wiki pages are not a current interoperability contract |
| RF6 · P2 · F5/F6/F7/F8/X3 · Extend | **Copy-linked receipts and evidence attachments:** condition photos, purchase receipts and provenance documents attached to the right physical copy. Require a visibility/retention design, authenticated downloads rather than a public static mount, size/type quotas and safe data-only handling. Cover backup/restore, Trash/merge behaviour and explicit portable-export exclusions or inclusion before delivery | [Copy editing](../app/routers/item_copies.py) and [its form](../app/templates/fragments/item_copy_edit.html) already expose condition, acquisition date/source/price and provenance. Only file attachments are the new gap. Compare [calibre extra data files](https://manual.calibre-ebook.com/metadata.html#adding-extra-data-files-to-a-book) and Koillection's file/image fields; do not add another condition/acquisition subsystem |
| RF7 · P2 · F2/F3/R8/F7 · Gap | **Personal notification inbox and preferences:** durable unread/read state, category/channel choices, digests/quiet hours, deduplication and useful links for relevant releases, loans and source issues. Reuse existing outbound delivery; define event ownership, retention, retry and disconnect behaviour. Admin job failures, another person's loans/activity and secret gift claims must not leak through inbox counts or messages | [notify.py](../app/services/notify.py) already sends reminders/price/release digests via ntfy or JSON webhook, with [instance destination settings](../app/routers/settings.py). The gap is an account-owned inbox/preferences layer, not alerts, a new sender or a required push service |
| RF8 · P2 · F8/R1/X3 · Extend | **Standards-based metadata/list exchange:** select useful codecs such as RIS/BibTeX/CSL JSON, then separately assess CBL lists and NFO metadata. Publish supported fields and loss/exclusion reports; preview identity matches, preserve manual edits and safely parse bounded data without executing templates, external XML entities or imported actions. Keep personal watched/progress data opt-in. Assess formats individually, not a promise to deliver every codec or a replacement for full backup | [CSV](../app/routers/items_csv.py), [portable ZIP/JSON](../app/services/archive.py), [typed API schemas](../app/api_v1_schemas.py) and [internal JSON-LD extraction](../app/services/shop_metadata.py) already exist. Missing user-facing codecs extend them. References: [Zotero formats](https://www.zotero.org/support/dev/data_formats), [Komga CBL matching](https://komga.org/docs/guides/readlists/#import-read-lists-from-comicrack), [Jellyfin NFO](https://jellyfin.org/docs/general/server/metadata/nfo) |
| RF9 · P3 · F2/F6/X5 · Gap | **Optional spoiler/advisory presentation:** separate content advisories and spoiler-marked descriptions/episode details with explicit reveal and account preferences. Reuse typed metadata where appropriate; keep private notes private. Define behaviour in search previews, assistive announcements, shares and exports. This is a presentation choice, not parental controls or a substitute for server-side access restrictions | [Descriptions/catalogue notes](../app/templates/fragments/item_editor_common_fields.html) and [private notes](../app/templates/fragments/personal_library.html) exist; dedicated advisory/spoiler flags and reveal controls were not found. This is a source-derived candidate, not a claim of missing notes |
| RF10 · P3 · F2/F1 · Gap | **Local “pick for me” discovery:** an optional random pick or transparent suggestions within the authorised, filtered catalogue, for example owned/unread items or a chosen list. Explain selection, offer another choice and leave status/history unchanged. Keep work bounded and personal taste local; no cloud AI, mandatory recommender service, automatic media launch or cross-account activity profiling | [Discover](../app/templates/discover.html) is an existing search surface; [Continue/Up Next](../app/services/personal_library.py) follows explicit personal state, not local recommendation ranking. No local random-pick route/control was found. [calibre's Library menu](https://manual.calibre-ebook.com/gui.html#library) includes a random-book action |
| RF11 · P2 · F2/F5/X4 · Extend | **Find this exact physical copy:** open its existing room/bookcase/shelf path and highlight its saved position with nearby items, including a keyboard/text alternative. Choose the copy explicitly where several exist; an unknown position, missing mark or changed location must be explained, not presented as a verified physical sighting. Reuse native ordering, preserve return context and keep queries bounded and permission-scoped | [Hierarchical locations](../app/services/locations.py), [copy ordering](../app/services/location_order.py) and [Arrange](../app/templates/location_order.html) already exist. [Item copy links](../app/templates/fragments/item_copies.html) currently open location-filtered Browse and show position as text; a targeted locator/highlight flow was not found in those views or [their script](../static/js/location-order.js). This is a source-derived UX extension, not a new floor-plan/location engine |
| RF12 · P2 · F5/F8/X1/X3 · Extend | **Sale, donation and disposal history:** record when and why a particular copy left the collection, with optional price/evidence, without purging its identity, acquisition record or personal reading history. Define possession/availability/counts, active-loan conflicts, correction/undo and backup/export behaviour before implementation. Keep recipient/financial details permission-scoped; source refresh must not silently reacquire a disposed copy. This is a collection ledger, not a sales/payment service | [Copy management](../app/services/item_copies.py) currently removes copies through reversible Trash; [copy fields](../app/templates/fragments/item_copy_edit.html) cover condition/acquisition/provenance, while audits already track missing/found. No dedicated disposal-event ledger was found in the checked schema/routes/services. [Collections Trust disposal information](https://collectionstrust.org.uk/resource/disposal-information/) illustrates date/method/reason/price linked to an object; it is a recordkeeping reference, not a requirement for museum procedures or a compliance claim |
| RF13 · P3 · F5/F6/F8 · Gap | **Boxed-set and accessory completeness:** a per-copy checklist for expected discs, booklets, manuals, slipcases or inserts, recording present/missing/unknown and optionally linking RF6 evidence. Distinguish edition expectations from the contents of each owned copy; connect any lending/return review without replacing its existing ledger. Missing parts must not delete a catalogue item, create duplicate ownership counts or merge unrelated titles. Include child-state preservation in X1/X3/F8 | Existing [series completeness](../app/routers/series.py), physical copies and `music_media`/`music_tracks` in [the schema](../app/database.py) are not physical contents checklists for individual copies. No such model/control was found in checked copy/schema/editor surfaces. [Snipe-IT's versioned component/accessory concepts](https://snipe-it.readme.io/v8.3.6/docs/overview) are a modelling comparison only, not a promised integration or a reason to turn Shelf into an enterprise asset system |

#### Second-pass refinements to existing candidates

- **RF8/F8 — reusable CSV mappings and an import review workbench:** preserve
  the existing [known-format detection/normalisers](../app/services/reading_imports.py),
  duplicate safeguards and [row errors/dropped-column reports](../app/routers/items_csv.py).
  Add user-defined named column mappings, sample-row review and a before-apply
  summary of proposed adds/updates/skips/restores/Trash changes. Make unmapped
  values visible, offer a protected full row-result report and a reviewed retry
  of failed rows without duplicating successful changes. Revalidate mappings,
  permissions and stale item state at apply; no executable transformations or
  silent weakening of current import rules. Existing archive staging is not a
  CSV preview/profile system. [Libib field alignment](https://support.libib.com/libib/website/add-items.html#field-alignment)
  demonstrates the mapping/preview interaction, not Shelf compatibility or a
  reason to copy Libib's overwrite/identifier policies.
- **ME2/F2/X4 — efficient edit/review sessions:** add Previous/Next and explicit
  Save and next within a bounded, authorised result/selection, preserving filters,
  page/scroll and return focus. Warn before abandoning unsaved input; handle stale
  or inaccessible items without silently applying a draft to a different item.
  Extend the requested common editor, not another editing surface. The current
  [full-page editor](../app/templates/item_edit.html) is a fallback, not this
  sequential workflow; [calibre's metadata editor](https://manual.calibre-ebook.com/metadata.html#editing-the-metadata-of-many-books-at-a-time)
  documents Previous/Next as a useful comparison.

#### Integration feasibility candidates

No dedicated Plex, Navidrome/OpenSubsonic, Trakt or Playnite adapter/intake was
found in the checked feature registry, services or routes. The explicit
[new-source registry](../app/services/source_connectors/__init__.py) currently
lists OPDS, Jellyfin and manual links; the separate existing ABS/Komga/RomM and
metadata integrations are not missing features. These are **feasibility tasks**,
not supported connectors or permission to register an app or obtain credentials.
First confirm which services the operator actually uses.

| ID / priority / existing area | Opportunity and public evidence | Required feasibility boundary |
|---|---|---|
| RI1 · P2 · F3/F4/F5 | **Plex personal-server catalogue:** enumerate selected permitted libraries and retain server-scoped identities/availability. Plex now has an [official PMS API](https://developer.plex.tv/pms/); an unofficial Python wrapper is not the API contract | Validate the named PMS/API/auth versions, selected-library permissions, safe connectivity, token renewal/revocation and returned pagination offsets/counts. Published API 1.0 starts at PMS 1.41.9; documented 1.2.3 requires 1.43.4. Keep tokens in headers, not links. Read-only calls do not imply a read-only credential scope. Prove a credential-free external handoff separately; `server://` identity is not an OS-launch protocol. No management, scrobbling, playback, downloads or transcoding |
| RI2 · P2 · F3/F4/F5 | **Navidrome/OpenSubsonic music catalogue:** artists/albums/holdings and eventual external-app handoff, not another MusicBrainz/Discogs metadata authority. [Navidrome compatibility](https://navidrome.org/docs/developers/subsonic-api/) documents Subsonic 1.16.1 with exceptions and string IDs; [getAlbumList2](https://opensubsonic.netlify.app/docs/endpoints/getalbumlist2/) has offset/size paging, max 500 | Discover actual extensions/auth support and accessible music libraries; require HTTPS and supported [form POST](https://opensubsonic.netlify.app/docs/extensions/formpost/) for secret-bearing requests, with no URL-credential fallback or secret-bearing artwork links. API keys only when advertised; do not assume read-only scopes or universal quotas. Bound the artist index, which has no documented paging, prove album completeness during rescans and preserve server-scoped string IDs. No scrobbling, playlist/rating writes, scans, streaming or downloads |
| RI3 · P3 · F4/F5/X3 | **Trakt own-history/progress intake:** prefer a user-supplied one-time export where sufficient; consider consented ongoing observation only if needed. The [official API](https://developer.trakt.tv/openapi.json) documents history/playback observations; the [use policy](https://developer.trakt.tv/docs/api-use-policy) recommends user data export for one-time migration | Verify export coverage rather than assume partial playback is included; handle episode identity, repeat watches, timestamps and paging. For API observation, verify registered-app access and endpoint eligibility; broad [Standard Production access](https://developer.trakt.tv/docs/create-an-app) requires review, not owner/invite-only Development use. Respect current limits/429 handling and use [OAuth/PKCE with safe single-use refresh-token rotation](https://developer.trakt.tv/docs/authentication-oauth). Ordinary public-data read-only access does not grant private-account history access. Minimise retention, support disconnect/deletion and required attribution. Observations never silently overwrite personal progress/history; any accepted import follows F4 conflict/provenance rules. No watched-state writes or scrobbling |
| RI4 · P3 · F3/F4/ST1/X3 | **Playnite export intake, including user-supplied GOG entries:** extend existing game import/review paths rather than promise direct store-account synchronisation. Playnite documents [library enumeration](https://api.playnite.link/docs/tutorials/extensions/library.html) and [GUID-based details/start links](https://api.playnite.link/docs/manual/advanced/cmdlineArguments.html). These do not establish a universal stable export schema | Select a maintained exporter/version and redacted sample format; require stable provider/library IDs, preview duplicates and record playtime as separate observations, not completion. Exclude credentials, executable actions, installation paths and unnecessary notes; never run imported scripts or actions. Any later GUID handoff needs a narrow allowlist and explicit user activation. **Direct GOG account integration remains BLOCKED:** the [Galaxy integration API](https://github.com/gogcom/galaxy-integrations-python-api) supplies other platforms into Galaxy, not an account-wide GOG library-read API. No private endpoints, copied client credentials, cookie or database scraping |

All integration proposals retain encrypted credentials, source-specific identity,
manual metadata/artwork decisions, bounded work and complete-only absence
reconciliation. Validate remote permissions against Shelf's account/shared-library
model before exposing private provider observations. Read-only catalogue access,
availability, play counts and playtime are not consent to change personal status,
progress or history. No universal read-only credential scope or numerical quota
was established for Plex/OpenSubsonic; do not invent either. Rate/terms/version
and named-server acceptance must be rechecked when selecting an implementation.

KOReader metadata snapshots are a **parked F4/RF8 investigation**, not a new
Kobo/cloud connector commitment: the [official guide](https://koreader.rocks/user_guide/#L2-metadataarchive)
describes metadata archives/sidecars, but a stable third-party interchange schema,
safe data-only parser and reliable edition/progress mapping have not been
established. Do not execute settings files, import annotations by default or
treat a partial-file hash as a bibliographic identifier.

#### Existing requirements sharpened by this research

- **F3/F4 — source history and capability clarity:** extend existing
  [capability declarations](../app/services/source_connectors/__init__.py),
  [legacy health](../app/services/legacy_sources.py) and
  [latest source snapshots](../app/services/sources.py) with bounded run history,
  counts, partial/error explanations and next steps. Show catalogue, handoff and
  progress support separately. This is not another health system or scheduler.
- **F5/F6 — compare editions without merging:** extend the existing
  [duplicate comparison](../app/templates/duplicates.html) and
  [format-stack expansion](../app/templates/fragments/library_results.html) with
  a general authorised edition/pressing comparison and selection view. Contrast
  identifiers, language, narrator, duration, source and copy/availability where
  known; unknowns remain unknown. Comparison must not create format links or
  merge records from title similarity. Duplicate detection itself is not missing.
- **F2/X1/X4 — bulk clarity and recovery:** existing selection and loaded-row
  Select All in [browse.js](../static/js/browse.js) remain foundations. Make the
  selection scope/count explicit, consider keyboard range selection, inspect
  individual outcomes and retain refused/stale rows for safe retry. General
  history/merge undo remains X1; do not count current merging or Trash as unmerge.
- **F2/ME2/ME4/ME7/X4 — accessibility as a shared gate:** apply
  [WCAG 2.2](https://www.w3.org/WAI/WCAG22/quickref/) to new preferences, cards,
  dialogs, menus, lists and status messages, not a separate decorative redesign.
  Check text scaling/reflow, contrast, reduced-motion preference and full
  keyboard/screen-reader journeys. [AA target size](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html)
  is 24×24 CSS px with defined exceptions; 44×44 is the enhanced AAA criterion,
  not the AA minimum. Give dragging/reordering and the cover slider a
  [non-drag single-pointer alternative](https://www.w3.org/WAI/WCAG22/Understanding/dragging-movements.html)
  as well as keyboard access. Ensure focus is not entirely hidden by author
  content under [AA 2.4.11](https://www.w3.org/WAI/WCAG22/Understanding/focus-not-obscured-minimum.html).
  Existing navbar geometry/browser receipts are not WCAG conformance evidence.

The references establish design patterns or documented external interfaces,
not compatibility with Shelf. Older Koillection/Zotero references and evolving
provider documentation require version/field checks before implementation.
All **185 original acceptance checks remain unchanged and unchecked**; new
candidates need their own explicit acceptance criteria if selected. Existing
tests, original data, keys, integrations, native editor fallback, complete mixed
Browse and administrative/bulk Browse are not replaced or weakened by this list.

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
- ABS, Komga, RomM, OPDS/Jellyfin, Hardcover and MusicBrainz/Discogs already have
  integration foundations; Steam is already **ST1**, not a newly found gap.
- [CSV import/export](../app/routers/items_csv.py) and
  [staged archive intake](../app/services/import_staging.py) already exist.
  RF8 adds selected exchange formats, not another import/archive foundation.
- Barcode/copy labels, inventory audits, valuation, overdue reminders, price
  alerts and series release radar are existing optional capabilities recorded in
  [the feature registry](../app/features.py). RF6/RF7 add attachments and account
  notification state, not those systems.
- Hierarchical locations, physical shelf positions, [scan-based copy moves](../app/routers/items_scan_modes.py)
  and [missing/found audit tracking](../app/services/audits.py) already exist.
  RF11 adds a targeted locator experience; RF12 adds disposal events, not another
  location, inventory, missing-item or Trash implementation.

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
