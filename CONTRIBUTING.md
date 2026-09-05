# Contributing to Shelf

Thanks for your interest in improving Shelf.

- **Bug reports are welcome.** Please use the issue templates and include your
  version or commit, browser/device, and any relevant logs (Settings → Logs, or
  `docker compose logs shelf`).
- **Feature requests are welcome**, but this is a personal project and there is
  no delivery commitment or roadmap SLA.
- **Pull requests are considered.** For anything larger than a small fix, open
  an issue first so the approach can be discussed before significant work is
  invested.
- **Docs fixes are always welcome.** The user guide lives in
  [`docs/`](docs/README.md); a typo-only PR does not need an issue first.

Please follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

```bash
git clone https://github.com/sudo-rpaisley/shelf.git
cd shelf
python -m venv .venv && source .venv/bin/activate
make setup                # dev deps, npm (Tailwind), Playwright Chromium
make dev                  # docker compose up -d --build
# or: DATA_DIR=./data-dev uvicorn app.main:app --reload
```

Full details — running, testing, project layout, and project-specific traps —
are in [docs/development.md](docs/development.md) and
[docs/architecture.md](docs/architecture.md).

## Before you submit

```bash
make test        # unit + integration tests
make test-e2e    # Playwright E2E tests (starts its own server)
make checks      # dependency audit, license check, secret scan, CSRF lint, Alpine CSP lint
make css         # if templates/Tailwind changed; commit rebuilt CSS and static/sw.js
```

Notes:

- Unit and E2E tests **cannot** run in a single pytest invocation — use the
  Make targets, not raw `pytest`.
- Any raw `fetch()` call in frontend JS must send the `X-CSRF-Token` header
  (`make check-csrf` enforces this).
- Templates must stay compatible with the Alpine.js CSP build
  (`make check-alpine`).
- E2E tests fail if a page leaves an uncaught browser error behind, even when
  the test's own assertions pass.
- `MIGRATIONS` in `app/database.py` is append-only — never edit or reorder an
  existing entry.
- No CDN references — all JS and CSS is vendored in `static/`.
- `GOTCHAS.md` lists the project's known traps; skim the relevant headings
  before touching migrations, Alpine components, covers or the service worker.

Add a line under `[Unreleased]` in `CHANGELOG.md` for anything user-visible.
The PR template asks which checks you ran; fill it in.

## License

By contributing, you agree that your contributions are licensed under
[AGPL-3.0](LICENSE).
