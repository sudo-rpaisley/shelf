# Development

Everything here runs from inside the repository root. `CLAUDE.md` and
`GOTCHAS.md` in the repo hold the deeper, agent-oriented notes; this page is
the human quick start.

## Stack

Python 3.12 · FastAPI · SQLite via raw `sqlite3` (no ORM) · Jinja2 · HTMX ·
Alpine.js (**CSP build**) · Tailwind CSS (built locally, committed) · pytest
+ Playwright. One container, no other services.

## Setup

```bash
git clone https://github.com/sudo-rpaisley/shelf.git && cd shelf
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
make setup            # dev deps, npm (tailwind), Playwright Chromium
```

`requirements.txt` and `requirements-dev.txt` are **generated, hashed locks** —
every package, direct and indirect, pinned with its hashes, so `pip` refuses
anything that does not match. Edit dependencies in `requirements.in` or
`requirements-dev.in`, never in a `.txt`, then run `make lock` to regenerate
both locks; its diff is what a reviewer reads. `make lock-upgrade` takes new
versions of the indirect dependencies on purpose. CI's `make check-lock` fails
a pull request whose `.in` change was not relocked, and it also checks every
`--hash=` line against the package index. It can therefore fail on a pull
request that changed no dependency, when PyPI adds a file to a version that is
already pinned. `make lock` is the repair in every case.

## Run it

```bash
# Docker development environment (uses docker-compose.yml — dev defaults:
# port 18889, ./data-dev, host networking):
make dev              # docker compose up -d --build
make dev-logs
make dev-down

# Or bare uvicorn with a local data dir (plain HTTP on :8000):
DATA_DIR=./data-dev uvicorn app.main:app --reload
```

`SHELF_DISABLE_RATE_LIMIT=1` is handy while iterating on login or `/api/`.

## Tests and lints

| Command | What |
|---|---|
| `make test` | Unit + integration, quiet and parallel (excludes `tests/e2e/`) |
| `make test-fast` | Re-run only the last failures |
| `make test-verbose` | Per-test output |
| `make test-e2e` | Playwright E2E; starts its own server |
| `make test-contract` | Live UPC Item DB contract check — spends one trial lookup; run at release, never on a gate |
| `python -m pytest tests/test_items.py::test_x -v` | One unit test |
| `python -m pytest tests/e2e/test_scan.py -v -m e2e` | One E2E file |
| `make checks-fast` | Offline lints: secrets, CSRF, `items_live`/`copies_live` read seams, Alpine CSP, service-worker version, test conventions, Python lint (ruff: unused imports and variables, redefinitions, syntax errors), README test-count badges |
| `make badges` | Generate README suite counts from collection; commit output in the fork PR that changes tests |
| `make checks` | All checks incl. `check-lock`, `check-lock-selftest`, `pip-audit` and licenses (network) |
| `make lock` | Regenerate the hashed `requirements*.txt` locks from the `.in` files |
| `make lock-upgrade` | The same, taking new versions of indirect dependencies |
| `make check-lock` | Fail if a lock (versions or hashes) is not what `make lock` produces (network) |
| `make check-lock-selftest` | Prove `check-lock` fails on an invented hash (network) |
| `make css` | Rebuild `static/css/app.css` and stamp `SW_VERSION`; commit both in the fork PR after template/JS changes |

Unit and E2E tests **cannot share one pytest invocation** — always use the
targets above. `make verify` enforces a minimum test count, so deleting
tests fails CI.

`make check-ruff` runs ruff with the rule set in `ruff.toml`, and
`python -m ruff check --fix app tests scripts` repairs most findings.

**Fork CI is read-only.** It checks the submitted candidate with unit, lint,
CSS and browser gates; it does not commit, restamp or push back to `main`.
Generated CSS, service-worker versions and badge counts are authored with their
inputs and committed in fork PRs. There is no `generated-output` rule banning
those changes and no upstream Docker publishing workflow in this fork.

Use the workflow/Makefile as the exact job definition, not upstream's historical
five-job/restamp description. A stale generated value or a broken parser is a
failure to repair before handoff, not something CI silently fixes or excuses on
a PR. During integration, only a frozen candidate can receive attributable
build/test results; the documentation is not a claim those gates have passed.

