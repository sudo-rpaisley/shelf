# Contributing to Shelf

Thanks for your interest in the `sudo-rpaisley/shelf` working fork of
`dgahagan/shelf`. Use this fork's [issue tracker](https://github.com/sudo-rpaisley/shelf/issues/new/choose)
and [support/security channels](SUPPORT.md). Upstream release and PR practices
are not promises made by the fork.

- **Bug reports are very welcome.** Please use the issue templates and include
  your version, browser, and any relevant logs (Settings → Logs, or
  `docker compose logs shelf`).
- **Feature requests are welcome** — no promises. The
  [roadmap](docs/roadmap.md) follows what my own library needs first, but user
  reports regularly shape it, and requests that fit get folded into a group
  there. It carries no dates and no ordering on purpose.
- **Pull requests are considered** on a best-effort basis, without a release
  schedule or guaranteed turnaround. For anything bigger than a small fix, open an issue
  first so we can talk about the approach before you invest time.
- **Docs fixes are always welcome** — the user guide lives in
  [`docs/`](docs/README.md); a typo PR needs no issue.

Please follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

```bash
git clone https://github.com/sudo-rpaisley/shelf.git
cd shelf
pip install -r requirements.txt
make setup                # dev deps, npm (Tailwind), Playwright Chromium
make dev                  # docker compose up -d --build
# or: DATA_DIR=./data-dev uvicorn app.main:app --reload
```

To change a dependency, edit `requirements.in` (or `requirements-dev.in`), run
`make lock`, and commit the `.in` file together with both regenerated
`requirements*.txt` locks. Never edit a `requirements*.txt` by hand.

Full details — running, testing, project layout, the rules that bite — are in
[docs/development.md](docs/development.md) and
[docs/architecture.md](docs/architecture.md).

## Preparing a focused PR

Upstream changes and fork features evolve independently. Read the
[integration contract](docs/upstream-integration.md), compare both intents when
resolving a conflict, and preserve unrelated work. Generated files travel in
fork PRs; regenerate them after source reconciliation rather than choosing an
old minified line or stamp.

What actually helps:

- **Branch from current fork `main`.** Coordinate any history changes with the
  maintainer; do not rewrite a shared integration branch.
- **One concern per PR.** A focused change is easier to review. A
  roll-up of seven unrelated fixes cannot be reviewed as a unit, and if any one
  piece is wrong the whole thing waits — and by then it has conflicted.
- **Check it is not already fixed.** `main` moves faster than the release notes
  suggest. `git log --oneline -20` and a quick search for the symptom costs a
  minute and has saved whole PRs from being written against bugs fixed weeks
  earlier.
- **Claim an issue before you build.** Comment on it, or open one. I do a lot of
  work in private branches, and the only way you can know something is already
  in flight is if I tell you — so ask, and I will.
- **If the behaviour looks wrong, ask before changing it.** Some of what looks
  like a bug is deliberate. For example, this fork rejects invalid Store codes
  without inserting a placeholder, retains one real Browse search input, and
  refuses removal of the last owned physical copy. Historical upstream behavior
  is not permission to reverse these explicit fork policies.

None of this applies to typo and docs fixes. Send those straight in.

## Before you submit

```bash
make test        # unit + integration tests
make test-e2e    # Playwright E2E tests (starts its own server)
make checks      # dependency audit, license check, secret scan, CSRF lint, Alpine CSP lint, Python lint (ruff)
make test-contract   # live UPC Item DB check (one lookup/run) — not needed for a PR
make css         # templates/JS/Tailwind changes — commit CSS + SW_VERSION in the fork PR
make badges      # test-count changes — commit generated counts, not invented pass claims
```

Notes:

- Unit and E2E tests **cannot** run in a single pytest invocation — use the
  Make targets, not raw `pytest`.
- Any raw `fetch()` call in frontend JS must send the `X-CSRF-Token` header
  (`make check-csrf` enforces this).
- Reads of `items` go through the `items_live` view and reads of `item_copies`
  through `copies_live`, never the physical tables (`make check-deleted`
  enforces both, matching `JOIN` as well as `FROM`). Writes stay on the physical
  tables, and so do the few reads that exist to predict a UNIQUE violation —
  a trashed row still holds its unique slot (`GOTCHAS.md` G107).
- Templates must stay compatible with the Alpine.js CSP build
  (`make check-alpine`) — in particular, guard a chain with a ternary
  (`x ? x.prop.length : ''`), never `&&`, which the CSP build evaluates
  eagerly and which therefore throws instead of guarding.
- E2E tests fail if a page leaves an uncaught browser error behind, even when
  the test's own assertions pass.
- `MIGRATIONS` in `app/database.py` is append-only — never edit or reorder an
  existing entry.
- No CDN references — all JS and CSS is vendored in `static/`.
- **Some files are generated — never hand-edit them.** `static/css/app.css` and
  the `SW_VERSION` constant in `static/sw.js` come from `make css`; the
  test-count badges in `README.md` come from `make badges`. Run the target and
  commit the result. If you rebase, regenerate these rather than replaying your
  old hunk — the service-worker stamp is a digest over the whole precache set,
  so a replayed stamp fails `make check-sw-version`.
- **Fork CI is read-only.** It checks authored output; no auto-restamp,
  bot commit or push-back repairs the branch after merge. The fork does not
  publish upstream Docker images or enable upstream's publishing workflow.
- Preserve published migration identities/provenance and one-time private-state
  seeds. Unsupported histories must refuse before mutation; do not erase migration
  history or use an upstream version number as fork-lineage proof.
- Catalogue writes require the target library role under lock, not merely a
  global role. Keep private state actor-owned, shared acquisition lists separate,
  and bearer capabilities read-only. Copy node IDs are not item flat-location IDs.
- UI text is translated with gettext (`docs/architecture.md`, Frontend). Mark
  new text with `{{ _('...') }}` in templates, `i18n.gettext(...)` in Python and
  `t('...')` in JS, then run `make i18n` and commit the updated `.po` and `.mo` files
  (`make check-i18n` fails otherwise). Leaving the untranslated `msgstr` empty
  is fine; the English shows until someone translates it. To add a language, add
  its code to `SUPPORTED` and `LANGUAGE_NAMES` in `app/i18n.py` and to `LANGUAGES`
  in `scripts/i18n.py`, then run `make i18n`. A language with variants takes its
  script or region in gettext's form (`zh_Hans`, `pt_BR`); a region that stands
  for a script goes in `ALIASES`.
- Phrase a translation so it doesn't depend on the reader's gender: Shelf doesn't
  ask for it, and the setup page runs before any user exists. Rewrite the
  sentence rather than pick a form ("Ti diamo il benvenuto", not "Benvenuto").
- `GOTCHAS.md` lists the project's known traps; skim the headings before
  touching migrations, Alpine components, covers or the service worker.

Add a line under `[Unreleased]` in `CHANGELOG.md` for anything user-visible.
The PR template asks which checks you ran; fill it in.

## License

By contributing, you agree that your contributions are licensed under
[AGPL-3.0](LICENSE).
