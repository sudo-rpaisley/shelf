# Connected-library acceptance checklist

Acceptance contract for the experimental `new-test` branch, recorded **2026-11-13**.
The operator has explicitly authorized **all F1–F8, R1–R10 and X1–X6**;
X1–X6 name the six Final practical refinements in the
[plan](connected-library-plan.md), in their existing order. This includes
cross-device preferences and offline reference browsing, previously optional.
Implementation is underway across parallel assignments. **Assignment is not completion.**
Every requirement and every status below starts unchecked; this document claims
no implementation completion, new test pass, deployment, stability or release.

## Evidence and sign-off

Read alongside the [architecture](architecture.md), [CLAUDE.md](../CLAUDE.md),
[GOTCHAS.md](../GOTCHAS.md), [current user guide](user-guide/connected-library.md)
and [executed verification record](connected-library-verification.md).
Existing foundations are not substitutes for the additional acceptance boundaries.
Each compound checkbox requires evidence for every property it names.

- **Implemented:** reviewed implementation, registered interfaces, migrations and documentation meet the entire ID; authored tests alone do not establish a pass.
- **Locally verified — frozen candidate:** executed tests and measurements identify an immutable candidate and exact runtime. A pass on a changing swarm tree is not attributable final evidence.
- **Operationally verified — named live server/runtime:** an authorized running installation demonstrates the workflow. Name the Shelf runtime and, where relevant, the actual provider/server and version; label disposable/test versus production explicitly. Fixtures are not live-provider acceptance.
- The future-evidence links below are **empty sign-off slots**, not result artifacts. Replace each ID's link with its actual reviewed diff, run report or operational record before checking its status. Keep blocked/failed/excluded work visible; do not infer one status from another.

### Future implementation evidence

**Pending for every ID.** Supply commit/tree and review links, actual route/configuration/schema references, regression selectors and supported limits. A file's presence, an assignment or an older foundation does not close the ID.

### Future frozen-candidate evidence

**Pending for every ID.** Supply candidate/tree digest, dependency/browser versions, commands, exit codes, durations, failures/skips and durable log/artifact links. Run unit/integration and browser suites separately; identify generated assets and keep the implementation frozen throughout the relevant gates. Record focused adversarial tests as well as full regressions.

### Future operational evidence

**Pending for every ID.** Supply date, non-secret installation/server identifier, image digest, Python/SQLite/browser and provider versions, authentication mode, selected scope/counts and observed outcomes. Include failure/recovery and repeated-run behavior, not merely a connection-test response. Never publish credentials, signing keys, private catalogue contents or recipient-secret gift data as evidence.

[e-i]: #future-implementation-evidence
[e-l]: #future-frozen-candidate-evidence
[e-o]: #future-operational-evidence

## Known evidence boundaries and blockers

- Historical full/focused passes in the [verification record](connected-library-verification.md) concern earlier frozen candidates; they do not verify this newly authorized scope or the currently changing implementation. No tests were executed for this documentation task.
- **Pinned-runtime/container verification is blocked here:** the recorded environment is Alpine aarch64, Python **3.14.8**, fallback Playwright **1.65.1** and native Chromium **155.0.8059.84**. It has no Docker engine or Python 3.12 interpreter. This is not Python **3.12** / Playwright **1.52.0** verification; see [CI](../.github/workflows/test.yml), [development pins](../requirements-dev.txt) and [Dockerfile](../Dockerfile). The orchestrator must arrange the pinned runtime/build elsewhere; dependency relaxation or a fallback-browser pass cannot close that gate.
- At source inspection, [`search_catalog`](../app/services/library_catalog.py) hydrates the live catalogue, holdings, identifiers and tags before filtering/paging. Existing [semantic tests](../tests/test_library_catalog.py) are not a 31,000-record bounded-hydration benchmark.
- At source inspection, [OPDS](../app/services/source_connectors/opds.py) and [Jellyfin](../app/services/source_connectors/jellyfin.py) have 5000-entry ceilings. Raising a limit, narrowing the operator's scope or repeatedly fetching the same first page is not full-source completion.
- **Restore safety blocker:** [`restore_backup`](../app/routers/settings.py) currently calls `src.backup(dest)` against the live database **before** `init_db()`. Its upload validator lacks `PRAGMA foreign_key_check` and `PRAGMA integrity_check`. This is a source finding, not a newly executed failure reproduction. F8 must reject failed migrations, corrupt databases and orphan-bearing uploads without first replacing or otherwise changing the live database.
- [Persistence](../tests/test_connected_persistence.py), [backup](../tests/test_backup_passphrase.py) and [older-fork upgrade](../tests/test_connected_pre037_upgrade.py) tests provide useful success-path/bridge coverage, not proof of that rejected-upload guarantee. Current [archive](../app/services/archive.py) exclusions and [created-items-only history semantics](../tests/test_archive.py) are not complete own-state portability or undo.
- The current [auth/static mounts](../app/main.py) exempt `/covers/` from authentication; [Store Mode](../app/routers/store.py) and its [service worker](../static/sw.js) have separate browser storage/cache behavior. Account-local [activity tests](../tests/test_personal_library.py) do not establish household isolation across these paths.

