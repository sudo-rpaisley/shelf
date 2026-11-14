# Connected library delivery programme

The operator authorised implementation of the entire
[consolidated backlog](connected-library-plan.md#remaining-library-frontend-work)
on **2026-11-13**, including the cross-device and offline refinements. Work
starts from `b336939` on `new-test`. The earlier backlog's optional ordering
does not mean those items have been delivered or excluded from this request.
The **2026-11-14 ME1–ME6/ST1 refinements** below extend that programme; they do
not replace the remaining F/R/X requirements or inherit historical test passes.

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

Implementation agents use isolated `swarm/20261113-*` branches/worktrees.
The coordinator integrates their commits, resolves shared schema/registration
changes, regenerates assets, runs combined gates and pushes `new-test`.
Workers do not push or deploy independently. Worktrees are not production
data directories and must never be seeded from the operator's live catalogue.

| Lane | Backlog responsibility | Evidence state, not completion |
|---|---|---|
| Search | F1 indexing, ranking, bounded grouping/paging and scale measurements | Candidate/fix review; synthetic 31,200-record measurements |
| Front door | F2 shelves, creators, collections, preferences and navigation; ME1–ME6 | Frozen `916a4dd6` media composition: 1935 affected passes; dialog/preferences/browser/scale gates open |
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
| Translation | R9 existing surfaces and new English UI, including ME1–ME6/ST1 | Bounded source gates and complete baseline ledger; final composition/language/browser acceptance open |
| National providers | R10 additional documented, permitted provider coverage | BN Poland first integration pushed; final operational gates open |
| History/undo | General change history, supported rollback and merge snapshots | Integrity fixes and later-table registry composition open |
| Source repair | Reviewed replacement/rebinding and non-destructive retirement | Known-schema integrated scale gate passed locally; broader authority gates open |
| Portability | Versioned music/account-data export/import and round trips | Bounded ownership and `b5f6aad9` calibration clear; full gates remain RED for phase-B artwork; X3 open |
| Offline reference | Explicit bounded, account-isolated offline reference browsing | Bounded retirement source review clear; strict-browser/current-core composition pending |
| Browser privacy | Account/library-scoped Store queues and navigation storage | Bounded error/abort source review clear; password/current-core Store/PWA open |

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

The [canonical ME1–ME6/ST1 requirements](connected-library-plan.md#operator-media-experience-refinements-2026-11-14)
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
