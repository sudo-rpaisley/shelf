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

Collection now has 8168 units; the earlier **full 8162-test pass belongs to
`38c73f97`**, not this later application. No full-suite or pinned-runtime pass
is inferred from this focused follow-up. Larger implementation candidates remain
separate, unmerged and incomplete.

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
and whitespace checks passed. The full suite has **not** been rerun on that
follow-up or the later source-repair/ASIN candidates.

After composing the full-metadata benchmark and source-digest optimization,
`7e86d0a4` passed **565 focused source-migration, authority, snapshot, availability,
handoff and delivery tests**, exit 0, in 173.64 seconds. That is not a full-unit
rerun or the required representative benchmark on the integrated schema/UI.

Separately, the eight-width source layout/census checkpoint used `cc129371`
with rebuilt CSS/SW outputs matching `29edfaf9`: **9 tests passed**, exit 0,
in 467.43 seconds. The relevant tests and preferred-source template also match
`29edfaf9`. The rebuilt outputs differ from the committed `cc129371` assets;
this is not a clean-commit gate on that SHA alone. All eight widths retain the
**80px** minimum. A separate focused pass captured **88 screenshots** without
geometry failures. These bounded results are not a repeat of the complete
browser programme, a provider-permission grant or live-server acceptance.

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
20-case module run includes it. Collection now contains **7564 unit tests**
and **384 browser tests**. Neither the complete suite nor pinned Python 3.12 CI
was rerun here. The container-side `python -m app.verify_upgrade` command in
the guide allows the operator to check a disposable copy with the real image
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
