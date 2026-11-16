# Live feature work queue

Updated: **2026-11-16**. This is the working checklist for the current development
wave on `new-test`, not a release-readiness claim. See [Progress](PROGRESS.md) for
completed batches and evidence.

An item is ticked only after its implementation, regression checks and independent
review are recorded. **In progress**, **unverified** and **blocked** are not done.
The original [acceptance checklist](connected-library-acceptance.md) is unchanged;
this queue does not waive or automatically complete any of its 185 entries.

## Priority: add a cassette without a barcode

Example: **Meat Loaf — Hits Out of Hell**, physical cassette. Album metadata alone
does not identify the exact pressing, country, catalogue number or release date.

- [x] **CAS-01 — Find usable metadata.** Verify public cassette candidates using
  artist and title; record source links and unresolved pressing differences.
  **Research complete:** Discogs returns 20 cassette editions; verified UK/European
  1984 examples include catalogue number EPC 40-26156. MusicBrainz's cassette
  query returns no matches. No exact pressing has been selected or added yet.
  Sources and limits: [Progress](PROGRESS.md#cassette-metadata-research).
- [ ] **CAS-02 — Make the entry path usable.** Reach title/artist search without a
  barcode, select cassette format, and show useful candidate choices.
  **In progress:** design ratified; separate implementation and regression writers.
- [ ] **CAS-03 — Save the chosen release correctly.** Preserve cassette format,
  artist/title, available tracks and source identity; do not invent an exact
  pressing or silently replace user-entered data.
  **In progress:** bounded source implementation and independent regression authoring.
- [ ] **CAS-04 — Keep a useful fallback.** No match or provider failure must leave
  a usable manual-add path without requiring a barcode.
  **In progress:** manual unknown-pressing path is part of the ratified contract.
- [ ] **CAS-05 — Prove the complete journey.** Add deterministic offline unit and
  browser regressions for the example, competing formats, ambiguity and failures;
  retain all existing assertions and strict CSP/CSRF protections.
  **In progress:** 142 unit and 4 browser cases authored independently; composition
  first produced 252 passes / 3 failures. Source repairs now cover Wishlist intent,
  existing-item format contradictions and honest provider failures. Strengthened
  regression composition and fresh browser qualification are pending.
- [ ] **CAS-06 — Publish a reviewed development batch.** Record checks and push
  the tested result to `new-test` for further testing.
  **Pending:** qualification; no deployment or `main` merge.

## Remaining programme: discovery and repair

These are known open groups, **not a claim that the published branch currently
fails every associated test**. Held experimental candidates and stale RED results
must be identified before a repair is selected. The inventory agent will expand
these groups into finite, dependency-ordered jobs.

- [ ] **BACK-01 — Reconcile unfinished/unstarted/broken work.** Produce an
  evidence-backed inventory from the delivery, verification and roadmap records.
  **In progress:** requirements audit.
- [ ] **REG-01 — Revalidate recorded failures.** Separate current defects from
  failures belonging only to held candidates; identify small repair packets.
  **In progress:** regression-evidence triage.
- [ ] **FLOW-01 — Repair independent user-facing gaps.** Inspect optional feature
  entry points and select confirmed, bounded defects for separate writer lanes.
  **Static audit complete:** three concrete repairs below have separate writers.
- [ ] **FLOW-COVER — Make missing-cover review reachable.** Needs Attention
  recognizes blank/whitespace paths, but Cover Review currently excludes them.
  **Source review clear:** revised candidate passes 113 units, including correct
  blank-path display. Parent composition/publication still pending.
- [ ] **FLOW-LABEL — Preserve label print selections.** Allow stock/start changes
  while retaining the exact newly assigned label range.
  **Blocked candidate:** 37 units pass; browser journeys reach their final CSP
  guard and fail on inherited shortcut-help inline handlers. FLOW-CSP owns the fix.
- [x] **FLOW-RADAR — Remove feature-gated dead controls.** Upcoming must not offer
  Series-dependent actions when Series is disabled but radar remains enabled.
  **Published:** `b4cfd3e4`; independent bounded gate, fresh parent checks below.
- [ ] **R9-DISCOVER / R9-COPY-CONFIRM — Finish these translated surfaces.** Mark
  Discover's interface and the copy-specific Trash confirmation safely, then verify
  real catalogs, placeholders and attribute escaping.
  **In progress:** reviewed templates/tests are composed with actual Italian and
  Simplified Chinese catalogs. Fresh real-locale browser checks are running.
- [x] **X4-BULK-RECEIPT — Report real bulk-update results.** Duplicate, missing and
  trashed IDs must not inflate the affected count or mutate Trash.
  **Published:** `b4cfd3e4`; unique live selection and receipt under one write lock.
- [ ] **FLOW-CSP — Repair shortcut help without inline handlers.** Remove the
  three handler attributes at source and bind translated controls through stable
  data hooks. Preserve the unchanged existing CSP assertions and shortcut actions.
  **In progress:** controlled native-close witnesses reproduced a duplicate-cleanup
  defect on baseline and candidate. The reviewed repair passes 185 unit / 56 browser
  cases; final combined application checks remain. Historical attribution is unknown.
- [ ] **EDIT-01 — Account preferences and editor completion.** Establish remaining
  compare-and-swap, isolation and complete-editor obligations before changes.
  **Unverified:** exact open subjobs being reconciled.
- [ ] **CORE-01 — Core/admission repair.** Review the existing isolated repair
  evidence and its exact three-file authority; do not transplant a held RED kernel.
  **Blocked on:** current failure attribution and critical contracts.
- [ ] **ART-01 — Artwork recovery.** Reconcile pre-commit recovery and durable
  metadata/file consistency obligations.
  **Unverified:** current candidate and missing evidence being identified.
- [ ] **SCALE-01 — Large-library performance.** Reconcile remaining F1/F3 indexing
  and scale evidence without reducing correctness or coverage.
  **Unverified:** current implementation/evidence boundaries.
- [x] **ADAPT-01 — Deliver the dormant pure protocol prerequisite.** Ratify C1/C2,
  implement the unwired data-only module and independently test its contracts.
  **Published:** `dbc948f1`; 807 fresh cases and independent bounded gate passed.
  This does not complete ME2-DIALOG/ME2-SESSIONS or authorize editor/authority wiring.
- [ ] **INT-01 — Preserve both branches in main integration.** Resolve the
  isolated integration semantically, including migrations, active library ACLs,
  private history, copy/location identity and both regression baselines.
  **In progress, isolated:** not the branch used for current feature testing.

## Detailed audited programme queue

This inventory is bounded by the current source and recorded evidence, not an
exhaustive audit of every archived candidate. **Implemented** means retain and
qualify it, not rewrite it. **Candidate** means work exists outside primary but has
not been accepted here. Historical failure totals are not current-primary totals.

### Existing foundations with remaining qualification/integration

- [ ] **F4 — Source handoff and progress.** Implemented, including registered
  explicit/scheduled pulls; retain conflict-aware account-local behavior and obtain
  the missing named-server/operational evidence. No remote writeback is implied.
- [ ] **R1 — Typed read API.** Implemented beyond stats; qualify current composition
  and real-client examples rather than create another token/API foundation.
- [ ] **R6 — Repeat sessions, journal and goals.** Implemented; complete portability
  and broader isolation/composition evidence without losing existing history.
- [ ] **X2 — Source replacement/binding repair.** Implemented with bounded local
  evidence; finish specific authority/composition and real-server gates.
- [ ] **R10 — National metadata coverage.** BN has real recorded public samples;
  finish runtime/operational failure evidence, not a duplicate integration.
- [ ] **R9-CONVERGENCE — Complete localization.** Re-audit current strings, dynamic
  errors and JS; historical 186/33 dispositions are not today's remaining total.
- [ ] **ME7 — Remaining presentation preferences.** Preserve qualified whole-item
  density; account persistence/CAS and above-default enlargement remain open.

### Missing or candidate-only product work

- [ ] **F1-INDEX / RF3 — Indexed search and smart shelves.** Helpers/design exist;
  approve central index/rebuild/restore contracts before bounded query integration
  and declared representative scale/RAM/query-plan gates.
- [ ] **F2 / ME5 / X5 — Account-owned presentation preferences.** Pure normalization
  and isolated frontend work exist; storage, actor ownership, CAS and real settings
  integration remain missing. Never import shared-device preferences silently.
- [ ] **RF1 — Theme and card options.** No complete configurable appearance flow
  established; share the preference model rather than add another store.
- [ ] **ME2-DIALOG / ME2-SESSIONS — Complete common editor.** Extracted fields are
  not dialog editing or Previous/Next; protocol, authorized result membership,
  unsaved/stale/unknown outcomes and native fallback need separate completion.
- [ ] **F3-JOBS — Scheduled catalogue refresh.** Isolated job candidates exist;
  compose leases, cancellation, recovery and registered lifespan before selection.
- [ ] **F3-ENUMERATION — Complete large source refreshes.** Primary connectors cap
  enumeration at 5,000. Raising a cap is not complete paging/checkpoint recovery;
  partial runs must never mark unseen records absent.
- [ ] **F5-HIERARCHY — Work/edition/copy and media children.** Existing book/music
  foundations must compose with candidate TV/child models and bounded navigation.
- [ ] **F6-ARTWORK-BATCH — Source/TV/batch artwork.** Preserve current chooser and
  override protections; candidate ingestion needs fencing, cleanup and fresh gates.
- [ ] **RF4 — Ordered mixed-media plans.** Existing list membership is not curated
  ordering; review personal/shared ownership and existing candidate collections.
- [ ] **RF5 — Typed custom fields/templates.** Definition/value/type-change,
  search/API/export and safe data-only template contracts are not established.
- [ ] **RF6 — Private copy attachments.** Existing provenance fields are present;
  attachments need authenticated storage, quotas and merge/recovery/export rules.
- [ ] **RF7 — Private notification inbox/preferences.** Inspect the author-inbox
  candidate before designing a general layer; never send private events globally.
- [ ] **RF8-PROFILES — Import mapping/review workbench.** Freeze versioned mapping,
  previews, stale apply and retry reports on existing import foundations.
- [ ] **RF8-CODECS — Additional interchange formats.** Select actual required
  formats and samples first; RIS/BibTeX/CSL/CBL/NFO are not one promised feature.
- [ ] **RF9 — Spoiler/advisory presentation.** Define scope across previews,
  assistive output, shares and exports; this is not parental access control.
- [ ] **RF10 — Local “Pick for me”.** No complete local-pick flow established;
  approve bounded visible-selection semantics and no-history-mutation behavior.
- [ ] **RF12 — Copy disposal history.** Sale/donation/disposal is not Trash/missing;
  define loans, counts, privacy, undo and non-reacquisition first.
- [ ] **RF13 — Per-copy contents/completeness.** Distinguish edition expectations
  from each copy's present/missing/unknown booklet, disc or manual state.
- [ ] **F5-F6-COMPARE — Edition/pressing comparison.** Stacks and duplicates are
  foundations, not general read-only comparison; unknown facts must stay unknown.
- [ ] **X4-RECOVERY — Cross-surface recovery.** Retain existing Browse restoration;
  finish drafts, delayed-response ownership, reauthentication, focus and retries.

### Critical or operationally blocked compositions

- [ ] **CORE-ADMISSION — Isolated core repair.** Recorded 50 failures belong to the
  held candidate; privacy, native-denial proof dependencies, cascades and auth each
  require bounded contracts. The three-file reservation remains unchanged.
- [ ] **F7 / R3 — Household permissions.** Candidate work is not current shared
  catalogue isolation. Complete the legacy data/mutation/asset/job census first;
  activation remains unauthorized.
- [ ] **R2 — Trusted-header/OIDC identity.** Isolated RED/owner/ingress decisions
  remain; no proposed API or held auth/kernel implementation is selected.
- [ ] **R4 — Inter-household lending requests.** Candidate lifecycle work needs
  actual household authority, revocation and concurrent journey qualification.
- [ ] **R5 — Secret gift claims.** Candidate exists; recipient-independent bytes,
  APIs, exports, history, notifications and concurrent secrecy remain open.
- [ ] **X1 — Complete undo/unmerge.** Primary merge is not complete restoration;
  affected-operation capability and full child/history ownership remain blockers.
- [ ] **X3 / ART-01 — Portable state and artwork recovery.** Held pre-commit crash
  failure remains. TEMP recovery must not sweep final artwork; approve distinct
  ownership/commit/incarnation composition before repair or selection.
- [ ] **F8-RESTORE — Rejected restore changes nothing.** Current source installs
  before migration; isolated integrity/migration preflight and concurrent-worker
  fencing need a centrally reviewed repair on disposable data.
- [ ] **F7-BROWSER / X6 — Account-private offline state.** Candidate fixes exist;
  current global Store keys and cross-account queue/reply composition remain open.
- [ ] **ST1 — Steam catalogue.** Candidate is unregistered and has a recorded
  credential-reflection failure; private association and real Sources integration
  are unfinished. No Steam account access is authorized.
- [ ] **R7 — Audible provider acceptance.** Default-off containment and local ASIN
  entry exist; applicable provider permission is unsubstantiated. Do not bypass it.
- [ ] **R8 — Author discovery.** Candidate/private-inbox work exists; composition,
  provider coverage, scheduling and cross-channel deduplication remain open.

### Feasibility, not supported connectors

- [ ] **RI1 — Plex.** Confirm operator use, named API/auth versions and bounded
  permitted enumeration/handoff before implementation or account access.
- [ ] **RI2 — Navidrome/OpenSubsonic.** Verify server/auth/complete enumeration;
  no credential-bearing URL fallback, scrobbling, scans or downloads.
- [ ] **RI3 — Trakt.** Prefer an adequate user export; freeze identity/timestamp
  coverage before separately authorized API/OAuth work or any remote writes.
- [ ] **RI4 — Playnite.** Require a maintained exporter/version and redacted sample;
  never execute imported actions. Direct GOG account enumeration remains blocked.
- [ ] **F4-RF8-KOREADER — KOReader interchange.** Parked until a stable data-only
  sample and edition/progress contract exist; never execute settings sidecars.

Canonical scope and evidence: [plan](connected-library-plan.md),
[delivery](connected-library-delivery.md), and
[verification](connected-library-verification.md).

## Qualification and external blockers

- [ ] **QUAL-01 — Pinned runtime/container qualification.** Python 3.12, declared
  Playwright/browser and nonpublishing container checks remain **blocked** in this
  workspace. Compatible local results will be labelled separately, not substituted.
- [ ] **OPER-01 — External/provider operational acceptance.** Account-dependent
  evidence remains blocked unless separately authorized and supplied. No production
  data, provider account, household/graph activation or deployment is implied.

## Working and publication rules

Published batch `b4cfd3e4` passed **383 unit / 5 browser** cases, offline checks and
independent **PASS_BOUNDED_BATCH**. All 435 original and 465 batch-base test files
retain their exact blobs/modes; all 185 original acceptance entries remain unchecked.
This is targeted compatible-local evidence, not full-suite/pinned/main readiness.

- Parallel agents receive nonoverlapping file ownership; shared schema, fixtures,
  generated assets and Git selection remain centrally coordinated.
- New tests are additive. Existing regressions are not removed, weakened, skipped
  or marked xfail to obtain green results.
- Freeze application inputs during runtime checks. Fix failures, rerun the affected
  checks, and review the resulting candidate before publishing a code batch.
- Push incremental reviewed batches to **`new-test`**. Do not publish the unresolved
  integration, push `main`, create release tags/images or change Actions settings.
- Only the coordinator updates this checklist and [Progress](PROGRESS.md), so
  parallel agents cannot overwrite one another's completion claims.
