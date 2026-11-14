# Connected-library experiment: verification

Branch: `new-test`, based on `100219d`. This is an experimental branch, not a
release or a claim of compatibility with every OPDS/Jellyfin server.

## Runtime

Recorded local gates used Alpine aarch64, Python **3.14.8**, and the isolated
interpreter `/tmp/shelf-new-test-compatible-venv/bin/python`. For those runs,
all direct Python requirements matched the repository pins **except Playwright
1.65.1 instead of 1.52.0**. The pinned Playwright package could not be installed
on that runtime. Historical browser runs used native Chromium **155.0.8059.84**
and `/usr/bin/node`.

At the current capture, the parent's standard-PATH check finds no `Chromium`,
`chromium-browser`, `google-chrome` or `python3.12`. Compatible Python 3.14 and
Node remain available, but fresh native-browser and pinned-runtime runs are
**blocked**. REA tool-catalog discovery is not a browser launch or an executed
session. This does not invalidate the historical **422-browser** result at
`1767d0cb`; browser command examples below record historical invocations, not
present executable availability. This documentation task performed no installs
or environment changes.

These results are **not pinned-CI verification** (the project CI uses Python
3.12 and its pinned Playwright browser). Dependency manifests were not changed
to accommodate the local environment.

## Full-backlog delivery: first integration (2026-11-13)

The expanded programme is tracked in the
[delivery record](connected-library-delivery.md) and
[acceptance checklist](connected-library-acceptance.md). These are local
incremental results, not completion of all F/R/X requirements.

The first application integration was pushed as `a4103d4d`. It includes typed
read APIs, reading sessions/journal/goals, source replacement/repair and BN
Poland metadata, with registration, native-form bounds, navigation and layout
coverage. Subsequent implementation lanes remain outside this candidate.

| Frozen application candidate | Gate | Result |
|---|---|---|
| `c7fe681f` | Combined focused unit/integration set | **1260 passed**, exit 0, 269.61 s |
| `c7fe681f` | Reading-life, Connected Library and navigation browser journeys | **27 passed**, exit 0, 101.26 s |
| `3cbecaa4` | Complete non-live unit/integration suite | **8158 passed**, exit 0, 1642.67 s |
| `a4103d4d` | Combined affected-feature/schema/legacy/privacy regression set | **1264 passed**, exit 0, 276.06 s |
| `a4103d4d` | Reading-life, delivery and all-page/eight-width layout browsers | **21 passed**, exit 0, 510.92 s |
| `a4103d4d` | Complete expanded non-live browser suite | **417 passed**, exit 0, 1741.12 s |
| `a4103d4d` | `make checks-fast`, whitespace checks | Passed; private roadmap-map lint explicitly skipped because `.devdocs` is absent |
| `38c73f97` (application unchanged from `a4103d4d`) | Complete non-live unit/integration suite, frozen sibling worktree | **8162 passed**, exit 0, 3265.91 s |

The `3cbecaa4` browser run initially reported **15 passed / 3 failed**:
opening-action labels, a macro incorrectly placed among top-level pages, and
the retirement journey needing a fresh inventory review. That strict journey
then exposed no-op apply superseding an earlier repair's rollback authority.
The later candidate corrects these issues without weakening the layout gates,
stale-inventory checks, real-binding ABA guards or metadata-authority protections.
Its full browser pass includes the unchanged 80-pixel content-width threshold.

The original full 8158-test unit result predates those final small fixes;
the later 1264-test gate covers the affected features. A subsequent frozen
run at `38c73f97` independently passed all **8162** units, including those fixes.
That commit changes only delivery documentation relative to `a4103d4d`.
Neither full result verifies any unmerged programme candidate. Warnings were
existing Starlette/httpx, deliberate duplicate-ZIP-fixture and browser-stack
deprecations; no blanket warning suppression or xfail was added.

Historical commands used the compatible interpreter and native browser
described above:

```bash
PATH=/tmp/shelf-new-test-compatible-venv/bin:$PATH \
  make test PYTEST_PAR='-n 4 --dist loadfile'

PLAYWRIGHT_NODEJS_PATH=/usr/bin/node \
SHELF_E2E_CHROMIUM_EXECUTABLE=/home/opencode/.cache/michael-doctor-verification/chromium \
PATH=/tmp/shelf-new-test-compatible-venv/bin:$PATH make test-e2e

PATH=/tmp/shelf-new-test-compatible-venv/bin:$PATH make checks-fast
```

Application/static/test files remained frozen during each gate; delivery
documentation and isolated sibling worktrees did not change the served code.
No production data, keys, Portainer resources, ports or deployment were changed.

The final 8162-test command ran in
`/workspace/shelf/swarm-20261113/first-batch-fullgate`:

```bash
PATH=/tmp/shelf-new-test-compatible-venv/bin:$PATH \
  make test PYTEST_PAR='-n 2 --dist loadfile'
```

It reported four existing warnings (Starlette/httpx and the deliberate
duplicate-ZIP fixture). Its result does not transfer to the later source,
household, history, browser-storage, portability or translation branches.

GitHub explicitly paused Actions execution on this fork because of usage
volume. The operator selected **continue local only** rather than re-enabling
Actions or supplying another runner. Its execution settings remain unchanged.
Python 3.12/pinned-Playwright/container acceptance and named live-server/IdP
acceptance remain unverified—not passes, waivers or claims of release readiness.

### Explicit authentication-cookie follow-up

Application commit `717dfeb6` fixes a separately reproduced middleware defect:
an aged request's optional sliding refresh appended its old session after a
route had explicitly logged out, logged in another account, changed the password
version or updated the profile. The four reproductions failed before repair;
ordinary aged/fresh read controls already passed.

Routes now retain ownership of explicit authentication/CSRF response cookies.
Ordinary aged reads still refresh and preserve their CSRF token. On frozen
`c528985c` (the same application, plus the generated collection badge):

| Gate | Result |
|---|---|
| Auth, boundary, cross-site, secure-cookie, security and API-auth regression set | **167 passed**, exit 0, 73.55 s |
| Native-browser authentication arrival and CSRF/XSS regressions | **6 passed**, exit 0, 14.54 s |
| `make badges checks-fast`, whitespace checks | Passed; private roadmap-map check remains unavailable |

```bash
PATH=/tmp/shelf-new-test-compatible-venv/bin:$PATH python -m pytest \
  tests/test_auth_response_cookie_ownership.py tests/test_auth.py \
  tests/test_auth_routes.py tests/test_auth_boundaries.py \
  tests/test_auth_cross_site_arrival.py tests/test_auth_cookie_secure.py \
  tests/test_security_fixes.py tests/test_api_v1_auth.py \
  -q --tb=short --no-header -n 2 --dist loadfile

PLAYWRIGHT_NODEJS_PATH=/usr/bin/node \
SHELF_E2E_CHROMIUM_EXECUTABLE=/home/opencode/.cache/michael-doctor-verification/chromium \
PATH=/tmp/shelf-new-test-compatible-venv/bin:$PATH python -m pytest \
  tests/e2e/test_auth_arrival.py tests/e2e/test_csrf_and_xss_fixes.py \
  -q --tb=short --no-header -m e2e
```

At this cookie checkpoint, collection contained 8168 units; the earlier
**full 8162-test pass belongs to `38c73f97`**, not this later application.
No full-suite or pinned-runtime pass is inferred from this focused follow-up.
The larger local candidates are recorded separately below.

### Second source-delivery batch: frozen local gate

Candidate `29edfaf9` completed the full non-live unit/integration command in
`/workspace/shelf/swarm-20261113/source-delivery-fullgate`:

```bash
PATH=/tmp/shelf-new-test-compatible-venv/bin:$PATH \
  make test PYTEST_PAR='-n 4 --dist loadfile'
```

Result: **8996 passed, 1 failed**, exit 2 from `make`, in 1907.12 seconds.
This is a failed gate, not a full-suite pass. Its sole failure was the older
availability test's assumption that a viewer has one outbound link and no forms.
The integrated F4 fragment deliberately supplies two re-resolved links and an
account-owned source-preference form; it must still prohibit catalogue edits.

Test-only follow-up `86879998` verifies both exact handoff destinations and link
protections, permits only the CSRF-protected personal-preference form, checks
that a forged account field cannot select another owner, refuses a viewer's
catalogue edit and compares unchanged catalogue rows. Availability, handoff and
source-delivery integration tests passed **118 tests**, exit 0. Test-convention
and whitespace checks passed. This was a focused-only checkpoint; later full
source gates are separately attributed to `1767d0cb` below, not this follow-up.

After composing the full-metadata benchmark and source-digest optimization,
`7e86d0a4` passed **565 focused source-migration, authority, snapshot, availability,
handoff and delivery tests**, exit 0, in 173.64 seconds. This predates the
separately declared integrated schema/UI benchmark below and is not a full run.

Separately, the eight-width source layout/census checkpoint reported **9 tests
passed**, exit 0, in 467.43 seconds, with **88 screenshots**. The tested tree is
`56cbe996fe42de14fe1cb8b31b0627735b4f5f17`: base `cc129371` plus the recorded
CSS/SW overlay, not clean `cc129371`. The relevant tests, preferred-source
template and rebuilt outputs match `29edfaf9`. All eight widths retain the
unchanged **80px** minimum. `.temp/source-delivery-layout-verification.json`
records bounded artifact integrity/source equivalence, not an independent
rerun of the worker's nine tests. These results are not a full-browser repeat,
provider-permission grant or live-server acceptance.

## Local follow-up candidates (documentation capture)

The bounded primary-source checkpoint is **`916a4dd6`**, including reviewed
media foundations, cards, strict format filters, full-page editor extraction and
proof, compatibility-test migrations, page adoption, shell/shortcut wiring and
generated assets. The remote checkpoint before this increment was `b9c5fbec`.
The earlier `f3809d6` / `89388e31` source checkpoint
and its `1cf3c0b6` / `89205f44` evidence remain historical, not latest-source gates.

This three-document snapshot records bounded source/evidence, not feature closure
or a release. Core, host/Steam and portability candidates
below remain isolated from the primary checkpoint. Evidence paths identify
frozen local archives, not CI artifacts. Parent executions, independent reviews
and worker reports are distinguished. The bounded parent gates use the approved
compatible **Python 3.14.8** interpreter, not pinned Python 3.12. The fresh
1935-case affected composition below is not the complete collected unit suite.

### Media-experience and Steam scope, not implementation acceptance