Run `make test`, `make test-e2e` and `make checks-fast` locally before pushing.

CI does **not** run `make test-contract`, and neither does any gate target.
The release gate makes no live third-party call — that is a stated invariant
(`architecture.md`, Testing), and the contract test is the deliberate exception
that lives outside it.

### Service worker versioning

`static/sw.js` precaches the store-mode shell **cache-first**, keyed by a cache
name built from `SW_VERSION`. If a precached file's bytes change and the version
does not, returning browsers keep serving the stale copy — neither
`Cache-Control` nor a hard refresh dislodges it, and unit tests, Playwright and
`curl` all bypass Cache Storage entirely.

So `SW_VERSION` is **generated, not written**: it is `v` plus the first 8 hex
chars of a sha256 over the sorted `PRECACHE` paths and their contents.
`make css` stamps it (`scripts/stamp_sw_version.py`); `make check-sw-version`
and `tests/test_store.py` fail if the committed value is stale or hand-edited.
`static/css/app.css` is precached, so a Tailwind rebuild renames the cache by
itself. Commit the generated stylesheet and stamp together in the fork PR.
After resolving concurrent source changes, regenerate rather than replaying an
old stamp; the digest describes the complete precache, not just your changed file.

One rule survives automation: **never add `sw.js` to its own `PRECACHE`** —
stamping would change the bytes the stamp is derived from and never converge.
`test_stamp_is_idempotent` guards it.

### README test-count badges

README's **unit tests** and **e2e tests** badges quote how many tests each
suite carries. Both are **generated, not written**: `scripts/stamp_test_badges.py`
takes the numbers from `pytest --co` — collection only, so it is offline and
runs in under two seconds — and rewrites the shields.io URLs in place.
`make badges` stamps them, `make check-badges` (inside `make checks-fast`, so
CI runs it) checks that committed numbers match collection. Generate and commit
them in the fork PR; no post-merge bot supplies missing values. During concurrent
integration, retain one recorded parent value until the owner collects the frozen
candidate. Do not invent a combined count or reuse a prior run's result.

The counts come from collection rather than from a run on purpose. Successful
collection does not prove test passes: the count asserts only *"this suite contains N tests"*,
which is a fact about the tree; whether they pass is what the **CI** badge
beside them already says. A badge that re-stated the pass/fail state would be a
second copy of it, free to disagree.

Add a test and forget to regenerate and the badge check reports the mismatch,
just as `check-sw-version` reports a stale stamp. CI does not repair either.

### Responsive geometry

`tests/e2e/test_responsive.py` measures every top-level page at
320/390/430/640/768/1024px and fails on two things a class-string lint cannot
see: a page that scrolls sideways, and a text control squeezed below 80px of
content box. 640, 768 and 1024 are the `sm:`, `md:` and `lg:` breakpoints — a
breakpoint's own width is the worst case for the layout it turns on, since the
wide row has just started rendering and has the least room it will ever have. **Add the
breakpoint width here whenever you introduce a new stacking seam.** A control
that is narrow by design opts out with `data-narrow-ok`, so exemptions stay
greppable.

Tests are isolated: an autouse fixture gives every test its own temp data
dir; use the `client` / `admin_client` / `editor_client` / `viewer_client`
fixtures (CSRF pre-seeded, rate limiting off) and `db` for direct SQL. See
`tests/conftest.py`.

E2E tests fail on a **dirty browser**: every Playwright page is watched for
uncaught errors, and a test that leaves one behind fails at teardown even when
its own assertions passed. The failure quotes Alpine's expression text, which
usually names the culprit outright — and, when there is something to say, the
failed requests, non-2xx `.js` responses and component-registration state that
explain *why* the page broke. On a healthy page that block is absent and the
message is unchanged.