## Preservation gates applying to every ID

- [ ] Retain catalogue IDs, identifiers, format/adaptation distinctions, copies including Trash, copy codes/numbering/primary status, locations/tree/order/missing marks, acquisition/provenance, loans and existing history. Ownership alone must not invent a physical copy; digital imports must not become discs.
- [ ] Keep Browse, scanning, inventory, labels, bulk editing, shares and Store Mode usable under their applicable permissions. Preserve saved feature choices and disable/re-enable without deleting data; new household rules must cover old routes, not only new Library pages.
- [ ] Preserve legacy Audiobookshelf/Komga/RomM settings, bindings and sync semantics unless an explicit reviewed migration changes them. Keep manual edits/clears, metadata provenance and independent cover suppression/allowance decisions through refresh, merge, repair and recovery.
- [ ] Use the existing live-read views and item/copy write funnels; keep migrations append-only with correct fresh/upgrade schema ownership. Exercise authentic historical DDL, interrupted/overlapping startup and a second boot; unknown forks/conflicts must fail safely rather than discard data.
- [ ] Preserve the single-container FastAPI/HTMX/Alpine CSP/SQLite architecture, local-network integration support, role-before-feature checks, CSRF, strict CSP, bounded input/network work and encrypted credentials. No mandatory external service, DRM bypass or built-in playback is implied.
- [ ] Preserve the operator's Portainer data volume, covers, ports, network, environment and matching `data/encryption.key` / `SHELF_ENCRYPTION_KEY` and `data/signing.key` / `SECRET_KEY`. Record configuration names and secure custody, never secret values. Authorization to implement is not authorization to deploy, merge or modify live data.

## Frontend delivery

### F1 — Fast, forgiving catalogue search

- [ ] Benchmark at least **31,000 representative records**, with mixed scripts/media, large format stacks, copies, tags and multiple source holdings. Publish numeric latency/memory/hydration budgets before execution, then cold/warm p50/p95, query plans and deep-page/broad/empty-query results on named hardware; unexplained full scans fail acceptance.
- [ ] Indexed/ranked search covers existing title/creator/narrator/series/publisher/tag/identifier fields; exact identifiers rank ahead of fuzzy matches. Demonstrate bounded typo/accent handling without deleting non-Latin identities, changing stored metadata, treating SQL wildcards as arbitrary SQL or silently replacing the user's query.
- [ ] Instrument hydration: item dictionaries, holdings, tags and provider JSON are loaded only for bounded candidates/visible page members, not all 31,000 records per request. Counts may use lightweight indexed IDs/graph data; oversized stacks require an explicit complete expansion/paging contract, never silent truncation.
- [ ] Preserve same-edition compound filters, same-holding source/availability, full permission-scoped matching/stack counts before pagination, format-only transitivity, stable tie-breaks and no repeated/split stacks. Trash and invisible members cannot bridge visible groups or leak counts.
- [ ] Keep indexes correct after add/edit/import/source refresh/merge/Trash/restore and permission changes; demonstrate rebuild/restart behavior and bounded query/token/page work. Re-run existing Unicode/grouping semantics against the intentionally documented accent/typo changes.
- [ ] **Implemented** — [pending F1 evidence][e-i].
- [ ] **Locally verified — frozen candidate** — [pending F1 evidence][e-l].
- [ ] **Operationally verified — named live server/runtime** — [pending F1 evidence][e-o].

### F2 — Library-first everyday browsing

- [ ] Home/Library provides media-first shelves, creator drill-down and configurable shelf composition/order using existing Continue, Up Next and saved views. Empty personal state is not replaced with another account's activity or inferred from shared status.
- [ ] Browse remains the administrative/bulk workspace; navigation distinguishes catalogue editing, personal activity and service launch. Deep links and return-to-results retain active filters, grouping, page, sort and scroll without overwriting existing Browse restoration.
- [ ] Complete keyboard/touch/screen-reader journeys at 320, 375, 640, 768, 1024, 1280, 1366 and 1920 pixels; preserve comfortable shell gutters, focus visibility, readable controls, zoom and print behavior. A screenshot alone is insufficient.
- [ ] Loading, no results, no holdings, feature-off, stale source and recoverable error states explain next actions. Implement X4–X6 rather than treating preferences/offline as implicitly delivered by the existing shell.
- [ ] **Implemented** — [pending F2 evidence][e-i].
- [ ] **Locally verified — frozen candidate** — [pending F2 evidence][e-l].
- [ ] **Operationally verified — named live server/runtime** — [pending F2 evidence][e-o].

### F3 — Reliable source refresh