The [ME1–ME6/ST1 refinements](connected-library-plan.md#operator-media-experience-refinements-2026-11-14)
extend, not replace, the full F/R/X backlog. They require one primary Browse
opening the complete authorised mixed live catalogue, nested family/format
disclosure for hover/click/keyboard/touch, and removal of duplicate top-level
media links while retaining administrative/bulk Browse under Manage. Other
requirements are an accessible shared Plex-style pop-out editor with common and
media-specific fields/full-page fallback, reusable native-proportion artwork
cards (including music), one horizontal recent row with View more, and
account-owned mixed/family/format/custom grouping. Shared partials and a registry
extend FastAPI/HTMX/Alpine CSP, not a SPA. Existing spacious layouts, the **80px**
control-width gate, permissions, catalogue/copies/history, integrations, keys and
original data remain constraints, not waived acceptance criteria.

The selected incremental contract uses the existing server-rendered framework,
not the unaccepted F2 `10560d5` kernel/schema assumptions. At `916a4dd6`, Home,
Music and Library consume the shared registry and card/artwork partials; Home
uses the shared default-mixed recent shelf. The full-page item editor uses shared
common/media-specific fields, **not a pop-out dialog**. Native multipart/CSRF proof
simulates encoding and the existing token injection, not browser JavaScript execution.

The real shell derives its model from already-authorised tabs. Primary Browse
targets `/library?reset=1&view=items&sort=recent`, or `/browse?sort=newest` when
Library is unavailable; Manage → Catalogue management retains `/browse`.
Family/format links use strict canonical `types` CSV filters. Server-route proof
does not establish hover/touch/keyboard/focus/resize or no-JavaScript layout.

#### Parent frozen media gates

Each result is the latest `coordinator-evidence/result.json` in its named archive
under `.temp/swarm-reviews/`. These are **separate bounded executions**, not an
additive coverage total or a transferred composed pass.

| Frozen archive | Parent result | Outcome and scope |
|---|---|---|
| `media-foundations-expanded-ea6c36f` | **772 passed**, 142.91 s | Expanded registry/query/pure-preference/navigation foundation union only |
| `media-cards-fixed-73b13d1` | **262 passed**, 100.83 s | Shared components and corrected cases only |
| `media-type-filters-fixed-6f3fc90` | **528 passed**, 144.89 s | Strict parser, affected routes and sort-restore regression only |
| `media-editor-native-c88920c` | **115 passed**, 66.60 s | Full-page extraction and simulated populated multipart/form-CSRF proof |
| `editor-test-compat-ffad4d3` | **124 passed**, 62.78 s | Three migrated legacy editor functions plus shared-editor/write cases |
| `media-page-compat-870caaf` | **790 passed**, 214.35 s | Registered Home/Music/Library card adoption and bounded test migration |
| `media-shell-389fed9` | **648 passed**, 127.46 s | Actual server context, links, filter consumers and shell wiring only |

All seven exited 0 with zero JUnit failures/errors/skips, unchanged archived input
hashes/modes and both guards, and zero child blocked attempts. Their source picks
are reviewed at the primary checkpoint; their execution results do not transfer
to the combined primary tree. The earlier registry `e484ba2` **104-pass / 69.54 s**
parent gate and the separately recorded three old shape-policy failures remain
historical; policy and compatibility migrations are now present, rather than still
awaiting adoption.
The core Browse timestamp-tie limitation is unchanged. Historical Tags counts,
creator-label JSON and Music blocked-DNS failures remain attributed to their
isolated `97dd90a`/`0f9f499` snapshots, not silently waived.

The chosen contained artwork boxes are 1:1 for CD/vinyl/digital music/audiobook,
5:8 for cassette, 3:4 for games and 2:3 otherwise. The bounded recent-query and
pure recent-preference helpers do not establish account-owned persistence/CAS or
the Library appearance UI. General Library search still hydrates the live
catalogue before grouping/paging; these helpers do not close F1 performance.
Shortcut `03198af5`, picked as `f574d67a`, reuses the rendered primary href and
retains `/browse` only when no primary anchor exists. Its parent frozen gate was
**352 passed / 1 stale-catalogue failure / 145.05 s**, including all 14 new cases;
the catalogue failure was enabled and reproduced before the fix. Its Node event
double/registered-route proof is not browser key dispatch or full app.js startup.

#### Fresh complete media composition

Frozen `916a4dd6402f0306155bb4d67091e686d9a6ad0c` has an exact source archive and
manifest under `.temp/swarm-reviews/media-primary-916a4dd6/`.

| Receipt | Actual outcome | Scope |
|---|---|---|
| Fresh affected composition | **1935 passed**, 415.19 s, exit 0 | All 1687 unique prior affected cases retained, plus 248 asset/shortcut/script controls; exact executed node set matches the fresh selection |
| Three separately enabled prior-baseline controls | **3 passed**, 3.82 s, exit 0 | Tags count, intake creator JSON and Music wishlist add execute unmodified on this same candidate |
| Generated outputs and offline checks | Passed; generation repeat-identical twice | Installed Tailwind 4.3.3, central PO/MO, generated SW `v060dfaa0` and collected-count badges; no install or live call |

The affected run has zero failures/errors/skips. All **895** tracked input hashes
and modes and both guards were verified unchanged afterward; inherited child
attempts were zero. `coordinator-evidence/result.json`, `affected.xml`,
`affected.log`, selection/command manifests and `baseline-result.json` retain the
individual receipts. The separately enabled baseline tests and their two directly
related templates are byte-identical to published `b9c5fbec`; their three passes
are not fixes attributed to this media delta or broad R9 closure.

Generation changes eight paths only. Existing same-key translations are unchanged;
22 new message entries use English fallback, with two retired editor wordings.
Badges record **10125 unit / 422 E2E collected**, not that those suites ran or passed.
The formerly stale catalogue check passes in the composed run. CSS rule checks are
structural, not computed-style/layout proof. This receipt grants no browser,
pinned-runtime, live-provider, full-unit, F1-scale, preference-persistence/CAS,
enhanced-dialog or original-185-check acceptance.

#### Steam and host policy

Steam remains **UNREGISTERED**, without account-private observation, pacing or
actual Sources/store/launch integration acceptance. The operator's owned-library,
links and reported-playtime requirement is unchanged; source playtime must not
overwrite personal progress/history. Private/missing enumeration is not a
complete empty library. Full database-backup fidelity and matching external keys
remain requirements; no blanket Steam backup exclusion is approved.

| Frozen archive | Parent result | Outcome and scope |
|---|---|---|
| `steam-first-close-333289b` | **RED: 454 passed / 1 failed**, 167.80 s | Bounded cleanup/cancellation source review clear; unchanged enabled forced-INFO credential-reflection counterexample remains |
| `transport-logging-fixed-481bdab` | **384 passed**, 118.54 s | Reviewed combined host policy and explicit-negative-level correction only; isolated, not in primary |

The latest result files are in each archive's `coordinator-evidence/`. Post-run
inputs/modes and both guards match, with zero child blocked attempts. The host
policy establishes WARNING floors before fetch, contains root DEBUG, clamps
explicit negative levels, preserves exactly-NOTSET inheritance and stricter
thresholds. It does **not** clear the standalone forced-INFO boundary. A coherent
Steam dependency composition is required before host integration, not a whole
candidate transplant or transferred gate.

Protocol proof uses installed **HTTPX 0.28.1 / HTTPCore 1.0.9 / h11** over an
in-memory backend, not TLS, a live Steam/account call or the actual Sources UI.
The first adapter `4d55c79e` **433 worker passes / 183.97 s** and its adverse
timeout/cleanup review remain historical; `333289b` supersedes that cleanup
checkpoint, not its missing integration/privacy/provider gates. Public Steam
documentation supports HTTPS header credentials and visibility limits; it is
not live-account evidence. No productive registration or launch acceptance follows.
The selected future launch path is generated `steam://rungameid/<uint32>` behind
explicit confirmation/revalidation, not general custom-scheme acceptance.

### Integrated source gates and unchanged scale protocol

Frozen `1767d0cb2beb9eba26aac3f44123065e7aa1a9a6`, tree
`6e5a1e6023b194b27af0d87b4fcd6a044aa2510f`, has a complete source copy and
`candidate-manifest.json` under `.temp/swarm-reviews/source-integrated-1767d0c/`.
Its `gate-evidence/` contains completed unit/browser logs, the 422-browser JUnit
and copy checksums.

| Candidate | Gate | Result |
|---|---|---|
| `1767d0cb` | Complete non-live units | **RED: 9301 passed, 1 stale-badge failure, 4 offline-fixture teardown errors**, 1971.10 s |
| `1767d0cb` | Complete non-live browsers | **422 passed**, 1945.22 s |
| `89205f44` | Test/README-only repair, focused tests and checks-fast | **61 passed** plus checks-fast, coordinator-reported; badge collection stamped at 9302 |
| `89205f44` | Frozen complete non-live units, external-network guard enabled | **9302 passed**, zero failures/errors/skips, exit 0, 2006.04 s |

The offline guard retained teardown failures for four caught outbound-artwork
attempts in fixtures; they were not waived.
`89205f44479fb58f08b2e90945bff57b0ce5401b` repairs the four mocks and badge only.
Its copied `next-unit-89205f44-plan.md` and
`next-unit-89205f44-freeze-before.json` identify tree
`877949e9487dd7ba75086c8ecd407aaaade0edc2` and identical committed `app/`,
`static/`, `tests/e2e/` and benchmark-harness inputs to `1767d0cb`.
After the full unit run, the coordinator verified unchanged HEAD/tree, all four
input objects, guard bytes and **782 committed application/static/test/tool
blobs and file modes**. JUnit confirms all 9302 cases with zero failures, errors
or skips. Results, complete log and before/after evidence were recorded in
`/workspace/shelf/swarm-20261113/next-integration/.temp/frozen-unit-89205f44/`.
Frozen copies are in
`.temp/swarm-reviews/source-unit-repair-89205f4/coordinator-evidence/`.
Input equivalence is **not a new execution** of 422 browsers or the benchmark.
The earlier `b0209aff` declaration was superseded **before execution** and has
no result.

The independent promotion gate passed **27/27 archived-evidence/input checks**
on `1cf3c0b6`: all **822 non-documentation files** equal `89205f44`, all **866
tracked files** match the promoted blobs/modes, and the three complete 1767/892/
promoted archives match their respective commits. Only the two canonical
delivery documents differ from the tested source parent. The gate reconciles
the narrower 782-input count, parses the original 9302-unit/422-browser JUnit
and recomputes benchmark sample metrics/checksums; it is **not a new unit,
browser, benchmark or SQLite-database audit execution**. Evidence:
`.temp/source-promotion-89205f44/independent-gate.json`; promoted source:
`.temp/swarm-reviews/source-promoted-1cf3c0b/`. These are promotion-time checks,
not a claim that subsequent WIP is frozen or independently cleared.

After that push, test-only `89388e31` strengthens an existing music-wishlist
test and passed **28 module cases** in **26.54 s**. A
test-process-only membership-write suppression produced the expected **1
failure** in **2.02 s** at the new positive assertion while owned=0/partition
checks remained valid. Independent P3 review is **REVIEW_CLEAR** for this bounded
positive-oracle change. It is committed and pushed, with no application or
case-count change; checks-fast also passed (private roadmap check unavailable).
Frozen evidence:
`.temp/swarm-reviews/wishlist-positive-membership-1cf3c0b/`. The historical full
suites were not rerun or reattributed. The committed/pushed identity and source
equivalence are recorded in `.temp/wishlist-positive-membership/published-result.json`.

The `benchmark-evidence/` copy retains the predeclared plan/protocol, manifest,
85 raw samples, summary and coordinator read-only SQLite audit. The completed
323.53-second run uses two sources of **31,400 records each**, real **8192-byte**
metadata, **565,200 initial provenance rows**, 100 genuinely changed pairs and
five cold/five warm cycles. Cold means a new SQLite connection/cache, not a cold
OS cache. Separate allocation/profiling cycles are excluded from latency p95.
Original limits remain unchanged: HTML p95 ≤ 3 s, writer-lock p95 ≤ 5 s, fresh
Python allocation delta ≤ 32 MiB, 45 minutes and 4 GiB artifacts.

| Measured p95 | Cold | Warm |
|---|---|---|
| Workspace HTML | 1.877 s | 1.887 s |
| Full applied-receipt HTML | 2.364 s | 2.371 s |
| Apply writer lock | 4.430 s | 4.320 s |

All declared local budgets passed: **40/40 real competing writers committed
within the original 5-second timeout**, 170 complete scans, and a maximum fresh
Python-allocation delta of **11,242,163 bytes**. The read-only audit verifies
62,800 protected catalogue rows and restored bindings, 12 rolled-back receipts,
100 binding versions all at 24, and all 125,600 original manual overrides
retained. Final provenance **568,200** and override count **130,000** intentionally
include the measured cycles; they are not the initial 565,200-row fixture.
Raw-sample SHA-256:
`dd45e817e2bbdc8fac469a4f4e6046d485c9970c3ee0e05b0187d99f177cb954`.

This is known-schema shared-host evidence only: no household-observer, legacy,
unknown-trigger/generated-column, raw-mismatch, full-size RAM, live-provider or
pinned-runtime/container acceptance is established.

### Separate memory attribution diagnostic — not a RAM gate

Every original sample reports raw `ru_maxrss` **11,373,289,472 bytes**, already
present on the first page. Keep that telemetry unchanged; **11,242,163 bytes is
Python allocation, not application RAM**.

A separately predeclared **64-record** diagnostic on exact `1767d0cb` completed
in **2.97 s** with **32 checkpoints**. Its plan, script, raw `stages.jsonl`,
summary, coordinator audit and `REPORT.md` are under
`.temp/source-memory-diagnostic-1767/`.

| Diagnostic observation | Bytes |
|---|---:|
| Raw `ru_maxrss` at script entry, before app/binding/seed | 11,373,289,472 |
| Parent `opencode` VmHWM at entry | 11,373,289,472 |
| Current VmRSS at entry | 17,969,152 |
| Maximum sampled current VmRSS | 105,701,376 |
| Maximum observed per-exec VmHWM | 146,681,856 |

Independent read-review supports this launch-path-specific telemetry discrepancy,
not proof of its exact fork/exec inheritance internals or the original full-size
execution's current occupancy/own peak. Entry means after interpreter/stdlib
startup; `/proc` counters are approximate, and the displayed HWM is a maximum
observed value. The 256 MiB check is post-work output size, not a RAM limit.
The original run lacks current/per-exec RSS telemetry, so this tiny diagnostic
cannot establish a scale-RAM pass. A separate post-diagnostic check confirms all
six original recorded artifact hashes still match. No original sample or memory
budget was replaced.

### Core, recovery and R2 admission remain bounded

#### Latest bounded core review (2026-11-14)

Independent read-only review of frozen `2ed54fb6e15d41deae46b16fc43dd72db4d0db02`
against `af07fd8e` returns **SOURCE_REVIEW_CLEAR for the two authorised findings
only**. Exact primitive-type comparisons avoid value-class hash/equality hooks;
lexical, success-only catalogue ownership and the complete actual TEMP definition
are authenticated **before the first certification MAIN catalogue SELECT**.
Genuine schema, IUD-guard, transaction, binding and installation fences remain.
Only the two isolated access service files change; `app/database.py` is unchanged.
The primary application does not contain these isolated service modules, and no
core transplant or fourth authority file follows from this review.

Independent nine-case H/M packet `2491b661` receives **TEST_PACKET_REVIEW_CLEAR**:
productive prerequisites, four independently durable child-write witnesses,
NOCASE synthetic protected-OLD delivery and truthful-output provenance controls
are substantive. Captured pre-fix **6 failures / 3 passes, 12.51 s**, and **96
reference passes, 210.05 s**, remain worker evidence; trailing after-fix assertions
were not reached on RED. Test-quality clearance does not itself prove the earlier
catalogue authentication prefix; that prefix was inspected separately in source.

Exact reviewed e7 schema and H/M files are received as test-only `9111716e` /
`3954f20c` atop R6, preserving source and all 486 existing cases. A fresh parent
gate on that exact composition passes **120 tests / 266.29 s**: 15 schema, nine
H/M and 96 unchanged references. All 938 tracked input hashes/modes and both guards
remain exact, with zero errors/skips or child-network attempts. Previously
unreached H/M after-fix assertions now execute on the corrected source. This is
bounded qualification, not the full core suite or primary integration.

New test-only `dcf71c67` adds two C1 forward-only observations and one synthetic
D1 observed-definition case, receiving independent **TEST_PACKET_REVIEW_CLEAR**.
Its captured worker gates are **3 passed / 4.60 s** and **96 references passed /
223.84 s**, not parent results. Exact receipt as `e1b18303` preserves source and
all 510 existing composed cases. Its fresh parent **513-case union completes
463 passed / 50 failed / 1586.60 s**. All original 486 identities and outcomes
match frozen R6 exactly; all 27 additions pass. All 939 input hashes/modes and
guards remain exact, with zero errors/skips or child attempts. The 50 enabled
failures remain 28 K13, 17 D3 proof dependencies and five baseline failures, not
waived regressions. D1 is not real missing/changed TEMP-guard or ledger-ownership
evidence. Actual T1/T3 construction remains unqualified without approved seams.

Separate fresh parent caller bands retain **4 passed / 5 failed** on `3954f20c`
(23.02 s) and `e1b18303` (21.41 s), with exact identities/outcomes matched to the
frozen R6 caller receipt. Three fail at `DELETE users` through `api_tokens`, and
two at the genuine datetime-function seal. Both runs preserve input hashes/modes,
guards and zero child attempts. They do not install the earlier public exception
code reporter: historical `23 / SQLITE_AUTH` metadata is not relabelled as a new
measurement. Caller bands are separate from the 513-case core union.

R6's captured **436 passed / 50 failed, 775.89 s**, and caller **4 passed / 5
failed, 13.51 s**, are not new parent runs or overall acceptance. The unchanged
50 comprise 28 K13, 17 D3 proof-dependency and five baseline failures. Account
deletion still reaches genuine `23 / SQLITE_AUTH` through `api_tokens`; two
datetime-seal fixture conflicts also remain. No cascade grant, fixture weakening,
privacy/deletion, pinned-runtime, primary integration or activation is cleared.
Immutable captures and bounded reviews are under
`.temp/swarm-reviews/core-primitive-provenance-2ed54fb/`,
`.temp/swarm-reviews/core-upsert-hm-2491b66/` and
`.temp/swarm-reviews/core-upsert-observations-dcf71c6/`. New parent receipts are
under `.temp/implementation-wave-20261114/`, including `BASELINE_RECEIPTS.json`,
`PARENT_CALLER9_RECEIPT.json` and `PARENT_FULL513_RECEIPT.json`.

#### Continuation UI baselines and frozen feature candidates

Before feature integration, clean `95ff5d63` passes **993 affected UI unit tests /
605.17 s** and separately all **19 original navigation browser cases / 30.13 s**.
All 898 input hashes/modes, guards and zero child attempts are verified; these
are pre-feature baselines, not cover-control/locator/palette acceptance.

Frozen palette `de5c018b` adds only five component/test files, preserving every
original file. Fresh parent gates pass **229 unit tests / 166.69 s** and **58
browser cases / 73.36 s** (39 new component cases plus the 19 original navbar
cases). Independent review nevertheless required changes: without native dialog
styling, its closed body exposed inert controls. Follow-up `3701f1ea` adds the
controlled missing-API-and-styling RED→GREEN regression and explicit CSS hiding,
receiving bounded **SOURCE_REVIEW_CLEAR**. Fresh parent checks on its exact-tree
receipt pass **229 unit / 159.71 s** and **59 browser / 72.23 s**. These remain
component-shell checks, not authenticated shared-wiring acceptance or actual
unsupported-browser qualification.

Parent live candidate `09d360cd` receives the exact reviewed production component
and adds real shell wiring, shortcut discovery, Italian/Simplified Chinese strings,
generated CSS/SW and documentation. Preliminary real-route checks pass **14 HTTP
unit / 14.97 s**, **five authenticated browser / 12.05 s** and eight offline
source/generated checks; the browser run preceded the translation update. A
targeted frozen run then correctly found the standalone component harness adding
a second palette after base wiring. Only that new fixture's setup was adapted to
inject fragment/assets/help marker when absent, assert singleton wiring and retain
`app.js` → host handlers → palette order. Its unchanged targeted case passes
**1 / 2.50 s**; all 40 component identities, test bodies, decorators, assertions
and the controlled-UA regression remain unchanged. Independent shared-wiring
review returns bounded **SOURCE_REVIEW_CLEAR**. Fresh frozen gates give **1158
unit passes / 737.33 s** and **81 browser passes / seven failures / 261.86 s**.
All 905 input hashes/modes and guards remain exact, with zero errors/skips or
child attempts. All 40 component and five authenticated journeys pass, but the
seven original browser failures remain enabled blockers; no UI primary selection.
README's **10205 unit / 486 E2E** badges are collection counts, not pass totals.

A separate clean-baseline parent run of the older original navbar file gives
**7 passed / 5 failed / 89.14 s**, with exact inputs/guards and zero attempts.
Store/Discover/Series visibility, narrow Browse collapse and direct mobile Stats
navigation are genuine programme blockers. Series is under **Explore**, not Media;
the older settings fixture legitimately hides unchecked Library. No test is
rewritten or waived. An isolated presentation-only repair is assigned for a unique
quick Store link, expanded in-flow collapsible Explore and narrow-menu primary
collapse, preserving navigation policy, the exact original 234-unit band and
all original 12 + 19 browser cases.

That writer is now frozen at `39007ccb`, reporting unchanged **234 original unit
and 31 original browser passes**, 63 new controls and 103 ancillary unit passes.
A fresh parent run passes **234 / 187.13 s**, separately **12 original browser /
35.07 s**, and **19 original + 39 new browser / 77.60 s**. Independent review
returns bounded **SOURCE_REVIEW_CLEAR**. This does not repair primary or close the
remaining phone journey. Original-12 and layout-19 invocations stay separate to
avoid the older settings fixture's legitimate hidden-Library state contaminating
later HTTP fixtures. Wider worker runs retain SVG and Home overflow failures;
no original case is removed.

The wider band also finds two original connected-Library phone failures. A fresh
clean `95ff5d63` run reproduces **both / 43.62 s** with unchanged inputs/guards and
zero attempts: a local SVG does not render through the raster-only shared-card
adapter, and the phone Main menu does not resolve as the expected button. The
palette run matches all 12 navbar and both phone baseline identities/outcomes.
The SVG case has a real contract conflict: existing unit tests explicitly reject
raw SVG paths and fix ordinary IMG attributes, while the original browser case
requires a genuine SVG image and an IMG shape hook. Neither test can be weakened,
and neither SVG allowlisting nor fake artwork is authorised. A separately reviewed
validated-source compatibility design is needed. Later account-scroll assertions
remain unreachable and unqualified; these are not obsolete tests.

The phone opener cannot be fixed by adding `role="button"` to the native first
Summary: official HTML-ARIA prohibits that override. The proposed external real
button also conflicts with an enabled enhanced-DOM Browse→shell adjacency check.
Both proposals are rejected. A capability-gated real button inside the same
shell, following its retained native Summary, now has bounded
**IMPLEMENTATION_CONTRACT_REVIEW_CLEAR** and is assigned to the same isolated
navigation writer. Synthetic browser capability evidence is not application or
accessibility qualification. The existing authorised nested
`all_media` link can be named **Library**, with **All media** retained as an
outside noninteractive description; no extra anchor or permission is approved.

ME7 component `b7daa1af` has bounded source clearance; fresh parent checks pass
**359 unit / 206.44 s** and **37 browser / 56.42 s** (18 component plus 19 original
navbar). Actual producer wiring at `7257402a` receives bounded
**SOURCE_REVIEW_CLEAR**, not primary selection. Predecessor `74e2ec8e` introduced four genuine original Music
unit failures by putting control buttons inside the protected local-record root.
The panel now sits immediately before that root; all original assertions remain
unchanged and the same four cases pass. A fresh frozen producer/unit band now
passes **368 / 234.25 s**, including 22 new HTTP/locale cases and the original
Music assertions. Its browser band gives **95 passed / one new test-locator
failure / 278.70 s**: Playwright excludes text on the `noscript` wrapper. Test-only
`98730939` instead checks the actual visible fallback paragraph and strengthens
native-card default sizing/focus/destination coverage. The targeted case passes;
fresh complete gates give **368 unit / 188.06 s** and **96 browser / 271.15 s** on
exact-tree receipt `b06b7357`. All 913 inputs/guards remain exact, zero attempts,
errors or skips. The one-file test delta also receives bounded
**SOURCE_REVIEW_CLEAR**; the combined UI tree still needs fresh qualification.
The selected increment keeps
grid columns and 10rem shelf slots fixed and sizes cards to
`min(host width, max(80px, host width × requested scale))`, 50–100%, default 100%.
This is floor-limited shrinking, not greater density or persistence. Integration
must explain that limit accessibly and hide the control for every Library
`view=series` page, including drilldowns that contain shared cards. Additive
drilldown coverage and actual-grid/layout checks at 50%, before Reset, remain
follow-up requirements until qualified in composition. No whole-ME7/account
preference closure.

Both unchanged original Home page walks at 320/1024 pass in the cover candidate's
96-case band. Fresh clean `95ff5d63` reproduces both Home overflow failures
**2 failed / 116.71 s**, retaining the original screen-reader-text and shelf
offenders. This attributes them to the unchanged baseline, not the navbar repair;
the cover containing-block correction is qualified only on its tested candidate.

Locator follow-up `995b1d37` and pure search-helper follow-up `aad34891` each
receive independent bounded **SOURCE_REVIEW_CLEAR**, resolving their respective
confirmed defects with qualified unchanged-test RED→GREEN evidence. Locator
preserves empty non-NULL SQLite position types rather than substituting zero;
helpers casefold after compatibility decomposition without changing canonical or
identifier semantics. Captured worker results are **368 unit / 39 focused browser**
and **374 helper/reference passes**, respectively. Fresh parent checks independently
pass **368 locator unit / 279.83 s**, **39 focused locator browser / 69.71 s** and
**374 helper/reference / 248.91 s**, with exact inputs/guards and zero attempts.
Locator shared assets/i18n are wired only in the unselected composition below;
this does not close whole RF11.

**Selected primary foundation:** `0d448bcd` integrates the exact reviewed helper
files and semantics-preserving catalogue delegation, not an index. Its two private
wrappers forward canonical folding and retain fresh ordered mutable snapshot
lists; the existing identifier-key binding aliases the same frozen registry.
The initial glue mistakenly removed that binding, causing an original retained
component assertion to fail. The strict purity guard then prevented pytest error
reporting/JUnit: that aborted run remains explicitly unqualified. An added binding
regression gives qualified `AttributeError` RED, then unchanged GREEN plus the
original strict-guard case after the alias repair. No helper or original assertion
was changed. Independent re-review clears the repaired source.

Fresh final **533 passes / 235.90 s** comprise 215 helper cases, 12 glue cases and
306 original references; all prior 374 identities/outcomes remain. Seven offline
checks pass. All 902 primary bytes/modes match the reviewed/tested tree; the
original 432 test files and 185 unchecked acceptance checks remain exact. Primary
badges **10352 unit / 441 E2E** record collection only. The planner still hydrates
the whole catalogue; forgiving matching, identifier ranking, persistent indexing,
RF3, bounded total SQL/RAM and eventual isolated-core compatibility remain open.
Selection receipts are `SEARCH_FOUNDATION_PRIMARY_SELECTION.json` and
`SEARCH_FOUNDATION_FINAL_RECEIPT.json` under `.temp/implementation-wave-20261114/`.

#### First combined UI and locator candidate — not selected

Parent `cfffa6dc` composes selected search with the reviewed palette, cover,
exact-copy locator and five-regression navbar increments. The only cherry-pick
conflict was generated README counts, then centrally regenerated. All **931**
tracked files' bytes/modes are verified against the immutable review archive; all 435
primary test files and 448 pre-existing composed test files remain unchanged.
No isolated core, portability or additional authority is transplanted.

The parent adds real locator asset registration, compiled Italian/Simplified
Chinese strings and four additive test modules. Independent shared-wiring review
returns bounded **SOURCE_REVIEW_CLEAR**. New checks give **12 HTTP/unit passes /
9.13 s** and, separately on the same production bytes, **15 browser passes /
32.72 s**: nine non-injected locator journeys and six actual-grid/artwork checks
while at 50%, before Reset. Three initial new unit cases failed because their
fixture called a nonexistent series-key helper; only that fixture changed to an
explicit canonical ASCII key, retaining every assertion. Their RED is preserved.
Series drilldowns now have explicit no-control coverage. Review identified a low
proof limitation: geometry agreed with a rendered shape token without independently
asserting media type→shape. Follow-up `8971dd90` adds explicit native-shape
expectations while retaining every existing assertion and case identity; its six
cases pass **14.17 s**, and independent re-review clears that test-only delta.

Seven offline checks and four JS syntax checks pass with exact inputs/guards and
zero attempts. **10582 unit / 583 E2E** badges are collection counts only. The
fresh wider unit band completes **1704 passed / 886.52 s** with exact inputs and
guards, zero attempts, errors or skips. The combined browser band completes
**178 passed / four failed / 530.93 s**; the separate original navbar invocation
passes **12 / 38.57 s**. Both have exact inputs/guards and zero errors/skips or
attempts. The full browser band retains the original SVG and Main-menu failures
and exposes **two genuine new composition failures**: unsupported-dialog palette
fallback expects the authorised primary Browse visible, while enhanced narrow
navigation hides it. Earlier isolated component passes do not waive this conflict.

The bounded fallback contract now has implementation-contract clearance. Frozen
repair `9562f558` changes only palette JS/CSS plus additive tests, docs and generated
assets. A present, complete component with unavailable native dialog support marks
the existing navbar; scoped CSS exposes the **same** primary Browse anchor without
opening menus. Missing/incomplete markup is not mislabeled, normal supported
collapse remains unchanged, and successful later initialization clears the marker.
No navigation-file, anchor, permission, request, storage or focus change is made.

Both original failures pass unchanged with 18 new browser controls; independent
review returns bounded **SOURCE_REVIEW_CLEAR**. Fresh exact frozen bands give
**371 unit / 262.25 s**, **116 browser / 151.28 s** and, separately, **12 original
navbar / 38.07 s**. The 116 retain all 98 earlier palette/layout/navigation IDs;
only the two known fallback outcomes change from RED to GREEN. All 933 inputs and
guards remain exact, zero attempts/errors/skips. Seven offline checks and four
JS syntax checks also pass. Badges **10588 unit / 601 E2E** remain collection only.
These are candidate-specific bands, not a replacement full-UI or pinned gate.

Phone navigation and local-vector compatibility remain separate candidates,
neither with a passing final composition. Phone `3c2e85c0` receives bounded
**SOURCE_REVIEW_CLEAR** and fresh parent **459 passed / two same RED**, retaining
all 234 original unit, 31 original browser, 63 prior additive
and 103 ancillary cases. The original journey reaches line 984 but finds Home
overflow **389px at 375px** in its tree, which lacks the cover correction. A
retained later-leg probe reaches line 990: canonical Library navigation correctly
selects two optional display choices, but the Summary includes their count while
the original requires a stable bare caption. No href, default, count algorithm or
assertion may change to disguise this. Fresh parent bands are **234 / 189.85 s**,
separate original **12 / 37.57 s**, **19 / 28.38 s**, **137 unit / 101.40 s** and
**57 browser passed / two RED / 90.90 s**. All 461 worker identities/outcomes are
independently reproduced, with exact inputs/guards and zero attempts/errors/skips.

The parent clarifies replacement-readiness scope: preserve the existing desktop
enhanced-and-open native Summary hiding required by original tests; new
replacement-specific or narrow-opener hiding requires verified readiness. This
does not authorize an unready narrow hide or a Summary role override. Immutable
phone capture verifies all 900 files' bytes/modes, all 432 original test files and
both existing regression modules as exact byte prefixes. Parent `1b1984cb`
composes the full three-commit phone range with reviewed `9562f558`, then regenerates
centrally. Its full original 375px journey passes Home line 984 and reaches the
caption failure at 990; the later-leg probe retains the same caption failure
**two failed / 19.58 s**. A pass at another width was not substituted.

The caption/status contract receives independent implementation-contract clearance.
Parent `aed1fce2` keeps the stable Summary and moves the same single translated
count beside it, still inside Details, with a nonzero-only description reference.
Versioned CSS exposes the real count while the complete optional body is closed;
old HTML and unavailable CSS retain native collapse. Counts/defaults/hrefs/form
values are unchanged. Source review returns bounded **SOURCE_REVIEW_CLEAR**;
new nine plus unchanged 78 unit cases pass **87 / 50.42 s** before generation.

The first browser band records **24 passed / three failed / one teardown error /
83.29 s**: a new unsupported-CSS fixture wrongly expected one capability block,
but navigation and caption legitimately have two. Test-only `ced7fa30` scopes the
same strict one-hit check to the caption block, strengthens no-JS native submission
proof, and adds a focused regression with the original threshold. No original
assertion changes. Fresh frozen checks give **25 browser passed / three retained
geometry failures / 57.25 s**, no errors/skips or guard/input changes. The test-only
follow-up receives bounded **SOURCE_REVIEW_CLEAR** separately; its fresh frozen
unit band passes **87 / 49.82 s** with exact inputs/guards and zero
attempts/errors/skips. Eleven offline/syntax checks also pass. The no-JS proof
checks closed, reclosed and submitted values; it does not separately compare
FormData while open.

The full original phone now passes caption, search width and results-position
checks but fails line **1003**: book heading **y=2209.1875**, required **y<900**.
The existing later-leg probe and new focused regression retain that same failure.
Actual order remains correct recent order, video then book; portrait artwork is
309×463.5px and results start at y=868. A read-only architecture pass is examining
the whole mobile vertical budget, not approving sorting, density, artwork-shape,
clipping, fake-title or threshold changes. Original later filter/Account/Sources/
item-copy/shortcut assertions remain unqualified beyond this barrier.

Vector `a873fefd` receives fresh parent **898 unit / 527.11 s** and **four browser /
14.60 s** passes, reproducing all 902 worker identities/outcomes, including the full
unchanged original SVG/Account journey through line 962. Parent capture verifies
all 935 files' bytes/modes, all 452 pre-existing test entries and exactly the seven
authorized paths. Its bounded Library-only capture/validation and HTML issued
source do not widen raw SVG acceptance, acquisition, API, CSP or filesystem write
authority. Nevertheless independent source/security review returns
**CHANGES_REQUIRED**: BOM-less UTF-16 can bypass the ASCII boundary, and expected
directory-close errors can lose descriptor ownership or escape enrichment. A
separate inner-task cancellation case also needs physical-worker drainage proof.
Passing gates do not waive these findings. A new corrective derivative is assigned;
the original remains frozen and uncomposed. Earlier RED/error/timeout evidence
remains in the complete ledger. No repaired-vector or final composed Account pass
is claimed.

Current phone/caption composition is frozen at `ced7fa30`; vector is not composed
while held for source correction. All
original rejection/raster/phone tests remain enabled and unchanged. No UI primary
selection or whole-programme approval follows.
Fresh broader unit/browser bands are running on exact `ced7fa30` trees. The
separate original-navbar invocation passes **12 / 38.57 s**, with exact
inputs/guards and zero attempts/errors/skips. Wider outcomes are not inferred
from the bounded caption checks; known geometry/SVG failures remain enabled.

#### ME2-A protocol refinement — design ratified, no writer

The read-only planner's finite v2 is persisted alongside historical v1, receives
independent **IMPLEMENTATION_CONTRACT_REVIEW_CLEAR**, and is ratified for design
only. It specifies paired explicit mode,
canonical signed-64-bit IDs as strings, a streamed **10,747,904-byte** POST bound
before CSRF/parsing, typed stored-state comparison, owned raw-fetch JSON bounded
to 1 MiB and server suppression of optional JWT refresh for editor candidates.
Correlation/digests are not authority, revisions, idempotency or save receipts.
Lost POST acknowledgement remains **OUTCOME_UNKNOWN**. No immediate logout
revocation, cancellation of dispatched writes or SQLite/filesystem atomicity is
promised. Native editing and its original tests remain unchanged; no editor
writer or additional SQL/kernel/issuer authority is assigned. Future implementation
must recheck response ownership after asynchronous reads and explicitly render the
finite mode controls rather than installing native camera/URL/Alpine markup unchanged.

#### Retained early palette harness evidence

The worker's broader **228 passed / 1 failed** is retained as an initial harness
path-format incompatibility. An original linter test expects a basename for its
temporary fixture, while the initial coordinator placed it inside the tested
repository root. Parent-only future temporary paths now live outside each tested
worktree, still beneath authorised primary `.temp`; the unchanged clean-baseline
linter file passes **23 tests / 13.58 s** there. No application/linter/assertion
changes or waiver were made. A first parent setup attempt omitted those temporary
parent directories and produced **23 / 229 / 58 setup errors**, executing no
tests; all reports are retained separately. The corrected fresh runs above do
not relabel those errors or the worker RED. Source/test evidence is under
`.temp/swarm-reviews/command-palette-de5c018/`; runner v2/v3 disposition records are
under `.temp/implementation-wave-20261114/`.

#### Current isolated core review

The following records retain earlier candidate-specific findings and executions;
the bounded review above supersedes only its two identified source findings.

Six-file source candidate `343c257` remains **CHANGES_REQUIRED**, with two
confirmed HIGH findings: discarded callback returns can run `__del__` after
suspension restores authority; and same-cursor COMMIT/END/outer RELEASE can
finalize a pending window VM **after FINAL admission**, permitting unvalidated late
publication, including after real revocation, and swallowed finalizer errors.
Independent test packets substantiate these findings; they are not source repairs
or clearance.

| Frozen archive | Parent result | Outcome and scope |
|---|---|---|
| `core-execution-343c257` | **RED: 340 passed / 50 failed**, 656.51 s | Same 390 cases and exact worker failure IDs; 28 K13, 17 D3 proof dependencies and 5 reproduced baseline failures remain enabled |
| `core-finalization-5a73348` | **RED: 35 passed / 9 failed**, 106.35 s | Reviewed 44-case packet; nine valid HIGH finalization failures, including actual revocation/publication and deliberate errors |
| `core-invocation-a87ff2b` | **RED: 13 passed / 3 failed**, 46.65 s | Reviewed 16-case packet; two durable destructor-write witnesses plus valid `failed_check` disposal-timing RED, **not a third write** |
| `core-execution-fixed-53e1fec` | **RED: 400 passed / 50 failed**, 731.21 s | All 60 additive controls pass unchanged; exact original 50 failure IDs remain; further source findings still require changes |

All four have zero JUnit errors/skips. Latest `coordinator-evidence/result.json`
records verify **932** source-gate inputs and **933** inputs for each additive
packet, and **934** for `53e1fecd`, hashes/modes and both guards unchanged, with
zero child blocked attempts.
DENY-only/observe-only factory seams are approved **test seams**, not productive
authority. `53e1fecd` corrects the original HIGH observer-return and pre-admission
window-reset paths. Rereview remains **CHANGES_REQUIRED**: failed adapter results
can survive normalization suspension, and traceback cleanup can clear unrelated
caller frames/resources. Parent rejected the broader clearing effect; a bounded
repair and independent lifetime controls are active. No core clearance or primary
integration follows from its 60 passing controls.

Ordinary-key/binding compatibility, whole-name/all-arity seals and physical-name
alias restrictions still need explicit disposition. A read-only caller census also
finds a real account-retirement OLD-value UPSERT conflict; no compatibility waiver
or productive-port authority was granted. The one-serving-native/shared
state and dual DENY-wins guards remain required; original K13/D3/baseline failures
are not waived. Productive R2/startup, recovery and pinned-runtime acceptance
remain open. The following earlier checkpoints retain their historical scope.

- `a850b2a7` has **REVIEW_CLEAR** for callback reentry, standalone-autocommit
  write admission and byte-exact username ABA repairs. The coordinator reports
  **128 freshness/observer/dual-fence/cookie tests passed**. Its frozen source is
  `.temp/swarm-reviews/sqlite-freshness-repair-a850b2a/`.
- SQLite candidate `29ee407a` composes only `d9051b88` as `4d1f` atop `a850b2a7`.
  Worker gates remain **57 passed / 6 failed** and **161 passed / 10 failed**
  (all **151 convergence tests** passed). Actual F8 restore → cold restart →
  token-version-38 setup is blocked at real `identity_provider_epochs` startup
  admission.
  The bounded setup-generation source is in
  `.temp/swarm-reviews/recovery-setup-floor-d9051b8/`; these partial passes are
  not successful end-to-end recovery.
- Core `e0f5a8d4` versus `9d900f9` is **REVIEW_CLEAR only for the two
  cascade-only predicate-oracle/manual-detach issues**. The exact-archive
  coordinator gate passed **328 tests**, including **13 new regressions**, in
  **301.45 s**, with **701 app/test/tool inputs** matching committed blobs
  afterward. Evidence: `.temp/swarm-reviews/core-cascade-root-admission-e0f5a8d/coordinator-evidence/`
  (`result.json`, JUnit, log and plan).
- Earlier R6 composition `8c4d1aae` adds only reviewed 9d/e0 to the existing
  `aae0a3a9`/`b4b3fcfa` reproduction. Its wider gate is **RED: 126 passed /
  3 failed**, 150.27 s; all **13 new e0 cases pass**. A 6.40-second control on
  unchanged `aae0a3a9` reproduces the same three names and failure messages.
  Real merge stops at an unregistered cover-block INSERT/SELECT before R6's
  private hook; upgrade/restore fixtures encounter managed DROP/unbounded DELETE
  refusal before their intended lifecycle. No bypass or fixture weakening was
  applied. Evidence: `.temp/swarm-reviews/r6-e0-composition-8c4d1aa/coordinator-evidence/`.
  These inherited failures remain missing workflow coverage, not completion.
- Test/contract-only `42927e71` reported **107 failed / 10 passed** on the
  **historical e0 component**, not the actual current façade. Earlier 682/292
  regression passes do not close those findings; neither d905 nor a850 fixes
  the protected-predicate/callback boundary.

The actual-façade replay is separately frozen at
`6d2906d2757fdff56074149b389c3150e2ffe02c`, directly atop
`29ee407a73aab0837772d7ea2232e0b996a9e65e`. Only the two exact original 429
test/contract files were added; app/static remain identical to actual 29ee, with
no e0 kernel transplant. Its unchanged **117 cases** report **RED: 92 failed /
25 passed**, exit 1, **453.44 s**, with no setup/API/collection/teardown/guard
errors, skips or xfails. Full report, classification, raw log/JUnit and exact
source proof are in
`.temp/swarm-reviews/core-current-replay-6d2906d/worker-evidence/`.

- Public C-base access is unavailable on the façade, but a **cold EXPLAIN
  formatter receives a native cursor and reads foreign data**. Warm-cache cases
  do not invoke that formatter; C-base unavailability alone is not proof that
  all managed reentry or denial poisoning is closed.
- Seeded step cases leak **during EXPLAIN seeding only**. The actual step probe
  is unavailable and captures nothing; no actual-step foreign-read claim follows.
- Fetch formatting attempts a foreign read that is **denied**, yet the outer
  fetch still acknowledges after the callback swallows denial. There is **no
  foreign capture in this fetch group**; retained-cursor reuse outside also denies.
- Fixed credential formatting exposes the synthetic private hash. Protected
  settings/users predicates and exact-13 configuration/getter leaks remain.
  **Four productive ordinary-settings/profile/self-password controls pass**.

The actual one-serving-native façade, shared state and permanent dual DENY-wins
guards are retained, not repaired by this replay. Subsequent independent
read-review corroborates **92/25 and the phase classification**;
production/API-compatibility readiness remains **CHANGES_REQUIRED**, not a fresh
test run or source repair.

Test-only `c9190332d6310dd2d2eacce317814cd84d8f75e1`, following `98d7800c`, now has
independent **TEST_PACKET_REVIEW_CLEAR**, not application clearance. Its frozen
59-case packet reports **37 intended FAIL / 22 PASS**, **84.37 s**, zero
errors/skips; all original 56 names/results retain **36 FAIL / 20 PASS**. The
targeted 26 cases report **21 FAIL / 5 PASS**. Native witnesses require public
trigger metadata **1811 / SQLITE_CONSTRAINT_TRIGGER** and authorization
**23 / SQLITE_AUTH**; genuine static refusal has no native-code attributes.
Post-private callback restoration, retained multirow and caller-savepoint proof
were added. Converter/cached-native/pinned gaps remain; the original 117 were
not rerun in this follow-up. Packet source, classification, logs/JUnit and public
metadata are in
`.temp/swarm-reviews/core-managed-followup-c919033/worker-evidence/`.

Independent **APPROVE_IMPLEMENTATION_DIRECTION** and the parent's explicit local
**D1–D6/S1–S6 approval** supersede the earlier proposal-only checkpoint. Decisions
are in `.temp/swarm-reviews/core-coordinated-proposal-P1/PARENT_DECISIONS.md`:
closed inert tableless metadata/Row cursors, independently admitted nested
READ/public-fetch only, once-adapted typed bindings for compile/proof/step, and
invocation-atomic failed managed writes/batches, including intentional OR FAIL
tightening. Native denial must stay sticky; callback writes/finalization must not
borrow authority. One serving native/shared state and both DENY-wins guards
remain mandatory. No early K13 hiding is authorised; existing blind-write/
rowcount visibility is retained, while public own/admin hash SELECT and K13
DB/ENV closure remain dependencies, not fixed productive ports.

The original source writer followed exact test picks `dc5508d2` → `c0614612`
on unchanged 6d application. The later frozen `343c257` and additive packets above
supersede that implementation-WIP checkpoint, **not** the unresolved admission
gates. Frontend/Steam do not own those six files; the active repair is not clearance.
The parent's **22-check** Python 3.14 stdlib in-memory feasibility probe passed
(`parent-feasibility-probe/result.json` under the same proposal archive), but is
not application isolation/regression, Python 3.12/SQLite shipping compatibility
or RAM evidence. R2/private admission, F8 cold startup, original/dependency
regressions and activation remain open, not cleared by fixture initialization.

R2 `d799a29e` versus `caf76382` is a test/contract-only checkpoint, not a
productive private write/startup permit. Its copied `worker-evidence/` under
`.temp/swarm-reviews/r2-current-core-contract-d799a29/` records **5 failed /
6 passed** on the combined real-core/password-receipt gate and **11 passed**
on a separate positive/denial selection. Real private reads/local cookie-session
controls work; provider-epoch startup, public ciphertext protection and the
registered-app OIDC path do not. The loopback IdP was not reached. A real
password POST durably mutates password/version, then returns **500**; the chosen
browser receipt remains stopped and incomplete.

The proposed finite `library_access_identity` operations and maintenance-owned
one-use startup permit are **not approved productive implementation**. Next work
must protect exactly the **13 `identity_config.KEYS`**, including general
`get_setting` / `_setting_identity` access, and port the crypto legacy reader;
a filtered view alone is insufficient. Owner agreement and separately authorised
implementation/verification of the actual-core seam are required before resuming
R2/browser receipt acceptance.

The first-milestone specification records **four proposed finite APIs**
(`configuration_snapshot`, `observe_configuration`, `boot_rotate`,
`rewrite_boot_secrets`), **17 future acceptance cases** and nine owner questions.
The proposal remains `.temp/r2-finite-milestone-spec/OWNER_DECISION_PROPOSAL.md`.
Copied `OWNER_RESPONSE.md` / `.json` there records R2-domain **AGREE for
Q1/Q2/Q4/Q6 only**. The persisted
`.temp/r2-finite-milestone-spec/CRYPTO_SIGNING_LIFESPAN_PROPOSAL.md` proposes a
fixed crypto producer/core-only B registration for all 14 key instances,
separate signing/browser prerequisite ports and a registered ASGI lifespan
protocol with honest limits: it is not a framework-origin Python sandbox.
Q3/Q5 technical-owner acknowledgement and signing races, post-file conflict and
NULL-fallback decisions remain pending; Q7–Q9 are unapproved. Actual 6d lacks
`app/browser_storage.py` and retirement-grant prerequisites; no convenience DDL
or kernel/auth/main transplant is authorised. The separate callback-repair
approval does not approve productive R2/startup. The reserved writer alone does
not close Q9's frozen-composition/verification gate. These remain **proposals,
not available APIs**; Q8's actual-core prerequisite is unmet.

### R9 source checkpoint, not language completion

`b049de8a` composes the reviewed `46d9721` single-format/slot helper as
`a81beddc`, early-status preflight at `20a19249`, and seven reviewed template-body
conversions. Its coordinator gate passed **771 focused tests** in **316.17 s**,
with zero bounded unmarked-string findings, four lints and exact
source/context/plural identity **and multiplicity** preservation in all seven
templates. Evidence is in
`.temp/swarm-reviews/r9-safe-body-converged-b049de8/coordinator-evidence/`.

The reviewed settings chain `5e52459e` → `672ec441` → `19f7fa94` closes the
prototype-property opaque-error counterexample that the earlier **18 passing
cases at 672** missed. The parent's exact strengthened `19f7fa94` archive gate
passed **18 cases**, zero failures/errors/skips, in **12.54 s**; all **816
archived tracked blobs/modes** were verified afterward. This is bounded source
**REVIEW_CLEAR**, not broader language acceptance. Evidence:
`.temp/swarm-reviews/r9-settings-own-property-19f7fa9/coordinator-evidence/`.

Local scan/settings composition `f52884aaa60e9662890f600d19a0d58958e6fba3`
then passed the parent's **946-test affected-selection union**, zero
failures/errors/skips, in **183.40 s**. All **817 tracked blobs/modes** and guard
bytes matched afterward. This is new combined execution, not reuse of the
418-test settings worker result or other worker passes. Plan, raw log/JUnit,
result and freeze are in
`.temp/swarm-reviews/r9-scan-settings-f52884a/coordinator-evidence/`.
That gate predates Library composition; full R9 language/browser/current-core
acceptance does not follow.

Library `d4d7992a` → `c4b4d57e` is **REVIEW_CLEAR only for bounded source/test
quality**: four source files and a 53-case dedicated set. The candidate remains
**RED: 52 passed / 1 failed** (41.31 s), broader **539 passed / 9 failed**
(130.42 s). Eight broader failures are stale parent assertions; the real
COUNTBOUNDARY failure previews one live item but unlinks two including Trash.
Source/tests/logs are frozen under
`.temp/swarm-reviews/r9-library-count-c4b4d57/`.

The operator selected **live-only visible unlink**, preserving Trash,
inaccessible entries, copies, history and private information without disclosing
hidden counts. Historical test-only `6bd04bda` reported **161 passed / 1 failed**
at missing watch retention. The approved live-view target update, internal
conditional watch-GC helper and exact lint entry are now frozen at `9df728c`:
bounded source review is clear and the parent gate passed **222 cases** below.
Watch-only references without metadata are retained. Actual household/current-core
acceptance remains open; rename policy and hidden-count disclosure are unchanged.

The original reconciliation is now complete: **219/219 exact original IDs,
186 source-closed / 33 still-open / 0 unfinalized**, at frozen `b049de8a`.
All six persisted batches under `.temp/r9-source-acceptance-b049de8/` preserve
original metadata, literal strings, bindings, reasons and order. The independent
`independent-ledger-gate.json` records **PASS_MECHANICAL_EVIDENCE_INTEGRITY_ONLY**:
**3017 assertions**, exit 0, **0.051 s**, 31 declared hashes, 120 expanded aliases
and 21 unchanged scoped files, with exact order/bindings/reasons. This verifies
the ledger, not final source, language or browser acceptance. The 33 baseline
open dispositions are not rewritten by subsequent fixes or added to overlapping
observations as new defect totals.

Accepted `ADJUDICATION_DECISIONS.md` overlays remain separate from that baseline:

- Count-independent aggregate category labels are valid; preserve exact bindings
  and the separate count-dependent series-row plurals.
- Hardcover's **total** selects volume plurality; missing uses a neutral label,
  never grammar inferred from the total. GNU sentinel controls are not fluency.
- Arrange is a page/action-label span, not a help hyperlink; restored-selection
  browser initialization remains unverified.
- Exact public filename `encryption.key` is technical data, never gettext copy.
  Only exact `NOT_COPY`/`technical_message` recognition is permitted, not a suffix,
  helper or per-file waiver; prose, other filenames and translated misuse must fail.

#### Parent frozen follow-up gates

These are new parent executions on the named frozen inputs, distinct from worker
reports. Result files are under `.temp/swarm-reviews/<archive>/coordinator-evidence/result.json`,
except the 717-case result under `combined-gate/result.json`.

| Frozen archive | Parent result | Verified inputs | Bounded selection |
|---|---|---:|---|
| `r9-live-only-9df728c` | **222 passed**, 73.98 s | 818 | Live-only Library/series/watch retention and lint |
| `r9-duplicate-pair-09d9b96` | **25 passed**, 23.41 s | 818 | Whole dismissed-duplicate pair, real route/GNU/shipped consumer |
| `portability-completed-form-55eeeac` | **62 passed**, 28.60 s | 834 | Completed-form disposal, not submitted-I/O drain |
| `r9-composed-shared-migration-6c74b97` + `b9b650bf` test overlay | **717 passed**, 139.05 s | 821 | Reviewed source plus six-path test migration, before later picks |
| `r9-wrapup-counts-30ae882` | **55 passed**, 42.27 s | 819 | Creator count basis; native canvas nontruncation unverified |
| `r9-archive-counts-6184de9` | **43 passed**, 51.60 s | 818 | Returned archive-plan totals and legacy fallback |
| `r9-technical-filename-a1c724b` | **108 passed**, 39.38 s | 819 | 25 new exact-filename/negative controls + 83 affected cases |
| `r9-neutral-missing-8922d1d` | **67 passed**, 53.48 s | 818 | Original 53 + 14 total/missing-count controls |
| `r9-count-fixtures-composed-582db82` | **1190 passed**, 225.15 s | 824 | Fresh 24-module affected union including the two-test fixture migration; source audit zero findings |

All nine exited 0 with zero JUnit failures/errors/skips and post-run input
hash/mode/guard checks. Bounded independent source/test reviews are clear for
these freezes only. Child telemetry is separately zero where instrumented;
`9df`/`09` record `child_guard: null`, not blanket child-process coverage. Counts
are not additive programme coverage and do not transfer to later compositions.
Language/assets, native browser, current-core/household, pinned and live-provider
acceptance remain separate.

Archive totals apply to **returned plans**: `records.append` is last in the item
try, and uncapped `items_archive_total` / `items_rejected` include item errors
before `errors[:20]` truncation. Existing classified verdict counts, error-detail
cap and import eligibility are unchanged. Legacy plans retain honest fallback,
not fabricated rejected totals. The technical-filename gate corrects only the
exact public basename; the **160 baseline catalogue diagnostics remain stale**,
not unfinalized ledger IDs or an audit waiver.

Local convergence picks run from `f52884aa` through `4297545d` / `932040cd` /
`d93e2b10` / `2ec2bc99` / `6c74b97d`, then test-only `b9b650bf`, then
`9ebb5882` (creator), `f496660d` (archive), `89a52343` (audit) and test-only
`0f9f499d` (neutral controls). The six-path `b9` migration is independently clear:
**10 RED → 10 PASS**, 6.73 s, plus two expected negative guards. The **717-case**
gate covers that earlier composition only, not the later picks or ME/Steam.

After the later picks, the parent reproduced four stale assertions (three
incoherent legacy archive fixtures and one creator key). The two-test overlay
corrects them and adds a single-verdict 0/1/2 summary control: **42 module passes /
16.91 s**, with **824** post-run tracked hashes/modes, unchanged guard and unchanged
app/static verified. Independent bounded review is clear and its exact overlay is
committed locally as `582db82d6be993e9dda987fb05e273387171f6e8`, with a clean inventory.
These changed controls use synthetic JS catalogue/DOM fixtures; routed-producer/GNU
coverage remains separately in the archive/wrap-up modules. The freshly frozen
24-module union passed **1190 cases / 225.15 s**, with all **824** tracked input
hashes/modes and both guards unchanged, zero child attempts and zero source-audit
findings. Evidence is under
`.temp/swarm-reviews/r9-count-fixtures-composed-582db82/`; the initial zero-test
selection error is retained and excluded, not application evidence. This is a new
bounded execution, not transferred 717 results. That gate did not promote source
or grant whole-R9 acceptance.

Those frozen R9 archives predate the current media composition. The coordinator
now reports central extraction/update/compile/check exiting 0, with **22 new empty
English-fallback msgids**, **two retired old neutral wordings**, and unchanged
same-key translations. These outputs are now frozen in `916a4dd6`, with the
generation/composed affected receipt above, but are not complete translations.
The two reviewed TMDb drafts
have no application receipt here. The `2fa9ff54` / `7caf79c1` / `f89df61c` /
`9a15c983` and **4718/798** preservation checkpoints remain historical, not
language or locale-journey completion; the 219-ID baseline ledger is unchanged.

### Portability and browser/offline dependencies

Frozen `93cc16d1` is **phase A only**: explicit TEMP `Artifact`/preview handles,
inode anchors, flock/fork lifetimes and filesystem-only startup recovery.
`build_export`/`build_legacy_export` return a closeable `Artifact`, and staging
returns a leased preview handle, not an unleased tuple. Source/API, worker logs
and manifest are copied under
`.temp/swarm-reviews/portability-temporary-phase-a-93cc16d/`.

| Historical `93cc16d1` worker gate | Result |
|---|---|
| Portability/recovery | **246 passed / 1 failed**, 216.20 s; phase-B final-artwork leak remains red |
| Focused archive/schema/write set | **412 passed**, 191.46 s |
| Adjacent catalogue/music/source set | **429 passed**, 161.21 s |

The initial independent resource/consumer reviews required changes for skipped
owner cleanup after close errors, obsolete inventories/wrong-boundary corruption
fixtures and multipart disposal masking the primary failure. Those frozen
findings remain in `.temp/portability-phase-a-review/FINDINGS.md`.

Follow-up `6d0f4407` is frozen under
`.temp/swarm-reviews/portability-disposal-followup-6d0f440/`. Rereview supports its
bounded consumer/RowStore/parser-disposal and inventory/index-fixture repairs;
the following remain **worker executions**, not overall independent clearance:

| `6d0f4407` worker gate | Result |
|---|---|
| Consumer/upload | **53 passed**, 39.86 s |
| All portability cases enabled | **271 passed / 5 failed**, 218.89 s |
| Focused archive/schema/write set | **412 passed**, 175.54 s |
| Adjacent catalogue/music/source set | **429 passed**, 151.67 s |

At `6d0f4407`, rereview remained **CHANGES_REQUIRED**: completed-form
`form.close()` after reservation exit could mask a downstream primary failure.
Follow-up `55eeeacfd0e564e4c1edb4dc768d4c42197d8a6b` repairs that bounded disposal
path and refines the contract. Its source/test review is clear; the separate
parent **62-pass / 28.60 s** gate is recorded above. Frozen worker history under
`.temp/swarm-reviews/portability-completed-form-55eeeac/worker-evidence/` remains:
**62 passed / 33.28 s**, broader **280 passed / 5 failed / 197.02 s**, and focused
**412 passed / 171.67 s**. The four real threaded open/read/seek/write failures
and phase-B artwork failure were not closed by that completed-form repair. Two
registered-app outer-final-send latch passes are **not I/O-drain proof**.

Historical `d6bdc2f` adds private `portability_io.Owner` and local adapters, without
changing `archive.py`. Its worker **88 focused passes / 52.91 s** and **304 passed /
1 phase-B failure / 205.23 s** did not clear rejected-close/parser cleanup or
accepted-helper/native-worker ownership gaps. The later bounded physical-ownership
repair is now reviewed and parent-tested; it is no longer merely repair WIP.

| Frozen archive | Parent result | Outcome and scope |
|---|---|---|
| `portability-physical-io-a1b9d37` | **109 passed**, 54.19 s | Bounded two-gap ownership repair clear; includes all 37 matrix and two registered final-send cases, not phase-A/X3 acceptance |
| `portability-rss-5be0fbc` | **RED: 332 passed / 2 failed**, 240.09 s | Bounded test-source review clear; new calibration cross-counter failure and unchanged precommit phase-B artwork failure remain |
| `portability-rss-calibration-b5f6aad` | **31 image passes**, 23.72 s; full parallel **333 passed / 1 failed**, 239.18 s; full serial **333 passed / 1 failed**, 419.88 s | Reviewed calibration-only correction; both full runs remain RED solely for unchanged phase-B precommit artwork |

Latest `coordinator-evidence/result.json` records verify **836 / 837 / 837** respective
tracked input hashes/modes, unchanged guards and zero child blocked attempts.
The RSS run retains the 37 matrix/two final-send cases. Its **original PNG/raw
128 MiB cap case passed**; the cap and raw measurement were not waived.
After-free VmHWM was **79,032 KiB**, below the earlier VmRSS **and** VmHWM
**79,236 KiB**; the same `getrusage` peak **79,032 KiB** persisted. These separate
counter observations are not an atomic invariant; the exact kernel mechanism
is unproven, and assertions after the failed comparison were not reached.

The `b5f6aad9` correction replaces the unsupported cross-counter dominance assertion
with the existing **60-MiB material-sensitivity floor** in both peak-report families,
retaining same-`getrusage` peak nondecrease, real 64-MiB touch/free, raw readings,
128-MiB limits, original PNG/raw criterion and genuine overbudget MemoryError.
Previously unreached tail assertions execute; parent raw-data checks verify them.
All 37 ownership and two final-send controls remain in both full gates. Earlier
RED receipts are retained, not reclassified. No kernel mechanism is claimed. Constructor,
signal/escalation and orphan ownership do not establish universal reaping; accepted
kernel I/O can outlive deadlines. Inventoried handoff diffs are not a wholly
clean-tree claim. Phase-B DB/incarnation/final-artwork work remains unapproved;
overall phase-A/X3/F8, full-size RAM and pinned-runtime acceptance remain open.

A fresh read-only diagnosis of the frozen `b5f6aad9` source confirms why this
failure cannot be repaired by broadening TEMP recovery: final JPEG installation
precedes COMMIT, its ownership is only in memory, and SIGKILL bypasses exception
cleanup. Invoked TEMP recovery expressly has no final-artwork/database authority,
with an existing positive control requiring artwork preservation. Separate
Phase-B exact-file ownership, durable COMMIT evidence, restore/incarnation
continuity and startup/current-core interfaces require design and review. Missing
receipts or references alone cannot prove rollback. On the existing RED, later
post-kill database/descriptor assertions are unreachable; live-cover digest
preservation also needs stronger additive observation. No new runtime result or
artwork-deletion permission is supplied by this diagnosis.

Additive test-only `98d1313b` now has independent **TEST_PACKET_REVIEW_CLEAR** and
fresh parent evidence: the new six cases pass **6 / 27.62 s**; separately, the
unchanged original 334 remain **333 passed / the same precommit failure / 402.37 s**.
All original identities/outcomes match captured worker evidence, all 839 inputs
and guards remain exact, and no child attempts occur. The 37 submitted-I/O
ownership cases, two named final-send cancellation cases and 31 image controls
all pass unchanged. These are separate invocations, not a green 340-case result.
The packet observes genuine precommit and post-SQLite-COMMIT/pre-hook boundaries,
fresh database state, descriptors and actual cover digests before verdicts. It
documents TEMP separation and retention; it does not repair final-artwork recovery
or approve durable commit/incarnation/core interfaces. No productive Phase-B grant.

Browser `36260af1` has bounded source clearance for all-status ownership, safe
429 handling and falsy aborts; password receipts and current-core Store/PWA are
excluded. Historical offline `1f5ad79` reported **40 browser / 63 server worker
passes**, but retirement stopped on consent-read failure before independent
captured-generation/legacy deletions. Follow-up `e8ae0890` now has bounded source
**REVIEW_CLEAR**: independent deletions are attempted despite that failure,
rejection waits for all attempts, original-generation retry preserves successor
and pending work, and blocked → late ERROR replacements retain both barriers.
Frozen source/worker evidence:
`.temp/swarm-reviews/offline-retirement-e8ae089/`.

Its **42 browser passes** (194.28 s) and **63 server passes** (38.16 s) are worker
results on the canonical **830 composition only**, not strict 362/current core.
Strict all-status ownership, current-core Store/PWA and password-receipt
independent gates remain pending. Earlier broader **87 passed / 1 failed** and
**190 passed / 10 failed** stay separately attributed RED; they were not rerun
or reclassified. Neither these domains nor other unclosed lanes inherit the
source candidate's full-browser/scale results.

All **F1–F8, R1–R10 and X1–X6** requirements remain in scope; all **185** original
acceptance checks stay unchecked, household/graph activation **OFF**, and Actions
settings unchanged under **continue local only**. Pinned Python 3.12/Playwright
1.52/container and named live-server/IdP/provider/licence acceptance remain
unverified. Direct Audible access is default-off; reference presence is not a
licence. No deployment, production/Portainer, external-key or operator-data
changes are part of these gates. These three documentation updates do not change
operator data, external keys, workflows or deployment configuration; referenced
parent executions are attributed separately, not inferred from documentation.

## Desktop spacing follow-up

**PASS — verified locally on 2026-11-13** with the compatible runtime above.

| Gate | Result |
|---|---|
| Complete non-live browser suite, including the all-page width/print audits and six toolbar widths | **405 passed**, exit 0, 548.08 seconds |
| Focused layout, navigation, labels/print, item/settings, feature-off and connected-library regressions | **720 passed**, exit 0, 134.12 seconds |
| CSS/service-worker regeneration, translation catalogs, collection badges and `make checks-fast` | Passed; private roadmap-map lint unavailable |

The operator found the unified pages too close to laptop screen edges. This
CSS-only presentation follow-up keeps the 96rem shell and desktop workspaces,
but changes desktop gutters from 32px to `clamp(48px, 5vw, 80px)` on each side
from 1024px. That is **64px at 1280px**, **68.3px at 1366px** and **80px at
1920px**; phone (16px) and smaller tablet (24px) gutters stay unchanged.
Navigation, content, public shares, Store and banners still share one inset.

The Browse bulk toolbar inherits the same gutter variable: its background can
bleed to the shell edge, but its controls stay aligned with page content.
Browser coverage checks both shell insets and toolbar alignment at phone,
tablet/laptop and wide-desktop sizes. Toolbar measurements await the actual
CSS transition rather than reading the temporary entrance scale, and seeded
barcodes remain distinct across all viewport cases. Print dimensions and
intentionally constrained authentication/artwork previews are unchanged.

Frozen verification candidate: staged tree
`53ea6e3c02e73ecef4853b807ebf485f409f410a` in `/tmp/shelf-gutter-verify`.
Logs: `/tmp/shelf-gutter-browser-verified.log` and
`/tmp/shelf-gutter-unit-verified.log`; visual/print artifacts:
`/tmp/shelf-gutter-verified` and `/tmp/shelf-gutter-music-verified`.
The 1366px all-page preflight and six toolbar-width cases passed before the
complete run. Application services, database schema/data and container settings
are not changed by this follow-up.
Laptop screenshots and label/valuation PDFs were inspected; both complete
browser and focused unit gates passed without skips. Only this verification
record was updated after the frozen run; the existing Starlette/httpx warning
was not suppressed.
The complete 7669-case unit suite was not repeated for this presentation-only
change; its immediately preceding full pass is recorded in the music section.

## Music matching, shop links and artwork selection

**PASS — verified locally on 2026-11-12** using the compatible runtime above,
not pinned CI. Both complete gates finished successfully on frozen checkouts.

| Gate | Result |
|---|---|
| Complete non-live unit/integration suite | **7669 passed**, exit 0, 1384.55 seconds |
| Complete non-live browser suite, including responsive/page-layout audits | **401 passed**, exit 0, 543.04 seconds |
| Final focused music, shop, cover, i18n and layout-site preflight | **316 passed**, exit 0, 58.79 seconds |
| Eight new browser journeys before the complete run | **8 passed**, exit 0, 46.10 seconds |
| README badge and test-convention follow-up | **8 passed**, exit 0, 26.91 seconds |
| `make css i18n badges checks-fast` | Passed; private roadmap-map check explicitly unavailable |
| Python compilation, JavaScript syntax, `pip check`, staged whitespace | Passed |
| Local Markdown link targets in the updated guides/README/architecture | **85 checked**, none missing |

New unit coverage exercises identifier-first matching and padding/fallback,
Lucene escaping, every music format's artwork dispatch, CAA redirects and album
fallback, Discogs tracks/images, public/pinned shop fetches, size/encoding and
redirect failures, inert structured metadata, distinct product variants, and
safe image signatures. Review cases cover user/item/expiry/purpose binding,
separate image tickets and login keys, stale writes before/during downloads,
staging cleanup, atomic image failure, duplicate-release rollback, preserving
shop tracks and canonical releases, feature/role/body gates, and unchanged
copies, ownership, memberships and private activity.

The five music journeys run at **320, 375, 768, 1280 and 1366 pixels**. They
verify selective Apply, persistence, independent artwork, Escape/focus return,
reopening, uploads and return navigation; three additional journeys use the
shared chooser on books, films and games. Provider-derived preview fragments
and images are offline fixtures; signed Apply, database writes, uploads and
navigation are real. No production data or signing keys are involved. Final
phone/laptop preview and gallery screenshots were inspected, alongside the
complete responsive/page-layout audit in the full browser suite.

The unit candidate is staged tree `e73ac2113885f43a45a99ae118ae6433a16d488e`
in `/tmp/shelf-music-verify`. The browser candidate is staged tree
`4aa850402318e04aaae9edaa8c5efde8504bf795` in `/tmp/shelf-music-final`.
Their application, static assets and unit tests are identical. Between them,
only README/verification documentation and the lazy Discogs-panel CSRF browser
assertion changed: the panel now has two POST forms, and the assertion checks
each form's actual token instead of assuming one token across the entire panel.
The first full browser run was **400 passed / 1 failed** on that stale count;
the second complete run passed without changing application code.
Only documentation was updated after both complete gates. The final warnings
were the existing Starlette/httpx deprecation and duplicate-ZIP-entry test
fixture; no warnings were suppressed and no tests were skipped.

Final logs: `/tmp/shelf-music-unit-verified.log` and
`/tmp/shelf-music-browser-final-verified.log`. Browser artifacts:
`/tmp/shelf-music-final-verified` and `/tmp/shelf-music-layout-final-verified`.

Bounded public smoke checks also retrieved a vinyl MusicBrainz match with nine
tracks and a CAA listing with six scans. A public Bandcamp album supplied its
2016 date, nine tracks and two images; the pinned image fetch returned a
342,230-byte image with HTTP 200. A different shop page failed with the manual
fallback message, and a subsequent MusicBrainz probe reported a transport
failure. These are sample observations, not a general shop/provider
compatibility guarantee; live CAA image download and authenticated Discogs
compatibility were not established. The complete test suites remain offline.

No database migration, dependency manifest or Portainer setting changed. Docker
builds, Cornelius deployment and the existing production catalogue were not
accessed here. Deployment still requires rebuilding the local test image and
recreating the existing container with pulling disabled, preserving its volumes,
ports, networking, environment and signing/encryption keys.

## Responsive layout verification (`644403d`)

**PASS — frozen layout follow-up on 2026-11-12.** This is the separately
published layout commit, not the later music implementation.

| Gate | Result |
|---|---|
| Complete non-live browser suite | **393 passed**, exit 0, 536.22 seconds |
| Focused layout, item/settings, labels, navigation, feature-off and connected-library regressions | **720 passed**, exit 0, 133.65 seconds |
| CSS/service-worker regeneration, translations, badges and `make checks-fast` | Passed; private roadmap check unavailable |

The routable-page audit checks shared shell width/gutters, visible controls,
horizontal overflow and stacked/desktop workspaces at **320, 375, 640, 768,
1024, 1280, 1366 and 1920 pixels**. Alternate populated/empty states, settings
panels, disabled features, authentication and print geometry are included.
Screenshots at phone/laptop widths and label/valuation PDFs were inspected;
authentication cards, camera/export previews and physical label dimensions
remain intentionally constrained. The fixes include the mobile Labels
dropdown and valuation scrolling/printing. A connected-library assertion was
updated to the new desktop columns, and destructive archive browser tests now
use their own disposable databases instead of relying on suite ordering.

Frozen checkout: `/tmp/shelf-layout-verify`; final logs:
`/tmp/shelf-layout-browser-verified.log`, `/tmp/shelf-layout-unit-verified.log`;
screenshots/PDFs: `/tmp/shelf-layout-verified`. No production data was used.

## Deployment follow-up: diagnosed pre-0.37 database

**PASS — scoped follow-up verification on 2026-11-12.** The operator's read-only
metadata identified a different migration namespace than `f161556` covered:
versions 24-66, a `location_nodes` copy FK, no location parent/label columns,
and no copy acquisition-source/provenance fields. A frozen fixture matching
those reported columns, FKs and ledger reproduced the namespace refusal before
the new bridge was added. No operator database was uploaded or modified here.

| Gate | Result |
|---|---|
| Expanded migration/schema, copy/location, provider-record, privacy, backup/restore, mutation and read/write-lint regressions | **857 passed**, exit 0, 196.32 seconds |
| Final `test_connected_pre037_upgrade.py` run, including an added overlapping-startup case | **20 passed**, exit 0, 23.30 seconds |
| Temporary-copy verifier success/failure and unchanged original files | Covered by the new suite |
| README badge regressions | **6 passed**, exit 0 |
| `make checks-fast` | Passed; private roadmap-map lint skipped because `.devdocs` is absent |
| Compilation and staged whitespace check | Passed |

The production implementation was unchanged between those two passing runs;
only the additional concurrency test and generated collection badge changed.
The new cases cover the actual copy FK replacement, old private state/music/
library retention, missing marks, child rows, copy sequence/custom index,
alias/binding conflicts, two initialization passes, real application lifespan
and pages, and original/converted database restore. An initial route assertion
used a nonexistent `/locations` page; it was corrected to the actual library
settings page rather than changing application behavior.

```bash
/tmp/shelf-new-test-compatible-venv/bin/python -m pytest \
  tests/test_connected_pre037_upgrade.py tests/test_connected_legacy_upgrade.py \
  tests/test_connected_library_schema.py tests/test_schema_parity.py \
  tests/*migration.py tests/test_items.py tests/test_personal_library.py \
  tests/test_connected_persistence.py tests/test_archive.py \
  tests/test_backup_passphrase.py tests/test_item_write.py tests/test_mutation_integrity.py \
  tests/test_hierarchical_locations.py tests/test_item_copies.py tests/test_location_order.py \
  tests/test_romm_records.py tests/test_komga_records.py tests/test_items_live_lint.py \
  -q --tb=short -n 4 --dist loadfile
```

That expanded command predates the final added concurrency case; the subsequent
20-case module run includes it. At that checkpoint, collection contained
**7564 unit tests** and **384 browser tests**. Neither the complete suite nor
pinned Python 3.12 CI was rerun there. The container-side
`python -m app.verify_upgrade` command in the guide allows the operator to check
a disposable copy with the real image
runtime before deploying; a successful Cornelius check is not claimed here.

## First deployment hotfix: older 0.37/0.39 fork databases (`f161556`)

**PASS — scoped legacy-upgrade verification on 2026-11-12.** This hotfix was
added after the original full-suite commit `1a06ee5`; the complete unit/browser
suites below were **not rerun for this hotfix**.

The deployed `no such column: status` failure was reproduced before the fix
using frozen older-fork DDL and migration descriptions. The compatibility
bridge now preserves original private-state fields/history, projects only
missing fields, and archives/translates known colliding migration identities
before the normal strict runner and auxiliary indexes execute.

| Gate | Result |
|---|---|
| Migration/schema, personal-state, backup/restore, archive and mutation regressions | **727 passed**, exit 0, 170.04 seconds |
| New `test_connected_legacy_upgrade.py` cases within that run | **29 cases**, including real lifespan/routes and pre-/post-upgrade backup restore |
| README badge regression tests | **6 passed**, exit 0 |
| Full historical schema smoke checks, `5d94d2e` and `6a46f467` | Both passed: bootstrap, upgrade, second boot, private-state preservation, foreign-key and integrity checks |
| `make checks-fast` | Passed; private roadmap-map lint unavailable as below |
| Python compilation and `git diff --check` | Passed |

The focused cases include retries after migration 59 and partial auxiliary DDL,
transaction rollback, overlapping startups, preserving newer edits and long
notes, valid/invalid progress scales, refusing unknown namespaces, and keeping
strict migration defects visible. Historical smoke checks extracted only SQL
constants from repository history into temporary databases, not user data.

```bash
/tmp/shelf-new-test-compatible-venv/bin/python -m pytest \
  tests/test_connected_legacy_upgrade.py tests/test_connected_library_schema.py \
  tests/test_schema_parity.py tests/*migration.py tests/test_items.py \
  tests/test_personal_library.py tests/test_connected_persistence.py \
  tests/test_archive.py tests/test_backup_passphrase.py tests/test_item_write.py \
  tests/test_mutation_integrity.py -q --tb=short -n 4 --dist loadfile
```

At this first hotfix, README badges reported collection of **7544 unit tests**
and **384 browser tests**; collection counts are not a claim that every test was rerun for the
hotfix. At that checkpoint no Docker engine or Python 3.12 interpreter was
available; these results do not establish current runtime availability.

## Full gates

**PASS — original implementation (`1a06ee5`) verified locally on 2026-11-12.**
These complete suites predate the deployment hotfix above.

| Gate | Result | Pytest duration |
|---|---|---|
| Full non-live unit/integration suite | **7515 passed**, exit 0 | 1339.24 seconds |
| Full non-live browser suite | **384 passed**, exit 0 | 1132.25 seconds |
| `make checks-fast` | Passed, exit 0; private roadmap-map check unavailable | — |
| `python -m pip check` | No broken requirements, exit 0 | — |
| `git diff --cached --check` | Passed, exit 0 | — |

No failed or skipped tests were reported by either suite. The private
roadmap-map lint explicitly skipped because `.devdocs` is absent; that private
mapping was **not verified**. Warnings were the existing Starlette/httpx
deprecation and the intentionally duplicated ZIP-entry fixture; no warning
suppression was added.

Commands:

```bash
PATH=/tmp/shelf-new-test-compatible-venv/bin:$PATH \
  make test PYTEST_PAR='-n 4 --dist loadfile'

PLAYWRIGHT_NODEJS_PATH=/usr/bin/node \
SHELF_E2E_CHROMIUM_EXECUTABLE=/home/opencode/.cache/michael-doctor-verification/chromium \
PATH=/tmp/shelf-new-test-compatible-venv/bin:$PATH make test-e2e

PATH=/tmp/shelf-new-test-compatible-venv/bin:$PATH make checks-fast
```

Unit and browser suites run in separate pytest invocations. The implementation
is frozen during each full gate; results from a changing tree are not counted
as final passes. Live contract tests and external media providers are excluded.
The final suites ran concurrently, and all post-test checks passed afterward.
The original staged candidate SHA-256 stayed
`a53b082d4c5ca4334a18573014d6d6c19e90d9c8c6b7b6a6baadd05a988b852b`
throughout both suites, with no unstaged or untracked files. Only verification
documentation was updated after that frozen run.

## Earlier evidence and fixes

- Unchanged baseline: **6267 unit tests passed**, with one pre-existing stale
  README badge failure; **372 browser tests passed** in this compatible runtime.
- First frozen branch unit run: **7472 passed, 9 failed** in 1366.95 seconds.
  It stopped before browser verification. The failures identified outdated
  Standard-profile/nav assertions, feature registry ordering, and a cover test
  still asserting the old provenance marker instead of the new durable block.
  Those assertions were updated to the intended contracts, not removed.
- Second frozen run: **7514 unit tests passed, 1 failed**; **382 browser tests
  passed, 1 failed**. The remaining cases were the ordered registry expectation
  and personal number fields squeezed at the 640-pixel breakpoint. The latter
  is fixed by delaying the three-column activity form until the wider tablet
  breakpoint, without weakening the existing responsive-control test.
- Third frozen run: **7515 unit tests passed** in 1343.63 seconds and **383
  browser tests passed** in 1133.16 seconds; offline checks, dependency consistency
  and staged whitespace checks also passed. A final presentation pass then
  constrained the account menu on short screens and stopped flex rows from
  stretching square music artwork. Their new real-browser regression and the
  final full run above include those changes.
- Focused source/catalogue hardening: **376 tests passed**, including malformed
  traversal, redirect scope, grouped totals, credential reflection and rejected
  compressed responses. No live server was contacted.
- A delayed-response browser probe reproduced a dropped Reset request when Save
  replaced its queued form. Shared in-flight control disabling replaces that
  unsafe queue; regression coverage verifies the real browser behavior.
- Review also found manual screen-item artwork falling through to book ISBN
  previews and a trailing-slash bypass of the metadata form's early size check.
  Both receive explicit negative regression tests.
- Initial mobile inspection found a 26-pixel copy-selector overflow and overly
  tall default filters/activity forms. The corrected layout passes **292 DOM,
  accessibility and layout checks**, with no failures or JavaScript/CSP/resource
  errors. All 29 sampled states fit their 375/1440-pixel viewports. On mobile,
  the Library form is 136 pixels high, results start at y=571, and the collapsed
  book activity panel is 266 pixels high. No provider or mutation request was
  made during that read-only inspection; direct pixel inspection is separate.
- The connected-library browser file now contains **12 passing journeys**,
  including delayed Save/Reset, error recovery, disclosures, mobile widths,
  short-screen menu scrolling and square music thumbnails.
- The complete responsive and connected-browser follow-up passed **47 tests**
  after the 640-pixel form fix; the final full browser run additionally includes
  the short-screen/artwork regression.
- Local Markdown target check: **197 targets in 13 changed files**, no missing
  targets at that point.

## Limits

- Pinned CI, Docker image builds, and compatibility against live media servers
  have not been established by these local tests.
- New connectors are bounded, read-only catalogue adapters, not native players,
  DRM handlers, an OPDS server, or remote progress synchronization.
- New source bindings, private activity, saved views, provenance and artwork
  decisions require a full database backup, not a portable collection archive.

See the [user guide](user-guide/connected-library.md) for supported types,
configuration, availability semantics and backup/security boundaries.