There is still no general opt-out, but there is now **one sanctioned
suppression contract**, and `tests/e2e/test_component_load_guard.py` is both
its definition and its only user. It applies to a test whose *subject is the
error* — one that deliberately breaks a script load to prove the app reports
it. The sequence is fixed: build the page inline with
`attach_page_guard(ctx.new_page())`, assert the expected error signature
yourself, clear the recorder lists on the Page, then call `assert_page_clean`
before the context closes. Clearing is not evasion — the errors are what the
test just asserted, and the trailing check still proves nothing *unexpected*
rode along. Any other test that wants to provoke an error needs its own
contract designed first; do not copy this one to silence an inconvenient
failure.

**Never wait with `wait_for_load_state("networkidle")` after a click.** It does
not wait for what you mean: Playwright resolves it immediately when the page has
already reached the state, and right after a click it usually has, because the
request the click starts may not have been issued yet. So the wait returns at
once and the assertion races whatever the click began. Arm the waiter **before**
the click, and pick it by **what the following assertion reads** — not by what
the click fires:

| the assertion reads | the waiter |
|---|---|
| nothing in flight (the click makes no request) | delete the wait; `expect(...)` auto-retries |
| the new document | `with page.expect_navigation(): click()` |
| the response, or the DB behind it | `with page.expect_response(<predicate>): click()` |
| a swapped HTMX fragment | `with page.expect_response(lambda r: "/api/search" in r.url): click()` |

`make check-tests` enforces this with an allowance of zero. `wait_for_load_state`
after a `goto()` is the documented use and stays legal — only the adjacency to a
click or a press is not. **G83** in `GOTCHAS.md` carries the reasoning, including
why `wait_for_url` is not the fix for a handler that redirects back to the URL
it posted from.

## Rules that bite

- **Strict CSP.** No inline `<script>`, no `eval`, no CDNs. All JS/CSS lives
  vendored in `static/`.