- [ ] Scheduled/background refresh has persisted observable job state, separate attempt/complete-success timestamps, bounded retries/backoff, cancellation/restart recovery and manual retry. Feature-off/disabled sources stop new work; leases and configuration revisions reject overlapping/stale applications without long network-held write locks.
- [ ] Enumerate an entire selected source **beyond 5000 records** using bounded pages/checkpoints or safely partitioned resumable work. Prove every expected remote ID is visited exactly as required; UI reports the full scope and partial progress, not a falsely complete capped subset.
- [ ] Only a warning-free complete enumeration of an unchanged scope reconciles absence. Test a failed later page, timeout, 429/auth failure, changing totals, duplicates/overlap, deleted page and restart mid-pass; no new absence verdict is written for unseen bindings, catalogue records persist and Trash is never resurrected.
- [ ] Repeated passes preserve local edits/clears, copy/location/ownership state, provenance and artwork decisions across new and legacy connectors. Safe diagnostics and progress cannot reflect credentials; retire/disable retains catalogue and recoverable bindings.
- [ ] Demonstrate the actual worker running against each claimed named server/version with real pagination, credential expiry and recovery. Offline fixtures such as [source safety tests](../tests/test_sources.py) do not establish live coverage or scheduling operation.
- [ ] **Implemented** — [pending F3 evidence][e-i].
- [ ] **Locally verified — frozen candidate** — [pending F3 evidence][e-l].
- [ ] **Operationally verified — named live server/runtime** — [pending F3 evidence][e-o].

### F4 — Open and resume the right edition

- [ ] Read/Listen/Watch/Play selects an explicit edition and eligible holding, with account-owned preferred-source choices and deterministic fallback. Disabled, invisible or unavailable holdings never regain an action through a saved preference; browser/internal roots and reverse-proxy base paths are tested.
- [ ] Show cached availability and last-seen evidence honestly; launching a credential-free URL is a handoff, not proof of access, native playback or resume. Verify that the named user's actual service account can open the intended remote item.
- [ ] Progress integration is explicit per account, source user and remote edition, with documented units/direction, opt-in, conflict resolution and history. Test simultaneous manual/remote changes, stale observations, repeat delivery, rewind/reset and completion without overwriting another user or shared legacy status.
- [ ] Prove real supported progress read/write behavior against each claimed provider; unsupported services remain labelled launch-only. Never infer cross-format page/time conversion, remote permission or write capability from a mocked endpoint or successful open link.
- [ ] **Implemented** — [pending F4 evidence][e-i].
- [ ] **Locally verified — frozen candidate** — [pending F4 evidence][e-l].
- [ ] **Operationally verified — named live server/runtime** — [pending F4 evidence][e-o].

### F5 — Media-specific navigation

- [ ] Books distinguish work/edition/physical copy and expose all live locations without treating adaptation links or a title match as equivalent editions. Existing edition/copy IDs and item-level loan limitations remain understandable.
- [ ] Music navigates artist → album/release group → release/pressing → medium/side → ordered tracks, preserving MusicBrainz release identity, identifiers and physical-copy condition/acquisition data. Repeated source imports do not collapse different pressings.
- [ ] TV sources that actually supply hierarchy expose series → season → episode, including specials, unknown numbering and missing/partial seasons. Whole-series records remain valid; films/digital video/physical DVDs are not reclassified or duplicated to fabricate episodes.
- [ ] Count/filter/paginate hierarchy children within permissions; verify deep links, Back and edition-specific actions with representative real provider data. Existing album/series-only imports are not evidence of track/episode support.
- [ ] **Implemented** — [pending F5 evidence][e-i].
- [ ] **Locally verified — frozen candidate** — [pending F5 evidence][e-l].
- [ ] **Operationally verified — named live server/runtime** — [pending F5 evidence][e-o].

### F6 — Metadata, artwork and collections

- [ ] Add permitted source-artwork ingestion and TV-specific artwork search with documented provider/media dispatch; no TV/game/music fallback into book ISBN enrichment. Verify real metadata/image responses, redirects, signature/size limits and allowed use; no borrowed third-party credential is acceptable.
- [ ] Automatic fill respects chosen artwork, explicit removal/suppression and later allowance, rechecking under lock after download. Metadata decisions remain independent; failed/stale operations clean staged assets and cannot leave rows pointing to absent files.
- [ ] Batch review previews selected fields/artwork and before/after values, rechecks stale evidence/permissions and reports per-item outcomes. Manual edits and clears survive repeated provider corrections; title similarity never triggers an automatic merge.
- [ ] Curated collections and discoverable smart shelves reuse saved-filter foundations with defined ownership/sharing and permission-scoped membership/counts. Implement X1/X2 safety for repair/merge/bulk work, including reversibility and source identity review.
- [ ] **Implemented** — [pending F6 evidence][e-i].
- [ ] **Locally verified — frozen candidate** — [pending F6 evidence][e-l].
- [ ] **Operationally verified — named live server/runtime** — [pending F6 evidence][e-o].

### F7 — Access and sharing

