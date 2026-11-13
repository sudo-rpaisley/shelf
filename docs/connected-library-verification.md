# Connected-library experiment: verification

Branch: `new-test`, based on `100219d`. This is an experimental branch, not a
release or a claim of compatibility with every OPDS/Jellyfin server.

## Runtime

Local verification uses Alpine aarch64, Python **3.14.8**, and the isolated
interpreter `/tmp/shelf-new-test-compatible-venv/bin/python`. All direct Python
requirements match the repository pins **except Playwright 1.65.1 instead of
1.52.0**. The pinned Playwright package could not be installed on this runtime.
Browser runs use native Chromium **155.0.8059.84** and `/usr/bin/node`.

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

Commands used the compatible interpreter and native browser described above:

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

The latest published test-only follow-up is
`89388e317689686b6e1023bc31492abbc0db544e`; its application/static/harness/
dependency objects remain identical to source integration
`1cf3c0b6560dc2d0b5d7cd00ecd1a2008b5351d0`, whose parents are `fdd51186` and
tested source `89205f44`. The earlier `2fb43756` / `717dfeb6` checkpoint is
historical. Core/R9/portability/offline follow-ups remain separate and local;
this is not a release. Evidence paths are ignored local copies, not CI artifacts.
Coordinator/worker reports are distinguished from independent gates; no tests
were run by this documentation task.

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

The actual one-native façade and permanent dual DENY-wins guards are retained,
not repaired by this replay. Subsequent independent read-review corroborates
**92/25 and the phase classification**; production/API-compatibility readiness
remains **CHANGES_REQUIRED**, not a fresh test run or source repair. The parent
has reserved one writer for actual-façade files and a separate test-only managed
reentry lane. The core owner's compatibility proposal is **not production-repair
approval**. R2/private admission, F8 cold startup and activation remain blocked,
not cleared by fixture initialization.

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
Q1/Q2/Q4/Q6 only**. Q3/Q5 still need technical-owner acknowledgement; Q7–Q9 are
unapproved. The reserved writer alone does not close Q9's frozen-composition/
verification gate. These are contract decisions, **not available APIs or
productive implementation**; Q8's actual-core repair remains unfixed.

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
**Library is not composed**; full R9 language/browser/current-core acceptance
does not follow.

Library `d4d7992a` → `c4b4d57e` is **REVIEW_CLEAR only for bounded source/test
quality**: four source files and a 53-case dedicated set. The candidate remains
**RED: 52 passed / 1 failed** (41.31 s), broader **539 passed / 9 failed**
(130.42 s). Eight broader failures are stale parent assertions; the real
COUNTBOUNDARY failure previews one live item but unlinks two including Trash.
Source/tests/logs are frozen under
`.temp/swarm-reviews/r9-library-count-c4b4d57/`.

The operator has now selected **live-only visible unlink**, preserving Trash,
inaccessible entries, copies, history and private information without disclosing
hidden counts. `app/routers/series.py` was unchanged/read-only at c4; only the
new bounded unlink follow-up is approved. The coordinator reports that test-only
`6bd04bda` adopts the live-only expected set: **161 passed / 1 failed**, then stopped because
watch retention lacks a supported capability. Metadata GC is correct, but a
watch-only entry without metadata must retain hidden references. The parent has
now approved only a narrow internal conditional watch-GC helper, its exact lint
entry and live-view target update. Implementation is active, **not yet passing**,
not in f528 and not current-core/household acceptance. Rename policy and
hidden-count disclosure remain unchanged.

The original **219-record** reconciliation is still **incomplete**. Four
persisted batches cover **160 exact original IDs: 142 source-closed / 18
still-open** at frozen `b049de8a`; **59 remain unfinalized**, not 59 defects.
`batch-01` through `batch-04` `dispositions.json` files under
`.temp/r9-source-acceptance-b049de8/` retain original metadata/reasons/bindings
and the mechanically joined read-only audit returns. The earlier two batches
accounted for 80 IDs (70 closed / 10 open); batch 03 added 40 (35 / 5), and batch
04 adds 40 (37 / 3). This is source inspection, not new execution. The final two
bounded batches remain active; no finalized result is claimed at capture. A prior
batch-05 attempt exhausted its inspection capacity without reading or finalizing
any records, so fresh read-only sessions own the remaining disjoint batches.
Later repairs need reviewed
source overlays, never retroactive baseline-count changes. At b049 the three unlink controls remained
open because the same-action success toast still said books. These dispositions
overlap earlier observations; do not add them as separate defect totals.
The multi-location `dcec0769d87dc33c6018e036` finding also retains a dismissed-
duplicate title-pair concern. A separate whole-message follow-up is active;
reviewed CSV/ABS successors alone do not close every binding. Catalogues/assets
remain stale; two
independently cleared Italian/Simplified-Chinese TMDb drafts remain unapplied.
Resolve semantics, then perform fresh English extraction/authorship/language
review and central regeneration. The `2fa9ff54` / `7caf79c1` / `f89df61c` /
`9a15c983` and 4718/798 preservation checkpoints in the delivery record remain
historical evidence, not translation or locale-journey completion.

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

Rereview is still **CHANGES_REQUIRED**: bare completed-form `form.close()` runs
after reservation exit and can mask a downstream primary failure. The owner's
bounded repro/fix and contract refinement are active, with no closure result at
capture. Four enabled RED cases exercise **real threaded open/read/seek/write**
ownership, not merely async producers; the fifth failure remains phase-B final
artwork. Two actual registered-app outer-final-send latch controls pass, but
**are not I/O-drain proof**.

The proposed operation-level submitted-work drain contract still needs ownership
before queue/submission, preservation of the first competing primary exception's
identity plus observable late I/O errors, returned `UploadFile`/completed-form
cleanup, and repeated raw-task/AnyIO cancellation coverage. Broader thread
implementation is **not approved**. A stalled kernel call cannot be forcibly
cancelled; retaining its lease is not bounded cancellation latency.

Tracked source was clean at freeze, but untracked `ACTUAL*.diff` handoff artifacts
are inventoried; this is not a wholly clean-tree claim. Phase-B DB operational
tables, incarnation and final-artwork reaping remain **unapproved**. No overall
X3/crash-recovery or new large-fixture memory pass follows from phase A.

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

All **185** acceptance checks stay unchecked, household activation **OFF**, and
Actions settings unchanged under **continue local only**. Pinned Python 3.12/Playwright
1.52/container and named live-server/IdP/provider/licence acceptance remain
unverified. Direct Audible access is default-off; reference presence is not a
licence. No deployment, production/Portainer, external-key or operator-data
changes are part of these gates.

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
hotfix. The same compatible Python 3.14 runtime limitations above still apply;
no Docker engine or Python 3.12 interpreter is available in this environment.

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
