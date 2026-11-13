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

| Lane | Backlog responsibility | Initial state |
|---|---|---|
| Search | F1 indexing, ranking, bounded grouping/paging and scale measurements | Assigned |
| Front door | F2 shelves, creators, collections, preferences and navigation | Assigned |
| Source jobs | F3 scheduling, progress, retry/recovery and large-source boundaries | Assigned |
| Handoff/progress | F4 preferred source and explicitly linked account progress | Assigned |
| Media hierarchy | F5 works/editions/copies, music and source children | Assigned |
| Artwork/review | F6 source artwork, TV artwork and bounded reviewed batches | Assigned |
| Household access | F7/R3 ownership, invitations, visibility and legacy enforcement | Assigned |
| Recovery | F8 restore preflight, integrity and synthetic recovery evidence | Assigned |
| Read API | R1 documented, typed, paginated read contracts | Assigned |
| Identity | R2 trusted proxy login and OIDC with local recovery | Assigned |
| Borrowing | R4 authorised owner search, requests and lending ledger | Assigned |
| Gifts | R5 recipient-safe concurrent gift claims | Assigned |
| Reading life | R6 repeat sessions, journal and annual goals | Assigned |
| Audiobook enrichment | R7 exact-ASIN marketplace-aware review/backfill | Assigned |
| Author discovery | R8 account-local following and deduplicated releases | Assigned |
| Translation | R9 existing surfaces, followed by an integrated new-string pass | Assigned |
| National providers | R10 additional documented, permitted provider coverage | Assigned |
| History/undo | General change history, supported rollback and merge snapshots | Assigned |
| Source repair | Reviewed replacement/rebinding and non-destructive retirement | Assigned |
| Portability | Versioned music/account-data export/import and round trips | Assigned |
| Offline reference | Explicit bounded, account-isolated offline reference browsing | Assigned |
| Browser privacy | Account/library-scoped Store queues and navigation storage | Assigned |

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