- [ ] Publish the household/library/member/owner/admin visibility and mutation matrix before enabling cross-household access or external authentication. Define private item/location inheritance, explicit grants and revocation; source ingestion credentials alone are not Shelf visibility permissions.
- [ ] Enforce that matrix server-side on Library **and legacy** Browse, item/fragment/search/autocomplete, creator/series/music, related/format groups, counts/stats, scan/barcode/inventory, source/attention/history and lending/wishlist surfaces. Invisible records cannot leak through identifiers, suggestions, error differences or grouping bridges.
- [ ] Protect direct/static cover and thumbnail URLs, preview/download paths, public shares and remote open actions; guessed filenames, stale links, conditional requests and caches cannot bypass item visibility. Public assets require explicit scoped authorization, not the existing unconditional `/covers/` exemption.
- [ ] Refuse guessed foreign IDs on raw HTTP mutations, mixed-ID bulk requests, imports, merges, cover edits, location/copy changes and export jobs; demonstrate zero unauthorized row/asset writes. Scope CSV/archive/API/print exports and background-job results, including related child rows, not just displayed item lists.
- [ ] Browser storage, service workers, Store data/scan queues and X6 caches are account/household-scoped. Test A → logout → B, multiple tabs, role/membership revocation and delayed responses; A's pending writes never flush as B and private activity never becomes household-shared.
- [ ] **Implemented** — [pending F7 evidence][e-i].
- [ ] **Locally verified — frozen candidate** — [pending F7 evidence][e-l].
- [ ] **Operationally verified — named live server/runtime** — [pending F7 evidence][e-o].

### F8 — Production acceptance and recovery

- [ ] Freeze the integrated candidate, verify repository-pinned Python 3.12/Playwright 1.52.0 and its browser, and build/boot the actual container. Record image/runtime/dependency digests; local Alpine/native-Chromium evidence stays separately labelled and cannot substitute for this blocked gate.
- [ ] Rehearse old/current/known-fork upgrades on disposable complete copies, startup twice, and full database + artwork + matching external-key recovery. Compare catalogue/copies/music/hierarchies/bindings/personal history/views/provenance/cover decisions and all newly added records; validate FK/integrity and credential usability without revealing keys.
- [ ] Stage restore uploads away from the live database; check all-table `PRAGMA foreign_key_check` with no violations and `PRAGMA integrity_check` returning `ok` before and after successful supported migrations, before installation. Reject unknown/incompatible schemas, corrupt files and orphan-bearing uploads without changing live schema, rows, sessions, keys, artwork or cache state.
- [ ] Inject migration/validation/install failures and concurrent request/job/restore attempts; prove a consistent live database/WAL and recoverable pre-restore state, no orphan staged uploads/assets and no false success. Successful restore still replaces post-backup changes and invalidates prior sessions; new search indexes must not make Shelf's own backups unrestorable or weaken hostile-SQL validation.
- [ ] Record real-server connector/handoff acceptance and 31k-scale measurements for the frozen image; each provider claim names tested version/auth/scope. Full recovery and X3 portable import are separate tests; neither a mock suite nor a partial portable archive is a disaster-recovery pass.
- [ ] Document the Portainer preservation/restart and previous-image + pre-upgrade-data rollback procedure with secure key custody. Any operator rollout requires separate authorization; this checklist authorizes no production change.
- [ ] **Implemented** — [pending F8 evidence][e-i].
- [ ] **Locally verified — frozen candidate** — [pending F8 evidence][e-l].
- [ ] **Operationally verified — named live server/runtime** — [pending F8 evidence][e-o].

## Roadmap additions

### R1 — Versioned read API and published schema

- [ ] Extend the existing `/api/v1/stats` foundation with paginated item, loan, wishlist, series and upcoming-release read contracts. Publish actual paths, response schemas, field/null/date/error semantics, bounded filters/order/cursors and compatibility/versioning policy; examples must match registered interfaces.
- [ ] Preserve session/bearer behavior, viewer cap, revocation, rate limits, no-store and role/feature/household rules. Tokens remain read-only and valid only on the versioned surface; forged IDs/cursors and aggregate counts cannot disclose private records or gift claims.
- [ ] Traverse complete multi-page datasets without duplicate/missing rows under the documented snapshot/change policy; reject malformed/oversized queries and verify schema/HEAD/auth/error responses. Preserve [existing bearer tests](../tests/test_api_v1_auth.py), not merely the successful stats example.
- [ ] Execute documented examples with a real client on a named running candidate and show authorized versus denied data for two households. Redact tokens; generated schema presence alone is not API acceptance.
- [ ] **Implemented** — [pending R1 evidence][e-i].
- [ ] **Locally verified — frozen candidate** — [pending R1 evidence][e-l].
- [ ] **Operationally verified — named live server/runtime** — [pending R1 evidence][e-o].

### R2 — Trusted proxy-header login, then OIDC SSO

