# Shelf

[![Release](https://img.shields.io/github/v/release/dgahagan/shelf)](https://github.com/dgahagan/shelf/releases)
[![Docker Pulls](https://img.shields.io/docker/pulls/dangahagan/shelf)](https://hub.docker.com/r/dangahagan/shelf)
[![CI](https://github.com/sudo-rpaisley/shelf/actions/workflows/test.yml/badge.svg)](https://github.com/sudo-rpaisley/shelf/actions/workflows/test.yml)
[![Unit tests](https://img.shields.io/badge/unit%20tests-2317%20passing-brightgreen)](https://github.com/sudo-rpaisley/shelf/actions/workflows/test.yml)
[![E2E tests](https://img.shields.io/badge/e2e%20tests-210%20passing-brightgreen)](https://github.com/sudo-rpaisley/shelf/actions/workflows/test.yml)
[![License: AGPL-3.0](https://img.shields.io/github/license/sudo-rpaisley/shelf)](LICENSE)

Shelf is a self-hosted home library catalogue for books and other media. Scan
barcodes, photograph shelves, fetch metadata and cover art, track reading and
lending, organise locations and series, and manage the collection from a
responsive web interface.

<p align="center">
  <img src="screenshots/demo.gif" width="800" alt="Photo Intake demo — a shelf photo is analysed by AI vision and detected books are prepared for import">
</p>

> **Repository note:** this repository is a working fork of
> [`dgahagan/shelf`](https://github.com/dgahagan/shelf). Stable release and
> Docker image links currently refer to the upstream project. To run the code
> in this fork, use the development instructions below.

## Highlights

- **Barcode scanning** — use a phone camera or USB/Bluetooth scanner for ISBN,
  EAN and UPC barcodes.
- **Eight scan modes** — Add, Wishlist, Lend, Return, Move, Inventory, Lookup
  and Quick Rate.
- **Photo Intake** — photograph a shelf or stack of books, review the detected
  titles and ISBNs, then add them in bulk.
- **Metadata and covers** — Open Library, Google Books, national libraries,
  Hardcover, TMDb and IGDB provide metadata, with multiple cover fallbacks.
- **Series tracking** — progress, gaps, completion status and Hardcover-powered
  series completeness checks.
- **Reading history** — TBR / Reading / Read / DNF status with dates and a
  per-item history.
- **Lending** — borrowers, due dates, overdue reminders and loan history.
- **Collection valuation** — ISBNdb estimates, manual overrides and value
  history.
- **Audiobookshelf integration** — import audiobooks and ebooks, including
  metadata, covers and linked physical/digital formats.
- **Komga integration** — import comics and graphic novels as Digital Comics,
  including metadata, covers and browser links back to Komga.
- **RomM integration** — import ROM libraries as Digital Games, including
  platform mapping, covers and browser links back to RomM.
- **Multi-user roles** — Admin, Editor and Viewer permissions.
- **Offline/PWA support** — installable app, cached browse data and a dedicated
  offline bookstore/store mode.

## Quick Start

### Docker Compose

```yaml
services:
  shelf:
    image: dangahagan/shelf:latest
    container_name: shelf
    ports:
      - "18889:18889"
    volumes:
      - ./shelf-data:/data
    restart: unless-stopped
```

Shelf serves HTTPS directly on port `18889`. On first run, open the site and
complete the setup wizard to create the initial Admin account.

For local development from this fork, see [Development](#development).

## Reverse Proxy

If Shelf is behind a reverse proxy, set `SHELF_TRUST_PROXY=1` **only** when the
proxy overwrites `CF-Connecting-IP` / `X-Forwarded-For`. This lets Shelf use the
real client IP for rate limiting without trusting spoofable headers from direct
clients.

The repository includes examples for Traefik and other deployment details in
[`docs/installation.md`](docs/installation.md).

## Development

```bash
git clone https://github.com/sudo-rpaisley/shelf.git
cd shelf
cp .env.example .env
# edit .env as needed
docker compose up --build
```

For tests:

```bash
python -m pytest
```

For the browser suite:

```bash
python -m pytest tests/e2e -m e2e
```

## License

Shelf is licensed under the GNU Affero General Public License v3.0. See
[`LICENSE`](LICENSE).
