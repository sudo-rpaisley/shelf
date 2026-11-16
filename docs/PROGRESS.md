# Feature work progress

The live checklist is [NEXT_ACTIONS.md](NEXT_ACTIONS.md). Entries here distinguish
implementation and verified outcomes from investigation or blocked acceptance.

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