- [ ] After F7/R3 policy is settled, define explicit account linking, immutable external identity, role mapping and local-admin recovery. Never bind/promote an existing account merely because a supplied username/email matches; role/group changes and unlink/revocation have reviewed session consequences.
- [ ] Authenticate **trusted ingress**, not uvicorn-rewritten `request.client`: preserve/attest the original transport peer or use an independently verified ingress boundary before forwarded-header rewriting. Existing `SHELF_TRUST_PROXY` client-IP/scheme support is not identity authentication; the proxy must strip client-supplied identity headers and set its own.
- [ ] Fail closed on direct-port access, spoofed forwarding/identity headers, duplicate/missing/malformed claims and untrusted proxy hops. Test the real entrypoint/proxy chain, not a synthetic `request.client` alone; retain correct scheme/cookie/HSTS behavior and the [existing proxy-IP contract](../tests/e2e/test_proxy_trust.py).
- [ ] OIDC validates issuer/audience/signatures, state, nonce, PKCE and exact redirect policy, with safe linking/logout/error handling and protected token storage. Demonstrate both trusted-header and OIDC login/recovery on named ingress/IdP versions; a discovery document or mocked callback is insufficient.
- [ ] **Implemented** — [pending R2 evidence][e-i].
- [ ] **Locally verified — frozen candidate** — [pending R2 evidence][e-l].
- [ ] **Operationally verified — named live server/runtime** — [pending R2 evidence][e-o].

### R3 — Multi-household sharing and invitations

- [ ] Upgrade the existing shared catalogue into an explicit initial household without losing items, copies, accounts, legacy tables or existing access. Retained old library/membership tables do not silently become trusted current grants; document the migration and owner/admin recovery policy.
- [ ] Create distinct households/libraries and narrowly scoped sharing levels; invitation creation/acceptance is authorized, expiring, revocable and single-use with concurrent acceptance protection. Recipients cannot choose a stronger role/household by editing request fields or replaying an invite.
- [ ] Private items and private location subtrees follow documented inheritance and conflict rules across copies, searches, grouping, export and raw mutations. Revocation removes effective access at the server and invalidates affected caches/jobs/links at next contact, including legacy routes covered by F7.
- [ ] Exercise two households with overlapping identifiers/titles, multiple roles, invited outsiders and an anonymous user. Verify no cross-household adoption/merge/binding repair or private activity exposure; a UI-only filter or user-state table is not isolation.
- [ ] **Implemented** — [pending R3 evidence][e-i].
- [ ] **Locally verified — frozen candidate** — [pending R3 evidence][e-l].
- [ ] **Operationally verified — named live server/runtime** — [pending R3 evidence][e-o].

### R4 — Borrow requests and permission-scoped owner search

- [ ] Owner search returns only items/owners/copy locations the requester may see under R3, with privacy-scoped counts and no hidden-owner inference through identifiers, availability, API or errors.
- [ ] Requests have explicit requester, authorized lender, selected item/copy and pending/approve/decline/cancel/fulfilled/returned transitions. Only permitted actors can advance them; stale/replayed/concurrent approvals cannot create duplicate loans or bypass existing active-loan rules.
- [ ] Accepted lending creates the documented ledger relationship exactly once and preserves existing checkout/check-in/borrower history. Cancellation, loss of visibility, deleted/trashed items and returns have defined non-destructive outcomes and permission-scoped notifications.
- [ ] Drive request → approval → checkout → return between two named households, plus refusal/retry and revoked-access cases. A pre-existing checkout form does not establish this workflow.
- [ ] **Implemented** — [pending R4 evidence][e-i].
- [ ] **Locally verified — frozen candidate** — [pending R4 evidence][e-l].
- [ ] **Operationally verified — named live server/runtime** — [pending R4 evidence][e-o].

### R5 — Gift-buying wishlists with secret claims

- [ ] Define recipient, eligible giver/claimant and any separately authorized administrative access. Claim/release/expiry/purchased rules are transactional and replay-safe; simultaneous givers cannot both claim an exclusive gift, and a recipient cannot claim their own item to probe its state.
- [ ] Recipient-visible responses are independent of claims: no claimant, reservation/purchase status, timestamps, changed counts/order, hidden HTML/data attributes or claim-specific errors. Test direct/HTMX/versioned API, public shares, exports, history, notifications/digests, logs exposed to the recipient and browser/offline caches.
- [ ] Authorized givers can see only the claim detail necessary to avoid duplicate buying; editing IDs or recipient identity grants no extra access. Visibility revocation, wishlist removal and release preserve secrecy and follow documented conflict rules.
- [ ] Run concurrent-claim and recipient-versus-giver browser/network comparisons on a named installation. Hiding a button or omitting a visible badge while sending claim fields fails acceptance.
- [ ] **Implemented** — [pending R5 evidence][e-i].
- [ ] **Locally verified — frozen candidate** — [pending R5 evidence][e-l].
- [ ] **Operationally verified — named live server/runtime** — [pending R5 evidence][e-o].

### R6 — Repeat sessions, journal and yearly goals

