# Connected library delivery programme

The operator authorised implementation of the entire
[consolidated backlog](connected-library-plan.md#remaining-library-frontend-work)
on **2026-11-13**, including the cross-device and offline refinements. Work
starts from `b336939` on `new-test`. The earlier backlog's optional ordering
does not mean those items have been delivered or excluded from this request.

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
| Front door | F2 shelves, creators, collections, preferences and navigation | Candidate review/fixes; household composition open |
| Source jobs | F3 scheduling, progress, retry/recovery and large-source boundaries | Streaming/lease integration and runtime verification open |
| Handoff/progress | F4 preferred source and explicitly linked account progress | Next-batch integration; scheduled-pull acceptance open |
| Media hierarchy | F5 works/editions/copies, music and source children | Atomic child hook prepared; production composition open |
| Artwork/review | F6 source artwork, TV artwork and bounded reviewed batches | Candidate/fix review; actual job fencing open |
| Household access | F7/R3 ownership, invitations, visibility and legacy enforcement | Security fixes plus six legacy-port lanes; activation off |
| Recovery | F8 restore preflight, integrity and synthetic recovery evidence | Candidate/fix review; maintenance and identity composition open |
| Read API | R1 documented, typed, paginated read contracts | First integration pushed; household/operational gates open |
| Identity | R2 trusted proxy login and OIDC with local recovery | Actual-core write/startup admission blocked; live IdP acceptance open |
| Borrowing | R4 authorised owner search, requests and lending ledger | Lifecycle/scan-race fixes; real household adapter open |
| Gifts | R5 recipient-safe concurrent gift claims | Candidate/fix review; backup/history/household composition open |
| Reading life | R6 repeat sessions, journal and annual goals | First integration pushed; portability/household gates open |
| Audiobook enrichment | R7 exact-ASIN marketplace-aware review/backfill | Next-batch integration; provider-permission/coverage gate open |
| Author discovery | R8 account-local following and deduplicated releases | Alias/revocation fixes; scheduler/household composition open |
| Translation | R9 existing surfaces, followed by an integrated new-string pass | Reviewed scan/settings composition gated; Library count repair and language acceptance open |
| National providers | R10 additional documented, permitted provider coverage | BN Poland first integration pushed; final operational gates open |
| History/undo | General change history, supported rollback and merge snapshots | Integrity fixes and later-table registry composition open |
| Source repair | Reviewed replacement/rebinding and non-destructive retirement | Known-schema integrated scale gate passed locally; broader authority gates open |
| Portability | Versioned music/account-data export/import and round trips | Disposal follow-up still requires changes; thread ownership/final-artwork/phase B open |
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
  frontend service. Keep spacious desktop layouts and administrative Browse.
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

Local fallback verification uses the existing compatible Python 3.14 environment
and native Chromium; it is not the pinned Python 3.12/Playwright 1.52 runtime.
Browser commands require the documented Node/native-browser environment in the
[verification record](connected-library-verification.md#runtime).
Pinned CI/container evidence must be recorded separately. No Docker publication
workflow or production rollout is part of worker execution.

Run focused tests per lane, then freeze the integrated implementation before
full unit, browser, migration, privacy, recovery and scale gates. Regenerate
CSS/service-worker stamps, locales and badges before freezing. Push tested
integration commits incrementally; do not mark the programme finished while
acceptance criteria or operational blockers remain unresolved.

### Current local checkpoints (documentation capture)

The latest published test follow-up is `89388e31`; its application remains
unchanged from source integration `1cf3c0b6`, after `fdd51186`, incorporating
tested `89205f44`. The earlier `2fb43756` / `717dfeb6` push checkpoint is
historical. Core, R9, portability and offline
follow-ups remain separate and local. Full evidence, candidate boundaries and
local artifact paths are in the
[verification record](connected-library-verification.md#local-follow-up-candidates-documentation-capture).

- **Source:** frozen `1767d0cb` completed the unchanged 31,400-record-per-source
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
- **Core/recovery:** `a850b2a7` has bounded freshness source-review clearance and
  a coordinator-reported **128-test** gate. SQLite composition `29ee407a` picks only
  `d9051b88` as `4d1f` atop it; actual restore → cold restart → token-version-38
  setup remains blocked by `identity_provider_epochs` startup admission. The
  bounded e0 **328-test** pass and R6 `8c4d1aae` **126 passed / 3 failed** retain
  their original scope. `42927e71`'s **107 failed / 10 passed** belongs to the
  historical e0 component, not the actual façade. Exact original-test replay
  `6d2906d2` on unchanged actual `29ee407a` is **RED: 92 failed / 25 passed**,
  with no setup/API/guard errors. Independent read-review corroborates that RED
  and its classification; production/API-compatibility readiness remains
  **CHANGES_REQUIRED**. Protected predicates/getters and callback privacy are not
  fixed. A sole façade writer and separate test-only reentry lane are reserved;
  the compatibility proposal is not production-repair approval.
- **R2:** `d799a29e` versus `caf76382` adds a contract and reproducers, not a
  productive private writer/startup implementation. Its combined gate remains
  **5 failed / 6 passed**; **11** separate positive/denial controls pass. A real
  password POST commits before returning 500; the chosen browser receipt remains
  stopped. The first milestone has **four proposed finite APIs and 17 future
  acceptance cases**. The R2 owner agrees **Q1/Q2/Q4/Q6 only**; Q3/Q5 technical
  acknowledgement is pending, and Q7–Q9 remain unapproved. This is partial domain
  agreement, not implementation approval; Q8's actual-core prerequisite is unfixed.
- **R9:** the historical `b049de8a` **771-test** checkpoint remains bounded.
  Reviewed settings chain `19f7fa94` passes **18 strengthened cases**; reviewed
  scan/settings composition `f52884aa` passes a new parent **946-test union**,
  not reused worker results. Library `d4d7992a` → `c4b4d57e` is source/test-quality
  review-clear only: **52 passed / 1 failed**, broader **539 passed / 9 failed**.
  The operator chose **live-only visible unlink**, preserving Trash,
  inaccessible entries, copies/history and private information. Its narrowly
  approved follow-up is not passed or composed; rename policy and hidden-count
  disclosure are not changed. Test-only `6bd04bda` adopts that expected set and
  reports **161 passed / 1 failed**, stopping at missing watch-retention
  capability. Metadata GC is correct, but watch-only entries without metadata
  must retain hidden references. The internal conditional watch-GC helper,
  exact lint entry and live-view target update are now narrowly approved and
  active, not a passing/current-core gate. Four persisted baseline-audit batches
  cover **160/219 exact IDs: 142 source-closed / 18 still-open** at frozen
  `b049de8a`; **59 remain unfinalized**, not 59 established defects. The final
  two bounded batches are active, with no finalized result at capture. A separate
  dismissed-duplicate pair follow-up addresses a surviving whole-message concern;
  fixing that multi-location finding's CSV text alone is insufficient.
  Later repairs require reviewed source
  overlays, not retroactive baseline counts. Catalogues/assets are stale; two
  reviewed Italian/Chinese TMDb drafts remain unapplied. Semantic convergence
  must precede fresh English extraction/authorship, language review and central
  regeneration; historical 4718/798 preservation is not language completion.
- **Portability:** phase-A `93cc16d1` remains historical. Frozen disposal
  follow-up `6d0f4407` reports **53 passed**, **271 passed / 5 failed**, **412**
  and **429** worker passes. Rereview supports the bounded disposal/inventory/
  corruption-fixture repairs but remains **CHANGES_REQUIRED**: completed-form
  close after reservation exit can mask a downstream primary failure. Its
  bounded repro/fix and contract refinement are active. Four real threaded-I/O
  cancellation failures remain; two registered-app final-send passes are not
  I/O-drain proof. Broader thread implementation and phase-B operational DB
  tables/incarnation/final-artwork reaping are unapproved; no overall X3 pass.
- **Browser/offline:** `36260af1` has bounded source clearance for all-status
  ownership, safe 429 handling and falsy aborts, excluding password receipts and
  current-core Store/PWA acceptance. Offline `e8ae0890` after `1f5ad79` is bounded
  source-review clear for independent retirement attempts and truthful retry/
  replacement barriers. Its **42 browser / 63 server** worker passes use the
  canonical **830-only** composition. Strict 362/current-core, Store/PWA and
  password-receipt independent gates remain pending; prior broader RED results
  are not reclassified. Source gates do not close these lanes.

All **185** acceptance checks remain unchecked and household activation stays
**OFF**. Actions settings are unchanged under the operator's **continue local
only** decision.
Pinned Python 3.12/Playwright 1.52/container, named live-server/IdP/provider and
licence acceptance remain unverified. Direct Audible access stays default-off;
a configured reference is not a licence. No deployment, production/Portainer,
key or operator-data change is authorised or evidenced by these local gates.

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
strings require fresh extraction, language review and integrated gates. None of
these catalogues is pushed or enabled by default. **R9 remains incomplete.**