- **Alpine CSP build.** Expressions must be simple; nested or bracketed
  `x-model` bindings silently drop input. Guard a *chain* with a ternary, never
  `&&` — the CSP build evaluates both operands before applying the operator, so
  `x && x.prop.length` throws when `x` is `false` or `null` (`x ? x.prop.length
  : ''` is safe, and optional chaining doesn't parse at all). `make
  check-alpine` enforces both, though it only sees the statically obvious guard
  shapes — a plain identifier dereferenced two levels deep or called as a
  method.
- **The script load order in `<head>` is load-bearing**, and `make
  check-alpine` enforces it too. A script under `static/js/` that calls
  `Alpine.data()` is a **classic** script — never `defer`, never `async`;
  `component-load-guard.js` comes **first**, ahead of every registering script;
  and Alpine's own tag is the only deferred one and comes **last**. Disturb any
  of the three and the affected page throws `Undefined variable` once per
  binding, with the guard installed too late to say which file was lost.
  A new `Alpine.data('name', …)` also needs a `name: 'file.js'` entry in the
  guard's `SCRIPTS` map, or losing that file goes unreported — the same lint
  fails until it is there.
- **Raw `fetch()` must send `X-CSRF-Token`.** `make check-csrf` enforces;
  HTMX is configured globally in `base.html`.
- **`MIGRATIONS` in `app/database.py` is append-only.** Never edit or
  reorder an existing entry; migrations must be replay-safe.
- **Browse filters are declared once**, in `app/browse_filters.py`. Add a
  `BrowseFilter(...)` there rather than editing `hx-include` lists, SQL
  conditions or `browse.js` by hand — they all derive from it.
- **Items are created through one function**, `insert_item()` in
  `app/services/item_write.py`. Call it inside an existing `with get_db()`
  block. Adding a column to `items` no longer means auditing a dozen insert
  sites.
- **Items and copies are each read through a view** — `items_live` and
  `copies_live`, both created per connection by `get_db()`. `items_live`
  filters `deleted_at IS NULL`; `copies_live` joins the items relation, so a
  copy is live only if it and its item are untrashed. Write `FROM items_live` /
  `JOIN copies_live`, never the bare tables; `make check-deleted` enforces both
  and matches `JOIN` as well as `FROM`, because several files reach a table
  through a join alone. Writes stay on the physical tables, as do the reads
  that exist to predict a UNIQUE violation and the few that must *find* a row
  the view hides — each allowlist entry states which (`GOTCHAS.md` G107).
- **Authenticated catalogue reads are library-scoped**, with actor-owned status
  overlays. Per-item/copy writes check library authority inside the write lock;
  API tokens stay read-only regardless of their owner's role. Central schema
  helpers never reseed private state or grant access. See
  [Integration](upstream-integration.md) for lineage and identity constraints.
- **A route that decides on a `SELECT` guards and writes in one
  transaction.** `db.execute("BEGIN IMMEDIATE")` goes first in the block,
  above the guard query — a bare `SELECT` opens no transaction, so guarding in
  one block and inserting in another takes the write lock only at the INSERT
  and a rival can commit in the window (`GOTCHAS.md` G18). Nothing that opens
  a second connection or writes a log record may run inside that block; carry
  the outcome out (`existing`, `item_id`, `value_error`) and act on it after
  the block closes (G3). A pre-check placed *before* an outbound lookup is
  allowed to read unlocked, because it only saves a paced request — it must
  decide nothing.
- **Item routes live in four modules** — `items.py` (scan, CRUD, search,
  bulk ops), `items_covers.py`, `items_csv.py` and `items_catalog.py` —
  sharing helpers from `items_common.py`. Import that as a *module* and call
  through it; a from-import binds a copy that tests cannot patch. Settings is
  likewise one template per tab under `templates/fragments/settings/`.
- **`from app.config import X` freezes the value at import time.** Read
  `app.config.X` at call time instead; tests override config.
- **Tailwind output is committed in fork PRs, together with the SW stamp.**
  The committed stylesheet is what the image serves; stale generated output is
  a real defect, not permission for CI to write the branch.
  The build is Tailwind CSS 4's `@tailwindcss/cli`, pinned exactly in
  `package.json`; the theme and the scanned paths live in
  `static/css/input.css` (`@theme`, `@source`) rather than in a JavaScript
  config file. The output uses cascade layers, `color-mix()` and
  `@property`, so it needs **Safari 16.4, Chrome 111 or Firefox 128**. Older
  browsers lose colours with an opacity modifier and the `@property`-backed
  utilities — store mode on an old phone is the case to watch.
- Before touching migrations, Alpine components, covers, the service worker
  or outbound rate limiting, read the matching entry in `GOTCHAS.md`.

## Project layout

```
app/
  main.py          FastAPI app, middleware (CSP, rate limit, auth, CSRF), lifespan tasks
  nav.py           nav tab registry (auto-hide for unconfigured integrations)
  browse_filters.py  the Browse filter set — SQL, querystring and UI, declared once
  config.py        paths, media types, platforms, vision caps, per-host rate limits
  database.py      SCHEMA + append-only MIGRATIONS
  auth.py, crypto.py
  routers/         one per feature: items (scan/CRUD/search), intake, store, series,
                   share, tags, valuation, sync (ABS), hardcover, checkouts, locations,
                   platforms, archive, settings, auth_routes, pages
  services/        external clients + domain logic: openlibrary, hardcover, googlebooks,
                   dnb/sbn/national + bib_normalize, igdb, tmdb, isbndb, upc, covers +
                   cover_queue, vision + tiling, audiobookshelf, archive,
                   reading_imports, notify, outbound
  templates/       Jinja2 pages + fragments/ for HTMX swaps
static/            vendored JS/CSS, Alpine components, service worker, Tailwind output
tests/             unit/integration; tests/e2e/ Playwright
scripts/           lint scripts (CSRF, Alpine CSP, test conventions), intake eval
Makefile, Dockerfile, entrypoint.sh, docker-compose.yml (dev defaults)
```

See [Architecture](architecture.md) for how the pieces fit.

## Submitting changes

Read [CONTRIBUTING.md](../CONTRIBUTING.md). Short form: open an issue first
for anything non-trivial, run `make test`, `make test-e2e` and `make checks`,
and fill in the PR template with exact results and limitations. Generate and
commit `app.css`, `SW_VERSION` and collected README counts with their source
inputs. Never claim an unrun gate passed.

## Releases

This fork does not currently publish its own Docker image. Stable release
artifacts are still provided by the upstream `dgahagan/shelf` project. Before
creating fork-specific version tags, configure a release workflow and image
namespace owned by this fork.