- [ ] Re-read/re-listen sessions have stable account-local cycle identities and independent start/finish dates, progress and notes. Starting/reopening/resetting a cycle retains earlier cycles/events and correct state-owned recency; shared legacy reading logs remain separate.
- [ ] A dedicated journal supports bounded browsing/edit history and private notes across media without changing shared ratings/status or another user's activity. Define how edition changes, source progress, merges, Trash and restored items affect each cycle.
- [ ] Annual goals publish timezone/year-boundary and completion/repeat/abandonment/counting rules; test repeated finishes, corrections, backdated sessions, leap/year boundaries and zero goals. Counts must reconcile with the underlying eligible sessions, not mutable shared status alone.
- [ ] Two-account journeys and X3 own-state round trips retain complete cycles/journal/goals/history without duplicate counting or data leakage. Existing personal status/rating/notes do not by themselves satisfy this ID.
- [ ] **Implemented** — [pending R6 evidence][e-i].
- [ ] **Locally verified — frozen candidate** — [pending R6 evidence][e-l].
- [ ] **Operationally verified — named live server/runtime** — [pending R6 evidence][e-o].

### R7 — Exact-ASIN audiobook metadata and artwork lookup

- [ ] Name a permitted provider/API and supported marketplaces before claiming coverage; use the exact validated ASIN and marketplace, not a title guess or unrelated ISBN. Absence of an authorized usable provider leaves this ID blocked, not satisfied by local ASIN storage or Audible CSV import.
- [ ] Preview/review narrator, duration, language, abridgement/edition identity, description and artwork with source/time provenance. Apply only selected compatible values, preserving manual overrides/clears, existing identifiers, covers and account/copy state.
- [ ] Backfill is bounded, resumable, cancellable and per-item observable; test malformed ASIN, regional mismatch, multiple candidates, no match, 401/429/timeouts, stale review and repeated application without duplicates or false success.
- [ ] Record real exact-ASIN response and permitted image-download evidence for each claimed marketplace/provider, including a genuine miss/refusal. No media extraction, credential borrowing, commercial account scraping or DRM handling is inferred.
- [ ] **Implemented** — [pending R7 evidence][e-i].
- [ ] **Locally verified — frozen candidate** — [pending R7 evidence][e-l].
- [ ] **Operationally verified — named live server/runtime** — [pending R7 evidence][e-o].

### R8 — Author-follow release alerts

- [ ] Define account-owned author following using stable provider identity, ambiguity review and Unicode-preserving names, not loose name/title matches. Discovery includes releases outside existing series and does not replace existing series watches.
- [ ] Publish actual provider/language/region coverage and freshness limits; distinguish announced dates, undated records and corrections/withdrawals. Use bounded paced polling with restart-safe cursors and feature/account permission checks.
- [ ] Deduplicate by release identity and user notification state across series/author overlap, owned/wishlisted editions, corrected dates and retries. Failed delivery remains retryable without double announcements; unfollow and visibility revocation stop subsequent scoped digests.
- [ ] Show a real permitted provider finding a non-series release for a followed author, plus a delivered test digest and repeat-pass silence. [Series-radar fixtures](../tests/test_release_radar_service.py) alone do not demonstrate author discovery.
- [ ] **Implemented** — [pending R8 evidence][e-i].
- [ ] **Locally verified — frozen candidate** — [pending R8 evidence][e-l].
- [ ] **Operationally verified — named live server/runtime** — [pending R8 evidence][e-o].

### R9 — Complete interface translation

- [ ] Inventory all pages/fragments/dialogs, setup/auth, feature-off/error/validation messages, JavaScript, notifications and new F/R/X interfaces for English, Italian and Simplified Chinese. Resolve untranslated user-facing strings; a complete extracted catalog is not a complete surface audit.
- [ ] Verify plurals, placeholders/percent signs, escaping, dates/numbers and long/non-Latin text without changing provider titles, user notes, stored identifiers or API enum semantics. Exercise translated errors and dynamic HTMX/JS paths, not only static page loads.
- [ ] Preserve account locale → supported browser locale → English fallback, automatic choice, anonymous/share behavior and cross-account isolation. Test missing/unsupported catalog fallback and locale changes across tabs/reauthentication; retain the [locale test foundation](../tests/test_i18n.py).
- [ ] Inspect real keyboard/mobile/zoom journeys in every claimed locale on the named candidate. Only then document a decision about `SHELF_I18N`; existing catalogs or manually enabling the gate cannot justify a fully translated claim.
- [ ] **Implemented** — [pending R9 evidence][e-i].
- [ ] **Locally verified — frozen candidate** — [pending R9 evidence][e-l].
- [ ] **Operationally verified — named live server/runtime** — [pending R9 evidence][e-o].

### R10 — Additional national metadata coverage

