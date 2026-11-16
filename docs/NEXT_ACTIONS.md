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

- [ ] **CAS-01 — Find usable metadata.** Verify public cassette candidates using
  artist and title; record source links and unresolved pressing differences.
  **In progress:** metadata research agent.
- [ ] **CAS-02 — Make the entry path usable.** Reach title/artist search without a
  barcode, select cassette format, and show useful candidate choices.
  **In progress:** workflow/design agent; implementation follows bounded ownership.
- [ ] **CAS-03 — Save the chosen release correctly.** Preserve cassette format,
  artist/title, available tracks and source identity; do not invent an exact
  pressing or silently replace user-entered data.
  **Pending:** workflow findings and metadata evidence.
- [ ] **CAS-04 — Keep a useful fallback.** No match or provider failure must leave
  a usable manual-add path without requiring a barcode.
  **Pending:** workflow findings.
- [ ] **CAS-05 — Prove the complete journey.** Add deterministic offline unit and
  browser regressions for the example, competing formats, ambiguity and failures;
  retain all existing assertions and strict CSP/CSRF protections.
  **Pending:** implementation and frozen-candidate checks.
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
  **In progress:** source audit; no speculative fixes selected yet.
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
- [ ] **ADAPT-01 — Dormant adapter prerequisites.** Reconcile ME2 C1/C2 contracts
  before granting a writer or enabling any dormant authority.
  **Blocked on:** prerequisite review; activation is not authorized.
- [ ] **INT-01 — Preserve both branches in main integration.** Resolve the
  isolated integration semantically, including migrations, active library ACLs,
  private history, copy/location identity and both regression baselines.
  **In progress, isolated:** not the branch used for current feature testing.

## Qualification and external blockers

- [ ] **QUAL-01 — Pinned runtime/container qualification.** Python 3.12, declared
  Playwright/browser and nonpublishing container checks remain **blocked** in this
  workspace. Compatible local results will be labelled separately, not substituted.
- [ ] **OPER-01 — External/provider operational acceptance.** Account-dependent
  evidence remains blocked unless separately authorized and supplied. No production
  data, provider account, household/graph activation or deployment is implied.

## Working and publication rules

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
