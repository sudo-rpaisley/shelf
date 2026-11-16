# Feature work progress

The live checklist is [NEXT_ACTIONS.md](NEXT_ACTIONS.md). Entries here distinguish
implementation and verified outcomes from investigation or blocked acceptance.

## 2026-11-16 — Unwired account-preference storage published

**[`c010b07e`](https://github.com/sudo-rpaisley/shelf/commit/c010b07e431f87827f5a7214bb2e4d96979d7dc1)**
is pushed and remote-verified on `new-test` after independent
**PASS_BOUNDED_PREF_STORE_BATCH**. This completes **PREF-STORE-1 only**: a bounded
SQLite storage/CAS prerequisite, not working preferences pages or complete
F2/ME5/X5/ME7. No HTTP route, auth behavior, consumer or editor is wired to it.

The store supports `recent`, integer `item_size_percent` in **50/60/70/80/90/100**,
and an account-owned canonical `default_saved_view` ID string. Immutable supported-only
snapshots and revision CAS preserve freshly reread original JSON and opaque fields.
The caller owns the transaction/commit and must supply the captured actor and verified
entry-cookie expiry; constructing the actor object is not authentication. Schema scope
is exactly **45 additive lines** in `app/database.py`; the service, three new test
modules and generated README collection badge complete the six-path batch.

### Fresh bounded qualification

All nine bands use the same **979 frozen source paths**: **1,575 disjoint passes**,
zero failures/errors/skips, recorded external attempts or source/guard drift.

| Band | Passes | JUnit seconds |
|---|---:|---:|
| New storage/schema/native SQLite | 569 | 277.981 |
| Retained schema/registration | 74 | 65.901 |
| Retained historical migrations | 152 | 232.413 |
| Retained legacy upgrades | 49 | 84.581 |
| Retained transaction callbacks/cache | 9 | 7.266 |
| Retained auth/account/library | 378 | 246.203 |
| Retained source handoff/progress | 240 | 233.001 |
| Retained backup/restore | 27 | 21.298 |
| Retained native editor | 77 | 77.333 |

The 569 new cases include **147 meta/oracle controls and 422 implementation-dependent
cases**; the remaining **1,006** are retained regressions. Offline checks pass and
official generation converges. README **12591 / 799** counts collection, not full-suite
passes. All **435 original / 486 batch-base test blobs and modes** and all **185
unchecked original acceptance entries** remain exact. No assertion weakening, skips
or xfails were used. Visible Starlette/`record_property` warnings remain disclosed.

Historical failures remain preserved, not counted as final passes: the identical
three-case malformed-TEXT revision probe went genuinely RED→GREEN after an integer-only
decoder repair. The earlier composed **480-pass/4-fail** run exposed independently
diagnosed new-test fixture/oracle issues, repaired without further production changes.
The separate cache meta-fixture RED and its narrow caller-query correction are retained.
The earlier incomplete step-limit audit is superseded by the completed final PASS.

Independent verification checked all source hashes/modes/Git/index entries, **524
sealed evidence files / 28,474,542 bytes**, metadata and historical bindings. Evidence:
`.temp/feature-completion-20261116/PREFERENCES_FROZEN_CANDIDATE.json`,
`PREFERENCES_FINAL_RUNTIME_RECEIPT.json`, `PREFERENCES_FINAL_EVIDENCE_MANIFEST.json`,
`PREFERENCES_FINAL_INDEPENDENT_GATE.json` and `PREFERENCES_PUBLICATION.json`.
Source fingerprint: `408c4efe807b6107585e7c9a2e8f390a93844478c756daa4250a23c9336b3756`.
Evidence fingerprint, recorded order:
`87385ded74dbe651e288cebbf69358e9ca8166bc130141d31b2fdaa96f8de1dc`.

This is compatible-local **Python3.14.8/SQLite3.53.4** qualification, not the full
suite, pinned Python3.12/container, browser or provider/account acceptance. Collision
preservation covers logical schema/rows/ledger, not WAL/filesystem bytes; later bootstrap
failure may retain the committed additive table. The separate native busy probe proves
zero-timeout handling, not measured nonzero waiting or global cold-start correctness.
Concurrent cold-start ledger races and restore/auth token-version collisions remain
open; only quiescent restore invalidation is covered. Selected native backups retain
the table; the closed portable archive still excludes it.

`main` remains `43e244f5`; held integration, deployments, tags/images, Actions and
household/graph activation are untouched. HTTP/UI, response-cookie ownership and
actual consumers need separate contracts and qualification. The documentation-only
successor does not claim another runtime run on its SHA.

### Next UI priority: unified Add Media

The user's next priority is one adding page, with inline type/format, title/identifier,
artist/platform, results, preview and manual actions rather than separate music search.
**Planning only, not implemented or qualified.** Preserve legacy Scan/API behavior and
all eight modes, native save paths, exact-release safeguards and MusicBrainz-first
cassette fallback. Catalogue numbers are not barcodes or provider release IDs; Library
search must not silently broadcast private queries to providers. The detailed queue
keeps **ADD-01** unchecked pending bounded contracts, implementation and fresh evidence.

## 2026-11-16 — Barcode-free cassette and UI repairs published

**[`fa90abdb`](https://github.com/sudo-rpaisley/shelf/commit/fa90abdb550fed6ae123ad5aa521414e94adf56a)**
is pushed and remote-verified on `new-test` after independent
**PASS_BOUNDED_APPLICATION_BATCH**. On **Music**, choose **Cassette**, enter
**Hits Out of Hell** and **Meat Loaf**, leave the barcode blank, then compare the
possible pressings before saving. Your exact pressing is not inferred or selected
on your behalf. See the [Music guide](user-guide/music.md#add-a-cassette-without-a-barcode).

The batch delivers genuine-empty-only public Discogs fallback after MusicBrainz,
explicit signed review/selective saving and a prefilled native manual alternative.
Scan retains current Add/Wishlist intent. Existing-item format contradictions,
duplicates/Trash references and stale reviews refuse before artwork/writes and
recheck under the write lock. Discogs IDs never become MusicBrainz IDs; tracks are
live details, not copied into canonical MusicBrainz tables. Copies, locations,
history and existing MusicBrainz metadata remain intact.

It also repairs blank-cover review, label stock/start controls, scoped Discover/
copy-confirmation translations and CSP-safe shortcut help. The palette's reproduced
duplicate-native-close cleanup is single-use and preserves deliberate outside
focus; historical failure scheduling remains unknown. The editor protocol stays
**unwired**.

### Fresh combined qualification

All bands use the same **975 frozen source files**, with **1,586 disjoint unit /
92 disjoint browser passes**, zero failures/errors/skips or recorded external
attempts, matching guards and raw before/after input manifests:

| Band | Passes | JUnit seconds |
|---|---:|---:|
| Cassette plus retained music/Discogs | 324 | 265.745 |
| Cover/labels/i18n/copies/help/palette/navigation | 455 | 326.328 |
| Protocol plus native editor controls | 807 | 523.234 |
| English cassette workflows | 12 | 48.122 |
| Separate Italian/Chinese cassette previews | 2 | 11.066 |
| Original ordered palette/help/CSP plus lifecycle | 56 | 70.322 |
| Labels plus real Discover/copy locale journeys | 17 | 66.680 |
| Upcoming plus retained Release Radar | 5 | 27.443 |

Offline checks pass and generation converges. README **12022 / 799** records
collection, not full-suite passes. Independent verification checked all source
hashes/modes/blobs/index entries, **151 sealed evidence files**, 18 supplementary
references, the exact 52-path scope and all eight actual screenshots. Source
fingerprint: `01ea86fb2a8d6ef6bbf31a662a7e2f8a61a3c64d34e9bc3194b340026466f447`.

All **435 original tests/modes** and the original185 acceptance SHA remain exact.
Of **469 published-base test files**, 468 are exact; the remaining file changes
only the previously ratified post-original consumed-ARIA/no-onclick assertion.
The new cassette tests retain their original 142 unit/four browser identities;
only the separately approved stronger status/title/no-error receipt oracle replaces
an unsupported capitalization assumption, with a real POST and 20 counterexamples.
No skips, xfails or weakened production assertions were used.

### Localization and honest evidence limits

The actual catalogs compile **63 scoped new messages / 126 Italian-Chinese values**,
preserving active existing translations and explicitly retiring one obsolete Discogs
notice per locale. Cassette runtime checks cover eight visible new messages per
language, not all 50 messages or native-language certification. Broad R9 remains open.

Initial Chinese text/state checks passed, but screenshot inspection found the local
OpenSans-only harness lacked Han glyphs. Those images were **not** accepted as visual
proof. An already-local embedded Droid fallback was copied into an isolated test
font configuration—no installation/download, native-library execution or application
font-asset change. Fresh affected two-case and 17-case runs pass, and Chinese1280
glyphs and Italian320 layouts are visibly readable. Chinese320 cassette layout is
not claimed. The initial three-case Upcoming selection also omitted retained Release
Radar; the complete original two-file five-case rerun passes. These earlier runs
are preserved but excluded from final unique totals.

Evidence: `.temp/feature-completion-20261116/FINAL_APPLICATION_FROZEN_CANDIDATE.json`,
`FINAL_APPLICATION_RUNTIME_RECEIPT.json`, `FINAL_VISUAL_INSPECTION.json`,
`FINAL_APPLICATION_INDEPENDENT_GATE.json` and `FINAL_APPLICATION_PUBLICATION.json`.
Evidence fingerprint: `94914b5dbc1424cffaf3e00c5a3208f28590943ebb2da7c5326e6741d11b98ac`.

This is compatible-local Python3.14.8/Playwright1.65.1/native Chromium qualification,
not the full suite, pinned Python3.12/declared browser/container, live-provider/account
or whole programme. Preferences/CAS, complete editing, persistent indexing/scale,
critical restore/core and the wider audited queue remain open. `main` stays at
`43e244f5`; the held integration, Actions, deployments, tags/images and household/
editor activation are untouched. This documentation-only successor does not claim
another runtime run on its new SHA.

### Storage planning at that publication (historical)

Requirements and critical-schema design reviews cleared **PREF-STORE-1** after
explicit amendments. Separate isolated writers now own the unwired domain store,
the centrally owned companion-table/bootstrap change and three independent new test
modules. No preference implementation has been selected or published yet.

The contract specifies strict supported fields, preserved original JSON, account
revalidation and revision-CAS under the caller's transaction. Schema collision checks
preserve logical data/ledger, not prior WAL/filesystem effects; a later bootstrap
failure may leave the committed additive table. Existing concurrent cold-start and
restore/auth token-version collision limits are not repaired or waived. HTTP/UI,
cookie-response ownership, browser storage and actual consumer integration require
separate grants and qualification. No F2/ME5/X5/ME7 completion is claimed.

The following checkpoints retain their outcomes **when recorded**; the latest
published section above supersedes their scoped pending notes, not their history.

## 2026-11-16 — Dormant editor-protocol prerequisite published

**[`dbc948f1`](https://github.com/sudo-rpaisley/shelf/commit/dbc948f19fb7609f68639c653c4d9a806ceb7f1e)**
is pushed and remote-verified on `new-test`. It adds only the pure, **unwired**
protocol module, its independent tests and the actual collection-count badge.
No request path, schema, permission, editor or household behavior is activated.

Fresh qualification: **807 passes / 382.56s**—730 protocol cases and 77 retained
native-editor controls—zero failures/errors/skips, unchanged inputs/guards and zero
external attempts. Offline checks pass. Independent **PASS_BOUNDED_DORMANT_BATCH**
verified all 957 source hashes/modes, 39 evidence files, exact three-path scope,
data-only purity and absence of application callers. All 435 original and all 468
base test files retain exact blobs/modes; original185 remains unchecked and exact.

The test review strengthened swallowed-exception detection, stored-ID positivity
and raw-path echo provenance. The earlier annotation-representation failure remains
recorded; a one-line finite-Literal normalization passed the fresh final suite.
README **11725 / 762** counts collection, not full-suite passes. This is targeted
compatible-local evidence, not active-editor, full-suite, pinned/container or main
readiness. `main` remains unchanged; no deployment, Actions or account change.

### Application composition continues

The reviewed UI composition passed **444 units / 24 browsers**, including real
Italian/Chinese catalogs and native confirmations. A separately reproduced palette
duplicate-native-close defect now has reviewed **185-unit / 56-browser** repair
evidence; the historical failure's exact schedule remains unknown. These increments
are being composed with cassette fixes, then rechecked on one frozen application
candidate rather than transferring earlier results to the combined tree.

Cassette review also found and repaired lost Scan Wishlist intent, contradictory
CD/vinyl attachment to an existing cassette, and failure-to-no-match classification.
New regressions and stricter selective-creation/receipt oracles preserve the actual
producer/consumer contracts. Final combined units, real cassette browsers, locale
checks and publication are still pending. **Cassette entry is not yet marked done.**

## 2026-11-16 — First application repair batch published

**[`b4cfd3e4`](https://github.com/sudo-rpaisley/shelf/commit/b4cfd3e4439e2601d682c3fca249ca234462704b)**
is pushed and remote-verified on `new-test`:

- **FLOW-RADAR:** Upcoming hides Series-dependent links/actions when Series is off,
  while retaining readable announcements, wishlist/dismiss actions and role gates.
- **X4-BULK-RECEIPT:** bulk update selects unique live targets under its write lock;
  missing/duplicate/trashed IDs neither inflate receipts nor mutate Trash.

Fresh parent qualification: **383 units / 305.10s**, **5 browsers / 28.94s**, zero
failures/errors/skips; offline checks pass. Generation converged. Independent
**PASS_BOUNDED_BATCH** verified all 955 source hashes/modes, 63 evidence files and
the exact six-path change. All 435 original and all 465 batch-base tests retain
their exact blobs/modes; the original185 checklist remains unchecked and unchanged.
README badges **10995 / 762** are collected counts, **not** suite pass totals.

Selection fast-forwarded the exact tested candidate; no source edits followed
qualification. `main` stayed at `43e244f5`; no deployment, tag/image or Actions change.
This is compatible-local targeted acceptance, not full-suite, pinned/container,
global-CSP, programme or merge readiness. The inherited shortcut CSP work remains
separately open, and its broader palette-focus failure is not waived.

The cassette has independent **142 unit / 4 browser** cases awaiting composition
with its source candidate. Public research now pins full UK/Canada track ordering
and European subtracks. No exact pressing has been attributed to the user's tape.
The dormant editor protocol's C1/C2 contract is independently cleared; module and
test implementation remain separate from any active editor or authority wiring.

This status-document update does not claim another runtime run on a docs-only SHA.

### Follow-up qualification in progress

The first combined cassette unit gate produced **252 passes / 3 failures** with
unchanged inputs/guards and zero external attempts. Two failures expose a duplicate
MusicBrainz search path bypassing the established coordinator; the third requires
a real Scan manual-entry link alongside the new prefilled native form. A two-file
source repair is active; the 142 independent tests are unchanged. **The cassette
feature is not yet qualified or published.**

The reviewed cover/labels/localization/CSP candidates are composed privately.
Thirteen scoped messages now have real Italian and Simplified Chinese translations;
all existing nonempty translations were preserved. Compilation and asset generation
converged. Fresh unit and real-locale browser gates are running—not yet reported
green. Cassette's separate 48-message translation proposals are ready for adoption
after its source freeze.

The broader palette-focus failure remains unwaived. Its queued-native-close cause
is a hypothesis, not established attribution; a bounded passive trace and controlled
lifecycle witness are being executed without protected-test edits or added sleeps.
The dormant editor protocol's independent tests are also being strengthened so a
swallowed bomb exception cannot hide forbidden method calls.

## 2026-11-16 — Parallel feature completion wave started

**Priority example:** add **Meat Loaf — Hits Out of Hell** as a cassette without a
barcode. Separate agents are researching public metadata and tracing the existing
entry workflow. No exact pressing or successful add has yet been demonstrated.

Additional agents are reconciling the unfinished feature inventory, triaging
recorded RED evidence and inspecting independent user-facing gaps. Productive jobs
will have finite file ownership and evidence requirements; an old experimental
failure is not automatically a failure of current `new-test`.

The user authorized incremental GitHub updates. Reviewed, tested code batches will
be pushed to `new-test`; the first documentation batch establishes the live queue.
No new application batch has been qualified or published in this wave yet.

The separate main integration remains an isolated, uncommitted merge. Its previous
UI passes do not qualify the merged result. Pinned CI/container acceptance remains
blocked under the existing local-preparation decision. No Actions settings,
production data, provider accounts, release tags/images or deployment are changed.

### Cassette metadata research

Public, unauthenticated catalogue GET requests found useful barcode-free metadata:

- [Discogs artist/title/cassette search](https://api.discogs.com/database/search?artist=Meat+Loaf&release_title=Hits+Out+Of+Hell&format=Cassette&type=release&per_page=5&page=1)
  returned **20 cassette editions**.
- Verified examples: [UK 1984 release 3935124](https://api.discogs.com/releases/3935124),
  Epic **EPC 40-26156**; [European 1984 release 8297774](https://api.discogs.com/releases/8297774),
  Cleveland **40-26156** / Epic **EPC 40-26156**; and
  [Canadian 1995 reissue 18670711](https://api.discogs.com/releases/18670711),
  Epic/Cleveland **BT 57615**. Track order and durations differ across editions.
- MusicBrainz returned album releases but **zero** results with its cassette-format
  filter. That provider gap must not be presented as proof that no cassette exists.
- Release-group artwork found in the research belongs to a CD release: it is only
  an **album-artwork fallback**, not verified artwork for the user's cassette.

**CAS-01 is complete as research only.** The application journey, exact pressing
selection, saving, fallback and regressions remain open. No provider account or
credentials were used, no remote data was modified, and image files were not fetched.

### Independent repair lanes

Source tracing identified three bounded gaps: blank-path missing covers cannot
reach Cover Review; assigned label ranges have no stock/start selector on the
print sheet; Upcoming offers Series-gated actions even when Series is disabled.
Each has a separate worktree and writer, with new regression files only. They are
not marked repaired until their fresh checks and independent reviews pass.

Recorded core and artwork REDs were attributed to held experimental candidates,
**not newly demonstrated failures of published `new-test`**. Their critical
prerequisites remain explicit. A separate planner is completing the dormant editor
protocol's C1/C2 contract before any pure-adapter writer is granted.

### Fresh baseline and candidate checks

- Retained music baseline: **113 unit tests passed / 59.91s**, unchanged inputs and
  offline guards, zero external attempts. This precedes cassette implementation;
  it does not demonstrate the new cassette journey.
- Missing-cover repair candidate: new regressions first produced **11 failures /
  5 passes**; the fixed retained band produced **103 passes / 60.75s**.
- Upcoming controls candidate: new regressions first produced **7 failures /
  21 passes**; the fixed band produced **83 unit passes**, plus **3 browser passes**.

Both repair candidates are frozen for independent review and parent composition
checks. No code batch is marked complete or published from these writer results
alone. Additional writers now cover the Discover/copy-confirmation translation
gaps and truthful bulk-update receipts. The audited programme queue has been
expanded without reclassifying existing implementations as missing or marking
blocked acceptance complete.

The label-print candidate passes **37 unit tests**, but its new browser journeys
fail at the unchanged global CSP guard after their functional assertions pass.
The existing CSP suite independently reproduces **2 failures / 1 pass** on unrelated
pages. A new shared-shell lane is removing the three inline shortcut handlers at
source and replacing English-title-dependent bindings with stable hooks. The label
feature remains blocked—not relabelled browser-green.

Bulk-update receipt regressions reproduced **8 failures / 28 passes** before the
fix. The candidate now passes **36 new cases**, plus separate **28 retained bulk**
and **9 value-guard** cases. Independent review and fresh parent composition remain
pending. None of these candidate counts is a full-suite or pinned-runtime claim.