- [ ] Select and name additional providers/countries from actual collection gaps, documenting permitted interfaces, registration-group coverage, rate limits and expected missing fields. BnF, DNB, SBN, KB, OSZK, PORBASE and NeoDB already exist and do not count as additions.
- [ ] Route by correct longest ISBN prefix without swallowing neighboring groups; choose the exact normalized edition from multi-record responses. Preserve NFC/non-Latin text, language, author-versus-translator/editor roles and provider-specific publication/series semantics.
- [ ] Preserve shared metadata outcome/fallback and outbound pacing contracts; test no match, malformed response, auth/rate/transport failure and image URLs bearing credentials or another party's API key. Metadata coverage does not automatically authorize a new artwork source.
- [ ] Retain [existing registry coverage](../tests/test_national.py) and add representative real payload fixtures, then record permitted live exact-identifier hits/misses and observed stored fields for every new provider. A registry entry or mocked single-record hit is insufficient.
- [ ] **Implemented** — [pending R10 evidence][e-i].
- [ ] **Locally verified — frozen candidate** — [pending R10 evidence][e-l].
- [ ] **Operationally verified — named live server/runtime** — [pending R10 evidence][e-o].

## Final practical refinements

### X1 — Understandable change history and safe undo

- [ ] Record authorized actor/provider, time, operation and complete before/after field values, including clears; preview high-impact bulk changes. Permission-scoped history must not reveal credentials, gift claims or another account's private activity through snapshots or diffs.
- [ ] Reversible merges snapshot **both original identities and complete child state**, including live/trashed copies and numbering/primary/location/order, tags/lists/links, loans/logs/audits, music release/media/tracks/identifiers, periodicals/hierarchies, legacy/new bindings and all per-account state/events/cycles. Include provenance, cover decisions and retained artwork bytes, not just the keeper's title.
- [ ] Inventory every affected child table, including records introduced by other F/R/X work; define restore/remap rules for IDs, unique constraints, sequences and derived indexes. Existing [child reparenting](../app/services/item_merge.py), including deliberate cascades, is not an unmerge snapshot.
- [ ] Rollback rechecks the saved post-change fingerprint and permissions under the write lock. Later edits, new loans/bindings, purged assets or changed membership cause refusal or an explicit reviewed conflict plan, never silent overwriting; failed/partial rollback leaves no half-restored children or orphan files.
- [ ] Publish snapshot/asset retention and size limits with visible expiry and protected cleanup; refuse unsupported/expired undo honestly. Rehearse edit, bulk and merge/unmerge with full child comparisons, concurrent edits, forced failure and restart on a named disposable installation.
- [ ] **Implemented** — [pending X1 evidence][e-i].
- [ ] **Locally verified — frozen candidate** — [pending X1 evidence][e-l].
- [ ] **Operationally verified — named live server/runtime** — [pending X1 evidence][e-o].

### X2 — Guided source replacement and binding repair

- [ ] Distinguish URL/base-path relocation of the same remote server from unrelated replacement or rebuilt remote IDs. Preview verified old/new identity mappings, ambiguities, unmatched records and changed opening actions; title similarity alone never transfers a binding.
- [ ] Confirm/apply is permission-scoped, fingerprinted, transactional and repeat-safe; stale config, concurrent sync or reused external IDs cannot reattach the wrong item. Preserve local item IDs, copies/locations, activity, manual metadata/provenance and artwork decisions.
- [ ] Permit reviewed correction/detachment of wrong machine attachments with durable reconciliation rules so the next sync cannot silently undo the repair. Keep legacy singleton identities distinct from new source instances and prevent cross-household adoption.
- [ ] Retiring/replacing/disabling a source never deletes catalogue rows. Rehearse same-server move, rebuilt-server mapping, wrong-binding repair, rollback and subsequent complete/partial sync against named servers; plain root editing is not this workflow.
- [ ] **Implemented** — [pending X2 evidence][e-i].
- [ ] **Locally verified — frozen candidate** — [pending X2 evidence][e-l].
- [ ] **Operationally verified — named live server/runtime** — [pending X2 evidence][e-o].

### X3 — Complete, permission-scoped portable export/import

- [ ] Publish a versioned supported-field/exclusion matrix covering music release/media/tracks/identifiers, catalogue/copies/relationships, **own** personal state/events/repeat sessions/journal/goals, saved views/preferences and chosen metadata/artwork decisions. Keep old archive compatibility explicit; portable data is not a full database/key backup.
- [ ] Export only authorized catalogue/child records and the caller's explicitly selected private state; do not export credentials, password/token hashes, signing/encryption keys, recipient-secret claims or another user's activity. Account IDs supplied by an archive cannot select a destination user or grant membership.
- [ ] Source bindings/configuration and instance-specific provenance require explicit choices and reviewed destination mappings, with credentials excluded even when bindings are selected. Unknown source IDs must not silently attach to a same-numbered destination server or acquire metadata overwrite authority.
- [ ] Preview exact item/child mappings, conflicts, exclusions and file limits; revalidate permissions/fingerprints on apply. Reject traversal/bombs/orphans, clean staged assets on failure and provide per-record results. Define behavior for matched existing items, not only newly created rows.
- [ ] Prove export → fresh install → import → re-export semantic equality for all selected own state and music/artwork data, then import into matched existing items and repeat without duplicated history. Compare values, dates, events, relationships and image hashes across remapped IDs; the current archive's matched-item history skip is not an acceptable complete round trip.
- [ ] **Implemented** — [pending X3 evidence][e-i].
- [ ] **Locally verified — frozen candidate** — [pending X3 evidence][e-l].
- [ ] **Operationally verified — named live server/runtime** — [pending X3 evidence][e-o].

