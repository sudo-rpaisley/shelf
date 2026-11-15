# Connected library delivery programme

The operator authorised implementation of the entire
[consolidated backlog](connected-library-plan.md#remaining-library-frontend-work)
on **2026-11-13**, including the cross-device and offline refinements. Work
starts from `b336939` on `new-test`. The earlier backlog's optional ordering
does not mean those items have been delivered or excluded from this request.
The **2026-11-14 ME1–ME6/ST1 refinements** below extend that programme; they do
not replace the remaining F/R/X requirements or inherit historical test passes.
The later [research intake](#research-intake-2026-11-14) is **research/list-only**;
its proposals do not inherit that earlier implementation authorisation.

**Subsequent operator continuation (2026-11-14):** the operator then explicitly
requested implementation of the full consolidated list, useful parallel agents,
continued discovery of relevant improvements and commits as work progresses.
RF/RI implementation may now proceed through their stated dependencies and
feasibility gates. Earlier research-only passages remain historical attribution;
they are not provider compatibility evidence or a prohibition on this newly
authorised implementation work.

**Subsequent operator instruction (2026-11-15):** push committed progress to
`origin/new-test` as work proceeds. This supersedes the earlier local-only/no-push
restriction, not the review or acceptance gates. Repository-local Git configuration
now reuses the existing GitHub CLI login; the first successful push advanced
`new-test` from `67461926` to `6ea540f6`. No tag, Docker publication, deployment,
Portainer change, provider
account access or household/graph activation is authorised.

Use coherent local commits, with independent review and relevant frozen-candidate
checks before coordinator integration. Preserve original tests, data, keys and
existing workflows. No production/provider-account access, app registration,
household/graph activation, deployment or expansion of narrowly approved
core SQL authority follows from the continuation request. Blocked operational
gates remain explicitly open rather than being marked complete.

## Completion and evidence

Assignment, a local implementation commit and a mock-provider test are not
interchangeable with completion. Each area must pass the applicable
[acceptance checklist](connected-library-acceptance.md), integration review
and regression gates. Record separately:

1. Reachable implementation and migration/UI/documentation support.
2. Tests and measurements on an identified, frozen candidate.
3. Operational acceptance against an identified runtime/server/account.

Never describe untested providers, missing container evidence or disabled
security-sensitive functionality as complete. New useful requirements found
during implementation join the relevant acceptance gate, rather than silently
changing what an existing item means.

## Parallel work ownership

Implementation agents use isolated `swarm/20261113-*` and `swarm/20261114-*`
branches/worktrees.
The coordinator integrates their commits, resolves shared schema/registration
changes, regenerates assets and runs combined gates. The current continuation is
coordinator-owned; incremental pushes to `origin/new-test` are authorised and
authentication is working. Workers do not push or deploy independently.
Worktrees are not production
data directories and must never be seeded from the operator's live catalogue.

| Lane | Backlog responsibility | Evidence state, not completion |
|---|---|---|
| Search | F1 indexing, ranking, bounded grouping/paging and scale measurements | Pure search foundation selected; catalogue hydration, persistent indexing, ranking and bounded SQL/RAM acceptance remain open |
| Front door | F2 shelves, creators, collections, preferences, navigation and whole-item sizing; ME1–ME7 | Published `f570b592`: 2137 unit / 327 browser / separate 12 navbar passes; document-local whole-item sizing/reflow, commands, locator and phone layout integrated; editor/account preferences/AT/pinned/scale gates remain open |
| Source jobs | F3 scheduling, progress, retry/recovery and large-source boundaries | Streaming/lease integration and runtime verification open |
| Handoff/progress | F4 preferred source and explicitly linked account progress | Next-batch integration; scheduled-pull acceptance open |
| Media hierarchy | F5 works/editions/copies, music and source children | Atomic child hook prepared; production composition open |
| Artwork/review | F6 source artwork, TV artwork and bounded reviewed batches | Candidate/fix review; actual job fencing open |
| Household access | F7/R3 ownership, invitations, visibility and legacy enforcement | Security fixes plus six legacy-port lanes; activation off |
| Recovery | F8 restore preflight, integrity and synthetic recovery evidence | Candidate/fix review; maintenance and identity composition open |
| Read API | R1 documented, typed, paginated read contracts | First integration pushed; household/operational gates open |
| Core admission | Callback/write/finalization privacy and compatibility | Isolated `53e1fecd`: original HIGH paths corrected, two further ownership findings; CHANGES_REQUIRED |
| Identity | R2 trusted proxy login and OIDC with local recovery | Actual-core write/startup admission blocked; live IdP acceptance open |
| Borrowing | R4 authorised owner search, requests and lending ledger | Lifecycle/scan-race fixes; real household adapter open |
| Gifts | R5 recipient-safe concurrent gift claims | Candidate/fix review; backup/history/household composition open |
| Reading life | R6 repeat sessions, journal and annual goals | First integration pushed; portability/household gates open |
| Audiobook enrichment | R7 exact-ASIN marketplace-aware review/backfill | Next-batch integration; provider-permission/coverage gate open |
| Steam | ST1 owned library, launch/store links and reported playtime | `333289b` standalone logging remains RED; isolated host-policy `481bdab` clear; Steam unregistered |
| Author discovery | R8 account-local following and deduplicated releases | Alias/revocation fixes; scheduler/household composition open |
| Translation | R9 existing surfaces and new English UI, including ME1–ME7/ST1 | Bounded source gates and complete baseline ledger; final composition/language/browser acceptance open |
| National providers | R10 additional documented, permitted provider coverage | BN Poland first integration pushed; final operational gates open |
| History/undo | General change history, supported rollback and merge snapshots | Integrity fixes and later-table registry composition open |
| Source repair | Reviewed replacement/rebinding and non-destructive retirement | Known-schema integrated scale gate passed locally; broader authority gates open |
| Portability | Versioned music/account-data export/import and round trips | Bounded ownership and `b5f6aad9` calibration clear; full gates remain RED for phase-B artwork; X3 open |
| Offline reference | Explicit bounded, account-isolated offline reference browsing | Bounded retirement source review clear; strict-browser/current-core composition pending |
| Browser privacy | Account/library-scoped Store queues and navigation storage | Bounded error/abort source review clear; password/current-core Store/PWA open |

### First continuation wave (2026-11-14)

The following records preserve earlier candidate-specific results. The
2026-11-15 selection below supersedes their pending UI-selection labels, not
their historical RED evidence or unrelated acceptance limits.

Independent architecture and additive acceptance audits partitioned three active
implementation lanes: a **page-only ME7 cover-size control**, **RF2 command
palette** and **RF11 exact-copy locator**. Shared shell wiring, styles/generated
assets, registration and documentation remain coordinator-owned. These are
assignments, not delivered features or inherited media/navbar test results.

The initial cover control is deliberately 50–100% of today's size, with native
keyboard input, non-drag buttons and reset. Its reviewed, explicitly selected
geometry retains fixed slots/columns and an 80px card floor: it shrinks cards,
not shelf density. Integration explains the limit and omits the control for all
Library series views, including drilldowns. Producer wiring has bounded source
clearance and fresh 368-unit/96-browser passes; the final test-only correction
also has source clearance. Combined UI qualification remains open. It does not
close above-default enlargement,
ME5/X5 account-owned persistence/CAS or cross-device privacy. The
palette derives only from rendered authorised navigation; locating uses exact
live copy/item IDs, bounded saved-order neighbours and truthful missing/unknown
states without feeding a partial list to Arrange's Save order. The locator lane
also owns an independently tested correction for a missing-only, unlocated copy's
invisible badge. No schema or access-authority expansion is assigned.

The isolated core's two-file `2ed54fb6` repair and independent nine-case H/M
packet now have **bounded source** and **test-quality** clearance respectively,
not overall core acceptance. Exact reviewed schema/H/M dependencies pass a fresh
**120-case parent gate**. The independently reviewed C1/D1 packet's full parent
513-case union completes **463 passed / the same 50 failed**, preserving every
original outcome and passing all 27 additions. Separate caller checks still have
the same five failures; actual TEMP-shadow/guard, overall core, pinned-runtime
and deletion acceptance remain open. See the
[current bounded review](connected-library-verification.md#latest-bounded-core-review-2026-11-14).

Clean UI baselines pass 993 affected unit and 19 original navbar browser tests.
The corrected palette component has fresh **229-unit/59-browser parent passes**
and bounded source clearance. Real shell wiring is frozen separately for review
and wider checks; its new standalone harness was adapted after a genuine duplicate
fixture RED, without changing any test bodies or assertions. Shared wiring has
bounded source clearance and **1158 unit passes**, but the wider browser band is
**81 passed / seven original failures**. All seven reproduce on the unchanged
baseline: five navbar failures plus two phone checks. The bounded five-regression
repair has source clearance and fresh original 234-unit/31-browser passes; a
conforming phone-opener follow-up is assigned after bounded contract clearance. Local-vector
artwork compatibility needs a distinct validated
source design, not an unsafe whitelist or test waiver. UI is not selected. The
unrelated original linter's temporary-path
assumption is qualified without changing source/assertions; earlier setup errors
and RED evidence remain retained. No original acceptance check is closed.

The narrowly selected **pure search-helper foundation and reachable catalogue
delegation** entered primary at `0d448bcd`, after independent source review,
**533 final passes** and seven offline checks. All original tests/acceptance bytes
remain unchanged. This is not indexed F1/RF3 or a schema/SQL-authority grant;
whole-catalogue hydration persists. Final-artwork recovery
still needs distinct commit/incarnation/lifecycle design approval. Only an additive
six-case crash-observation packet is frozen; its fresh parent **six passes** are
boundary evidence only, retaining the original Phase-B RED and TEMP's no-artwork
authority. Packet-quality review clears its test-only scope; the fresh original
334 remain **333 passed / the same precommit failure**. Locator source and parent
368-unit/39-browser checks clear only their bounded scope; actual shared
asset/i18n producer integration is qualified only in the unselected candidate
below. Single-item editor-dialog requirements
review requires changes to freeze limits, stored-state/actor checks, response ownership, unknown outcomes,
cookie-refresh privacy and artwork boundaries before assigning writers.

The first combined search/UI/locator candidate `cfffa6dc` now has bounded shared
wiring source clearance, **12 new HTTP/unit and 15 new real-browser passes**, and
eleven offline/syntax checks. Its wider browser gate is **178 passed / four
failed**, with the separate original-navbar 12 passing. Two retained phone
failures remain, and two unsupported-dialog palette fallback failures are genuine
composition regressions—not test waivers. The wider unit gate passes **1704**. The
phone writer has frozen the reviewed genuine-button direction at `3c2e85c0`.
The palette fallback repair `9562f558` now has bounded source clearance and fresh
**371-unit / 116-browser**, separately **12-original-navbar**, passes; the two
original fallback failures pass unchanged without a forced-open menu or new link.
Safe existing-local-vector compatibility `a873fefd` reproduces **898 unit / four
browser** passes in fresh parent gates, but is **held for two source-review
defects** (encoding boundary and descriptor-close ownership) plus cancellation
proof. A corrective derivative is assigned; green tests are not a waiver.
Phone `3c2e85c0` now has source clearance and fresh **459 passes / two same REDs**.
Actual composition clears Home375 and, with the source-reviewed caption/status
repair, reaches original line 1003: the book heading is at y=2209 versus the
unchanged y<900 requirement. Current `ced7fa30` has **25 browser passes / three
retained geometry failures**; the wider browser gate gives **241 passes / four
failures** (the same three geometry checks plus the uncomposed SVG original),
with the separate original-navbar 12 passing; its broader unit gate passes **1729**.
A bounded mobile-layout proposal is under requirements review, not yet authorized.
Vector correction `5ef2a7ea` now has bounded source clearance and fresh parent
**928-unit / five-browser passes**. Its Linux-only close-semantics boundary remains
explicit. The full vector range is now composed with phone/caption at `d11b37d4`:
five vector browser checks, including the full original SVG/Account journey, pass
on that actual tree. Its wider checks give **2076 unit passes / 246 browser passes
and three retained geometry failures**, with no component-pass transfer.

The reviewed finite mobile-layout contract is now implemented in a new, unselected
prototype `499ab86f`. Both complete original phone journeys, the retained later-leg
probe and focused geometry regression pass unchanged; the book heading is y=870.1875
against y<900. Retained checks also pass **170 unit / 48 browser**, plus eleven
offline/syntax checks and bounded source-review clearance. The two independent
test modules now supply 25 unit/35 browser cases. Their first unchanged composition
gives **23 unit passes / two failures** and **32 browser passes / three failures**.
Two bounded source repairs address series Help ownership and enlarged search-input
wrapping; four enlargement checks and all four retained phone journeys pass.
Their production delta has source clearance; reruns give **24/25 unit and 34/35
browser passes**, with the remaining failures under test-oracle review.
Independent test review also requires rejecting transparent focus decorations in
its oracle. Remaining oracle questions, reviews and final wider gates stay open.
Original tests are unchanged; this is not overall acceptance or UI primary selection.
Original tests remain unchanged and enabled; UI is still unselected.

### Previously selected UI foundation (2026-11-15)

Primary previously selected application **`56d4a434d86f1e54e3347017ce8a8a90b8a1c81c`**, by
fast-forward to the exact independently verified tree. It composes the full
reviewed palette, page-only sizing, exact-copy locator, native phone navigation,
caption/status, validated local-vector and Library wrapping-presentation ranges.
Fresh checks on that tree pass **2115 unit / 1137.87 s**, **300 browser / 785.71 s**,
the separately invoked **12 original-navbar checks / 38.57 s**, and eleven
offline/syntax checks. The complete original phone journeys and all three retained
geometry regressions pass unchanged.

The corrected presentation modules preserve all 60 original case IDs and add 30
oracle/boundary controls: **39 unit / 51 browser cases**. Independent test review
clears the owner-scoped notice/heading, visible-focus and all-fragment painted-order
corrections; no global palette/font manipulation or original-test weakening was
used. All 435 pre-integration primary test files, 459 D11 test files and the original
185 unchecked acceptance entries remain exact. Historical failures remain recorded.

This is bounded source selection and experimental-branch publication, **not
whole-programme, ME1–ME7, AT, pinned runtime or provider acceptance**.
Collection badges **10891 unit / 701 E2E** do not
claim full-suite passes. Core's same 50 REDs, artwork-recovery precommit RED,
F1/RF3 indexing and scale, account-owned preferences/CAS, complete editing and
operational gates remain open. The qualified code is published through the
confirmed `6ea540f6` push to `new-test`; nothing was deployed or image-published.

ME2-A's source-grounded finite protocol v2 is recorded, independently cleared for
design and ratified. No editor writer, new issuer, schema or expanded isolated-core
authority is assigned. Lost-response, cookie
ownership, native fallback and non-atomic artwork boundaries remain explicit.

### Whole-item sizing refinement (2026-11-15)

Application **`f570b5923e1f7e123a850302d66b6df2e9ae18c0`** is selected and
published on `new-test`, after independent final **PASS — READY** verification.
Fresh affected gates pass **2137 unit / 1207.76 s**, **327 browser / 881.02 s** and
the separately invoked **12 original-navbar checks / 38.08 s**. Offline/syntax
checks pass; private roadmap-map checking is unavailable, not accepted.

The operator clarified that resizing must change the **whole displayed item** so
more or fewer items fit, superseding the earlier fixed-density producer decision.
The bounded `density-v1` implementation resizes Library articles (including
holdings/editions), complete local Music tiles and all Home shelf slots. Grid
reflow and narrower shelf slots change visible density, not the fetched dataset,
order, filters, counts or pagination. Fonts and artwork proportions are preserved;
readable floors, reset and document-local 50–100% controls remain. Series/Browse,
account preferences/CAS and above-default enlargement remain outside this increment.

An independent design review's finite corrections were ratified before productive
edits. Original 435 test files and acceptance185 remain exact; legacy component
tests remain unchanged. Only post-original producer assertions for the superseded
fixed-host contract transition, retaining case identities and independent checks.
The fresh qualification preserves every prior 2115/300/12 case identity/outcome
and adds 22 unit / 27 browser cases. All 945 files/modes match the gated candidate;
all 18030 retained evidence hashes were independently checked. Earlier failures
remain retained, not waived. Badges 10913/728 count collection, not full-suite
passes; no programme, AT, pinned-runtime or provider acceptance is inferred.

Independent architecture, requirements, privacy and recovery audits accompany
implementation. Additional reviewers/testers are assigned to completed
candidates, not used as substitutes for implementation ownership.

## Non-negotiable integration decisions

- Preserve the catalogue, physical copies, identifiers, locations, personal
  activity, original legacy data, existing integrations and external keys.
  Do not alter the operator's ports, volumes, network or Portainer setup.
- Keep FastAPI/HTMX/SQLite and a single container; no mandatory search, queue or
  frontend service. Keep spacious desktop layouts, the unchanged **80px**
  control-width gate and administrative/bulk Browse under Manage.
- All persistent schema belongs to `app/database.py`. Integrate additive
  tables and append-only migration changes centrally; no lazy request DDL or
  independent conflicting migration numbers.
- Existing data/accounts become the initial household without silent access
  changes. Distinguish installation administration from library capabilities.
  Extra households and external account provisioning cannot be activated until
  server-side isolation passes the legacy-surface census.
- Catalogue visibility must cover direct IDs, mutation funnels, Trash, exports,
  counts, auxiliary names, source actions, public shares, artwork and browser
  caches—not just Library templates or live-view reads.
- Network fetches do not hold SQLite write locks. Recheck authority, source
  revision and stale review evidence immediately before applying results.
- Schedules, remote progress connections and external notifications require
  explicit configuration/consent. Implementation permission does not authorise
  production deployment, use of production credentials or remote progress writes.
- Playback/readers/transcoding/DRM are outside the agreed catalogue-and-launcher
  scope. An external link is not evidence of playback, remote access or resume.

## Media-experience refinement (2026-11-14)

The [canonical ME1–ME7/ST1 requirements](connected-library-plan.md#operator-media-experience-refinements-2026-11-14)
require one primary Browse click to open the complete authorised mixed live
catalogue, with nested family/format disclosure usable by hover, click/touch and
keyboard. Remove duplicate top-level media links, not capabilities or the
administrative/bulk Browse destination under Manage.

The same scope requires an accessible shared Plex-style pop-out editor with
common fields, media-specific sections and full-page fallback; reusable
artwork-led cards (including music, not text-only bars) with native format
proportions, not uniformly square artwork; one horizontal recent row
with View more; and account-owned mixed/family/format/custom grouping. Shared
partials and a media presentation registry extend the existing server-rendered
framework, not a separate SPA. Permissions, validation, private activity,
catalogue/copies/history, integrations, keys and original data must survive.

**ME7 — cover-size slider** remains an unfinished F2 backlog item: provide live
cover/card resizing, keyboard access and a default-size reset while preserving
artwork proportions and responsive layouts. The reviewed first-wave component
and fixed-slot decision above are not yet primary producer integration.
Persistence belongs with the existing account/device preference work. Previous
media and navbar test results do not close this new requirement.

The operator explicitly selected **Steam library, launch/store links and
playtime**. Reported playtime remains separate source data and must never
overwrite personal status/progress/history. The selected incremental contract
does not transplant the unaccepted F2 `10560d5` kernel/schema assumptions.
Reviewed registry/cards/strict filters, shared full-page editor fields and proof,
compatibility migrations, Home/Music/Library adoption, shell wiring and the primary
Browse shortcut are integrated in **`916a4dd6`**, including generated assets.
The editor is not yet the required dialog;
pure recent-preference helpers are not account-owned persistence/CAS or settings UI.
The seven separate [parent media gates](connected-library-verification.md#parent-frozen-media-gates)
are supplemented by a fresh **1935-case / 415.19 s** composed affected gate, not
summed or transferred. This is not browser interaction or full-UI acceptance.

Steam remains **UNREGISTERED**, without account-private observation, pacing or
actual Sources/store/launch acceptance. Its latest standalone gate is RED at the
unchanged forced-INFO logging boundary; the reviewed host policy passes only its
separate isolated contract. See [Steam and host policy](connected-library-verification.md#steam-and-host-policy).
Public documentation and in-memory protocol proof are not TLS/live-account
evidence. Private/missing enumeration is not complete empty enumeration. Preserve
full database-backup fidelity and external keys; no blanket Steam exclusion is
approved. New English UI remains part of R9 convergence.

## Research intake (2026-11-14)

The [canonical research additions](connected-library-plan.md#research-additions-2026-11-14)
record **thirteen functional/visual candidates (RF1–RF13)** and **four integration
feasibility candidates (RI1–RI4)** following the operator's request to research
and add useful gaps to the list. **No implementation, provider/app registration,
account connection, production access, push or deployment is authorised by this
intake.** P1/P2/P3 are proposed ordering, not assignments or delivery promises;
existing safety blockers and F1/F2-first ordering remain in force.

Read-only source inventory against `f637f475` distinguished missing subcapabilities
from existing features, and separate official-public-documentation research
assessed integration feasibility on 2026-11-14. These are not tests or live-provider
receipts; experimental work elsewhere was not certified by this survey. The plan
holds the detailed source links, references, boundaries and uncertainties.

The follow-up source pass against `36fc4423` adds RF11–RF13 and sharpens RF8/ME2
after checking existing copy/location/import/editor behaviour and further public
Libib, Collections Trust and versioned Snipe-IT documentation. This is additional
research-only intake, not implementation or a new operational/test receipt.

| Intake group | Extend existing ownership if selected | Dependency / current state |
|---|---|---|
| RF1 appearance/card metadata; RF2 command palette | F2/ME6 with X4/X5; coordinate RF1 with ME7 | Theme/display controls and palette proposed, not implemented; existing cards, Browse columns, shortcuts and cover-slider backlog retained. Reuse account/device preference rules |
| RF3 composable queries/smart shelves | F1/F6 | Extend current filters and own saved views; bounded query/paging, same-member/holding semantics and permission-scoped counts remain prerequisites |
| RF4 ordered mixed-media plans | F2/F6, then F7/R3 for collaboration | Existing My List, private progress and public collection/wishlist links are not missing. Curated ordering/collection UX is proposed; sharing cannot bypass access-design gates |
| RF5 typed custom fields; RF6 receipt/evidence files | F5/F6 with F7/F8/X3 | New field-definition and attachment subcapabilities require permissions, central migration design, safe storage/parsing, retention and backup/export contracts. Copy condition/acquisition/provenance already exist |
| RF7 inbox and notification preferences | F2/F3/R8 with F7 privacy | Reuse current ntfy/webhook sender and reminder/price/release features; durable account inbox/read state and preferences are proposed, not a second alert engine |
| RF8 standard metadata/list codecs and CSV mapping/review | F8/R1/X3 | Extend CSV import/export, staged archive imports and typed read-contract foundations; add reusable column profiles and pre-apply/result review without claiming existing CSV staging or missing row-error reporting. Choose formats from actual samples, document loss/exclusions and test safe matching/round trips. Not every researched format is promised |
| RF9 spoiler/advisory controls; RF10 local pick/suggestions | Optional later F2/F6/X5 and bounded F1 queries | Source-derived gaps, not missing notes or Up Next. No parental-access claims, cloud AI, shared taste profiling or automatic launch |
| RF11 exact-copy shelf locator | F2/F5/X4 | Reuse hierarchical locations, copy positions and Arrange; add targeted highlighting/neighbour context with a text alternative. A saved location is not proof the copy is still there; handle unknown, moved and missing copies honestly |
| RF12 sale/donation/disposal ledger | F5/F8/X1/X3 | Separate historical copy disposition from Trash, missing/found and personal reading history. Ownership/count/availability/loan conflicts, private details, correction and complete recovery need explicit design before implementation |
| RF13 boxed-set/accessory checklist | Optional later F5/F6/F8 with RF6/X1/X3 | Per-copy physical contents are not series completeness or music track/disc metadata. Define expected/present/missing/unknown parts and preserve child state without duplicate ownership counts or automatic item merging |
| RI1 Plex; RI2 Navidrome/OpenSubsonic | Source jobs/handoff/media hierarchy: F3/F4/F5 | Documented API surfaces warrant feasibility work only. Confirm operator use, named versions/auth, permissions, identity, safe credential transport, complete enumeration and credential-free handoff before selecting an adapter |
| RI3 Trakt own-data import/observation | F4/F5/X3 with account/privacy design | Prefer a sufficient user export; API/app access, OAuth/token rotation, endpoint eligibility, consent and retention remain unqualified. No scrobbling or automatic personal-progress/history overwrite |
| RI4 Playnite export intake | Existing game imports, ST1/F3/F4/X3 | Exporter/schema/version/sample and GUID handoff need validation. Direct GOG account enumeration is BLOCKED for lack of an established official public interface; no cookie/private-API/database-scraping substitute |

Source-run history/capability presentation strengthens **F3/F4**; non-merging
edition comparison strengthens **F5/F6**; clearer bulk scope/outcomes strengthens
**F2/X1/X4**. WCAG 2.2 interaction/reflow/focus/target-size work strengthens the
existing shared UI acceptance rather than opening a parallel redesign. KOReader
metadata is parked as an F4/RF8 format/identity investigation, not working Kobo or
device sync. These distinctions avoid duplicate projects.

Previous/Next and Save and next refine **ME2/F2/X4** for a bounded editing
session; they do not replace native editor fallback or its unsaved/stale-input
protections. The follow-up does not add another location system, scanner,
missing/found tracker, series checker or import engine.

If a candidate is selected later, define its acceptance and access/backup/privacy
dependencies before assigning implementation. Preserve native fallbacks, the
complete mixed Library and management Browse, existing catalogue/copies/history,
all integrations and keys. Do not expose private source observations through the
currently shared catalogue or infer a read-only credential scope from read-only
calls. Mock/local fixtures, named live-provider evidence, pinned-runtime acceptance
and rollout remain separate decisions. No existing acceptance checkbox or test
result is changed by this documentation-only intake.

## Audit findings promoted to release blockers

1. A live-view household filter alone does not protect legacy raw mutations,
   globally named auxiliary records or the public static artwork mount.
2. Independent households need deliberate identifier/namespace constraint and
   duplicate-adoption handling; hidden global collisions must not reveal or
   restore another household's items.
3. Database restore must migrate and integrity-check an isolated upload before
   replacing the working database. Rejected uploads must leave live rows,
   sessions, artwork and keys unchanged.
4. General merge undo needs losing and kept rows, reparented/cascaded children
   and artwork decisions. Current metadata provenance is not an undo journal.
5. A scheduler over a permanently truncated source is not complete large-source
   support. Absence reconciliation requires a terminal, warning-free enumeration.
6. Existing Store/browser storage is not account-isolated. Account switching
   must never replay another person's queued changes.
7. Search indices must remain compatible with backup/restore's rejection of
   arbitrary triggers/views/virtual tables, and always join authorised live items.

## Verification environment and rollout

Historical local browser gates used the compatible Python 3.14/native Chromium
environment, not pinned Python 3.12/Playwright 1.52. At this capture the parent's
standard-PATH check finds no `Chromium`, `chromium-browser`, `google-chrome` or
`python3.12`; fresh native-browser and pinned-runtime runs are **blocked**.
Compatible Python 3.14 and Node remain available. REA tool-catalog discovery is
not an executed browser session. Historical browser evidence remains valid on
its identified candidates; see the
[verification record](connected-library-verification.md#runtime).
Pinned CI/container evidence must be recorded separately. No Docker publication
workflow or production rollout is part of worker execution.

Run focused tests per lane, then freeze the integrated implementation before
full unit, browser, migration, privacy, recovery and scale gates. At `916a4dd6`,
CSS/catalogue/service-worker/badge generation converged twice, offline checks
passed and the composed affected gate passed. Generation does not establish
translation completeness, computed layout or feature acceptance. Publication
requires independent source/evidence and documentation review; do not mark the
programme finished while acceptance criteria or operational blockers remain open.

### Current local checkpoints (documentation capture)

The verified media source checkpoint is **`916a4dd6`**. The remote checkpoint
before this increment was **`b9c5fbec`**. This is a bounded implementation/evidence
record, not feature closure, programme acceptance or deployment. The fresh
**1935-pass** affected composition and generated outputs have their own receipt;
core, host/Steam and portability remain isolated from this media source.
Detailed results and historical attribution are in the
[verification record](connected-library-verification.md#local-follow-up-candidates-documentation-capture).

- **Historical source:** the earlier `f3809d6` / `89388e31` checkpoint retained
  source integration `1cf3c0b6`. Frozen `1767d0cb` completed the 31,400-record-per-source
  known-schema benchmark and **422 browsers**. Its full units were **RED: 9301
  passed, one stale-badge failure and four offline-fixture teardown errors**.
  Test/README-only `89205f44` repairs those fixtures and stamps collection at
  9302; **61 focused tests and checks-fast passed** (coordinator report). Its
  full-unit rerun passed **9302 tests**, with no failures/errors/skips, in
  2006.04 s. App/static/E2E/harness equivalence to `1767d0cb` and 782 committed
  input blobs were verified after completion. The independent promotion gate
  passed **27/27 evidence checks**: all **822 non-documentation files** match
  `89205f44`, all **866 tracked files** and three full archives match their
  respective commits/modes. No unit/browser/benchmark rerun occurred at merge.
  The reviewed 64-record RSS diagnostic does not measure original full-size RAM.
  Test-only `89388e31` closes the music wishlist positive-membership coverage
  gap: **28 tests passed**, and a membership-suppression negative control fails
  at the new assertion while ownership/partition checks still hold.
- **Media:** `916a4dd6` passed **1935 affected cases / 415.19 s**, zero failures,
  errors or skips; all **895** input hashes/modes and both guards match, with zero
  child blocked attempts. It retains all 1687 unique prior affected cases and adds
  248 asset/shortcut/script controls. **b** follows the rendered authorised Browse
  href. Three separately enabled prior-baseline controls passed on the same
  candidate; their historical Tags/creator-JSON/Music-DNS failures belong to other
  snapshots, not a media fix or waiver. Dialog, account-owned preferences, browser,
  pinned-runtime and scale acceptance remain open; this is not a full-unit pass.
- **Core/recovery:** isolated `343c257` is **CHANGES_REQUIRED**: discarded callback
  return destructors run outside suspension, and pending same-cursor window
  finalization after FINAL admission permits late publication despite revocation
  and swallowed errors. Parent **340 passed / 50 failed / 656.51 s** reproduces the
  same 390-case failure IDs. Reviewed finalization and invocation packets separately
  report **35 passed / 9 valid HIGH failures / 106.35 s** and **13 passed / 3 failed /
  46.65 s**; the latter has two durable-write witnesses, not three. Original
  **28 K13 + 17 D3 proof dependencies + 5 baseline failures** remain enabled.
  Later `53e1fecd` passed all 60 additive controls in a parent **400 passed / 50
  failed / 731.21 s** run. The original HIGH paths are corrected, but source
  rereview is **CHANGES_REQUIRED** for retained failed adapter results and clearing
  unrelated caller traceback frames. Repair and key/binding/seal/alias decisions
  remain open; the census also identifies a real retirement-workflow conflict.
  DENY-only/observe-only test seams grant no productive authority. Historical
  freshness/e0/replay/feasibility gates retain their original scope. Actual F8
  cold-start `identity_provider_epochs` admission remains blocked. See the
  [core record](connected-library-verification.md#current-isolated-core-review).
- **R2:** `d799a29e` versus `caf76382` adds a contract and reproducers, not a
  productive private writer/startup implementation. Its combined gate remains
  **5 failed / 6 passed**; **11** separate positive/denial controls pass. A real
  password POST commits before returning 500; the chosen browser receipt remains
  stopped. The first milestone has **four proposed finite APIs and 17 future
  acceptance cases**. The R2 owner agrees **Q1/Q2/Q4/Q6 only**. A persisted
  Q3/Q5/Q7 proposal now covers fixed crypto preparation/core-only registration
  for all 14 key instances, separate signing/browser prerequisites and registered
  ASGI lifespan, not a Python sandbox. Q3/Q5 technical/signing-race, post-file
  conflict and NULL-fallback decisions remain pending; Q7–Q9 are unapproved.
  Actual 6d lacks browser/grant prerequisites; no DDL/kernel/main transplant or
  productive identity/startup approval follows from the separate core approval.
- **R9:** the frozen `b049de8a` baseline ledger remains **219/219 exact IDs,
  186 source-closed / 33 open / 0 unfinalized**. Its **3017** mechanical assertions
  and the later `582db82d` **1190-case / 225.15 s** affected union are bounded,
  not latest-media/language/browser acceptance. Earlier gates remain attributed
  in the [parent table](connected-library-verification.md#parent-frozen-follow-up-gates).
  Central committed i18n generation reports 22 new empty English-fallback msgids,
  two retired old neutral wordings and unchanged same-key translations; exit 0
  does not mean all translations complete. Historical **4718/798** preservation
  and GNU sentinel checks are not fluency. Rename/hidden-count policy is unchanged;
  native creator-canvas nontruncation and whole-R9 acceptance remain unverified.
- **Portability:** isolated `a1b9d37` has bounded physical-ownership source
  clearance and a parent **109-pass / 54.19 s** gate, superseding the two-gap d6
  repair-WIP statement. RSS `5be0fbc` is **RED: 332 passed / 2 failed / 240.09 s**:
  a new calibration cross-counter assumption and unchanged precommit phase-B
  artwork. The original PNG/raw **128 MiB** cap case passed, without waiver.
  Separate `/proc` observations and retained `getrusage` peak do not prove the
  kernel mechanism or full-size RAM acceptance. Reviewed calibration-only
  `b5f6aad9` then passed **31 image cases / 23.72 s**; fresh full parallel and
  serial gates each remain **333 passed / 1 unchanged phase-B failure**, in
  **239.18 / 419.88 s** respectively. No universal constructor/signal/
  orphan ownership, overall phase-A/X3/F8/pinned pass or phase-B approval follows.
- **Browser/offline:** `36260af1` has bounded source clearance for all-status
  ownership, safe 429 handling and falsy aborts, excluding password receipts and
  current-core Store/PWA acceptance. Offline `e8ae0890` after `1f5ad79` is bounded
  source-review clear for independent retirement attempts and truthful retry/
  replacement barriers. Its **42 browser / 63 server** worker passes use the
  canonical **830-only** composition. Strict 362/current-core, Store/PWA and
  password-receipt independent gates remain pending; prior broader RED results
  are not reclassified. Source gates do not close these lanes.

All **F1–F8, R1–R10 and X1–X6** requirements remain in scope. All **185** original
acceptance checks remain unchecked and household/graph activation stays **OFF**.
Actions settings are unchanged under the operator's **continue local
only** decision.
Pinned Python 3.12/Playwright 1.52/container, named live-server/IdP/provider and
licence acceptance remain unverified. Direct Audible access stays default-off;
a configured reference is not a licence. No deployment, production/Portainer,
key or operator-data change is authorised or evidenced by these local gates.
These three documentation updates do not change operator data, external keys,
workflows or deployment configuration. Referenced parent executions are attributed
separately; no install, live-provider test or deployment is implied.

### Starting-point evidence

- The unchanged `b336939` application passed **7669 unit/integration tests**
  in 1375.28 seconds using the compatible local runtime. This is a baseline,
  not verification of any new implementation lane.
- The first browser invocation failed during Playwright setup because the
  fallback installation needs an explicit Node path. It was restarted with
  the documented Node/native Chromium environment and passed **405 browser
  tests** in 1534.43 seconds. No application fix was inferred from that setup
  error. The application/static/test implementation remained unchanged during
  both baseline suites; only delivery documentation/workflow setup changed.
- `3e60c99` adds a read-only, non-publishing acceptance workflow for pushes to
  `new-test`: pinned-runtime unit/browser gates and production-image schema
  checks. Workflow configuration is not an executed CI result. No container
  publication workflow was dispatched.
- GitHub's fork-level execution gate reports that workflows are paused because
  of Actions usage volume. Read-only API checks found no run/check suite for
  the acceptance push, despite enabled repository permissions. A maintainer's
  explicit re-enable decision or an authorised external runner is required;
  no execution setting was changed and no pinned-CI pass is claimed.
- The first integrated focused gate on `d91255d` reported **1158 passed and
  two failures** in 261.29 seconds. Both failures were the navigation registry's
  exact inventory assertions needing the two deliberately added destinations.
  Independent review also found uncovered correctness defects; their fixes
  and new regression tests remain required even if this focused suite passes.

### First application integration pushed

`a4103d4d` was pushed to `new-test`, without deployment or image publication.
It contains the versioned read API, reading-life additions, reviewed source
replacement/repair and BN Poland provider, with central registration, native
form bounds, navigation and browser/layout coverage. This is an incremental
integration, **not closure of the full backlog or its dependent acceptance gates**.

- `c7fe681f`: **1260** focused unit/integration tests and **27** browser tests.
- `3cbecaa4`: complete local unit/integration suite, **8158 passed** in
  1642.67 seconds. The subsequent browser gate found three integration issues;
  all eight widths passed the corrected number-control geometry checks.
- `a4103d4d`: after correcting the opening labels/fragment location and no-op
  review continuity, **1264** focused tests passed in 276.06 seconds and
  **21** browser/layout tests passed in 510.92 seconds. Fast offline checks pass.
- `a4103d4d`: the expanded full browser suite passed **417 tests** in
  1741.12 seconds. The original full 8158-test unit result belongs to
  `3cbecaa4`.
- `38c73f97`: a subsequent frozen full unit run passed **8162 tests** in
  3265.91 seconds. Its application/static/test code is unchanged from
  `a4103d4d`; only delivery documentation differs. This verifies the first
  application batch, not any unmerged implementation branch. No full
  expanded-programme gate is claimed.
- `29edfaf9`, a separate source-delivery candidate: complete non-live units
  finished **8996 passed / 1 failed**. Its availability expectation was updated
  in test-only `86879998` to verify viewer-owned preferences and safe handoffs
  while still refusing catalogue edits; **118 focused tests passed**. Later
  source-repair composition `7e86d0a4` passed **565 focused tests**. These are
  historical focused checkpoints; later full browsers/benchmark belong to
  `1767d0cb` and full units to `89205f44`. These were local checkpoints at the
  time; the later source promotion is recorded above.

The operator chose **continue local only** when offered pinned CI/runner
options. GitHub Actions execution settings remain unchanged. Python 3.12,
pinned Playwright and production-container acceptance remain blocked, not
passed. Named live-provider and IdP evidence remains separately incomplete.

### Historical translation checkpoints

All 53 initial translation maps received structural validation: **4154 entries,
4223 forms**, with no placeholder/plural/whitespace errors. Eleven independent
language reviews supplied 15 corrections and flagged source-context problems.
Secondary-only catalogue candidate `f89df61c` applied **3922 exact matches**,
preserved **798 existing translations** and retired **232 mappings** without
reusing fragments. Subsequent reviewed source fixes `2fa9ff54` / `7caf79c1`
repair attribute escaping, false enum extraction and the TMDb grammar fragment.
Catalogue candidate `9a15c983` applies **328 independently reviewed fresh
identities / 357 forms**, including four Chinese corrections, while preserving
all **4718** then-current translations and the original **798**. Independent
exact-map/placeholder/four-MO checks and bounded source/catalogue review clear
those checkpoints only. Two new TMDb locale identities remain empty there.

The source audit against `2fa9ff54` recorded **70 genuine records / 55
identities**, including the subsequently repaired TMDb case. The remaining
count at that checkpoint is historical, not today's open-audit count. Current
scan/library/settings semantic and residual repairs are tracked above.
Retirement is not semantic closure; new source messages and later merged feature
strings require fresh extraction, language review and integrated gates. These
historical catalogue gates do not establish publication or default activation of
the current generated outputs. **R9 remains incomplete.**