### X4 — Usable navigation and recoverable failures

- [ ] Preserve filters/sort/grouping/page/scroll when opening items, dialogs, another format or a source and returning via Back/deep link. Respect explicit URL clears over saved views and retain existing Browse behavior across reload and repeated HTMX swaps.
- [ ] Provide predictable focus entry/trap/return, Escape, keyboard activation, meaningful labels/live error announcements, contrast and zoom across Library, reviews and handoff; inspect phone/tablet/laptop/desktop, not only DOM geometry.
- [ ] Protect unsaved input through timeout, validation/provider failure, offline transition and session expiry/reauthentication without exposing drafts to the next account. Replaced/late responses cannot apply a stale selection or discard a newer draft.
- [ ] Bulk outcomes distinguish succeeded, refused, stale and retryable rows; busy controls prevent double submission and always recover. Test slow/out-of-order responses, partial completion and retry with persisted outcome checks, not a success toast alone.
- [ ] **Implemented** — [pending X4 evidence][e-i].
- [ ] **Locally verified — frozen candidate** — [pending X4 evidence][e-l].
- [ ] **Operationally verified — named live server/runtime** — [pending X4 evidence][e-o].

### X5 — Cross-device preferences

- [ ] Extend account-owned saved views rather than create a competing system; support documented shelf ordering/default views/selected display preferences with validation, limits, reset and cross-device persistence.
- [ ] Publish deterministic explicit-URL/account/device/default precedence and concurrent-edit rules. A browser's existing Browse sort/column/view state remains device-local unless its owner explicitly imports it; never assign shared-browser preferences to the next user automatically.
- [ ] Scope preference reads/writes, caches and migration by authenticated account, not supplied user ID; unknown settings or unavailable feature/source references fall back safely without disclosing hidden records or resurrecting revoked access.
- [ ] Demonstrate two devices for A and A → logout → B on one shared device, including delayed save/reset, stale tabs and reauthentication. Export/import selected preferences through X3 without changing B's state.
- [ ] **Implemented** — [pending X5 evidence][e-i].
- [ ] **Locally verified — frozen candidate** — [pending X5 evidence][e-l].
- [ ] **Operationally verified — named live server/runtime** — [pending X5 evidence][e-o].

### X6 — Offline reference browsing beyond Store Mode

- [ ] Provide explicit account/device opt-in, selectable bounded catalogue/artwork scope, byte/item limits, expiry, last-successful-sync time and clear remove-cache controls. Offline pages explain stale data and unavailable mutations/handoffs; no offline media download/playback is implied.
- [ ] Use an independently reviewed account/household-scoped cache design; generic service-worker/page caches contain no credentials, private activity, gift claims or private HTML. Preserve ordinary private-page no-store defaults and Store Mode's separate supported behavior unless an explicit reviewed replacement covers them.
- [ ] Test CacheStorage, IndexedDB/localStorage, covers, service-worker versions and pending Store/browser queues across logout/login, account switching and multiple tabs. Unknown-owner legacy queues require explicit safe recovery, never automatic adoption; A's cached records/queued mutations cannot display or flush under B.
- [ ] On next contact, revalidate session/membership/scope and purge revoked data/open actions across tabs before refresh/replay; discard late responses for old identities. Define offline expiry and cleanup. Clearly state that a disconnected device cannot observe immediate remote revocation and retained copies cannot be remotely guaranteed erased.
- [ ] Exercise install/upgrade, offline restart, cache-limit/quota failure, expired sessions, permission revocation while offline, reconnect and logout cleanup in a real browser on a named runtime. A working `/store` precache or a fixture-only service-worker test does not establish this ID.
- [ ] **Implemented** — [pending X6 evidence][e-i].
- [ ] **Locally verified — frozen candidate** — [pending X6 evidence][e-l].
- [ ] **Operationally verified — named live server/runtime** — [pending X6 evidence][e-o].

## Execution handoff

- [ ] Orchestrator: freeze the integrated candidate and arrange full unit/browser gates, focused adversarial coverage, schema/restore/portable round trips, 31k measurements and checks against actual documented interfaces; attach evidence separately for each ID above.
- [ ] Orchestrator: obtain pinned Python 3.12/Playwright 1.52.0/container execution and authorized named-server/ingress/IdP/provider tests. Missing runtime, permissions, provider capability or permitted credentials remain explicit blockers, not waived passes.
- [ ] Reviewer: close statuses only from attributable evidence; reconcile canonical setup/API/architecture/recovery/user documentation with the final interfaces and limits. No checklist or historical pass is permission to commit, push, merge, deploy or touch production data.
