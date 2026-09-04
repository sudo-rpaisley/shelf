# Shelf

[![Release](https://img.shields.io/github/v/release/dgahagan/shelf)](https://github.com/dgahagan/shelf/releases)
[![Docker Pulls](https://img.shields.io/docker/pulls/dangahagan/shelf)](https://hub.docker.com/r/dangahagan/shelf)
[![CI](https://github.com/sudo-rpaisley/shelf/actions/workflows/test.yml/badge.svg)](https://github.com/sudo-rpaisley/shelf/actions/workflows/test.yml)
[![Unit tests](https://img.shields.io/badge/unit%20tests-2571%20passing-brightgreen)](https://github.com/sudo-rpaisley/shelf/actions/workflows/test.yml)
[![E2E tests](https://img.shields.io/badge/e2e%20tests-215%20passing-brightgreen)](https://github.com/sudo-rpaisley/shelf/actions/workflows/test.yml)
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
- **Nine scan modes** — Add, Shelf Fill, Wishlist, Lend, Return, Move,
  Inventory, Lookup and Quick Rate.
- **Photo Intake** — photograph a shelf or stack of books, review the detected
  titles, then import them in bulk.
- **Automatic metadata and covers** — book metadata can cascade through Open
  Library, Hardcover, Google Books and DNB; games and films can use IGDB and
  TMDb; music releases can use MusicBrainz and the Cover Art Archive.
- **More than books** — catalogue audiobooks, eBooks, DVDs/Blu-rays, vinyl,
  cassettes, CDs, digital music, comics, Manga, children's books and video games
  alongside physical books.
- **Collection management** — locations, tags, bulk editing, series tracking,
  bulk issue ranges for physical comics and magazines, reading status, lending,
  wishlist management and collection statistics.
- **Import, export and backup** — CSV migration, Goodreads/StoryGraph import,
  portable collection archives and database backup/restore.
- **Offline Store Mode** — a PWA workflow for checking your collection while
  shopping without a network connection.
- **Multi-user** — admin, editor and viewer roles for a shared household
  catalogue.

Shelf itself runs on your network in a single Docker container with SQLite and
does not require a Shelf-hosted account or service. Optional metadata, cover,
sync and vision features contact the providers you configure. Metadata lookups
send identifiers or search terms to those providers; Photo Intake sends the
selected image to a remote vision provider unless you use a local backend such
as Ollama or a local OpenAI-compatible endpoint.

## Screenshots

| Browse | Scan |
|---|---|
| ![Browse](screenshots/browse.png) | ![Scan](screenshots/scan.png) |

| Item detail | Photo Intake |
|---|---|
| ![Detail](screenshots/detail.png) | ![Photo Intake](screenshots/photo-intake.png) |

| Series | Stats |
|---|---|
| ![Series](screenshots/series.png) | ![Stats](screenshots/stats.png) |

## Quick start

### Stable upstream image

For a normal installation, run the published image:

```yaml
services:
  shelf:
    image: dangahagan/shelf:latest
    container_name: shelf
    ports:
      - "18888:18888"
    environment:
      - CERT_SAN=${CERT_SAN:-DNS:shelf,DNS:localhost}
    volumes:
      - ./data:/data:z
    restart: unless-stopped
```

Save that as `compose.yaml`, then run:

```bash
docker compose up -d
```

Open `https://localhost:18888` and create the first admin account with the
setup wizard. Browsers will initially warn about Shelf's self-signed
certificate; see [HTTPS & reverse proxy](docs/https-and-reverse-proxy.md) for
production options.

If you access Shelf by IP address or another hostname, add it to `CERT_SAN` in
an `.env` file beside the Compose file, for example:

```bash
CERT_SAN=IP:192.168.1.100,DNS:shelf,DNS:localhost
```

See [Installation](docs/installation.md) for `docker run`, image tags and the
full setup guide.

### Run this repository from source

The root `docker-compose.yml` is a **development Compose file**. It builds the
local source, uses host networking, stores data in `./data-dev` by default and
listens on port `18889` unless overridden.

```bash
git clone https://github.com/sudo-rpaisley/shelf.git
cd shelf
docker compose up -d --build
```

Open `https://localhost:18889`.

## Persistent data

A normal container installation keeps persistent state under `/data`:

```text
data/
  shelf.db        SQLite database
  covers/         cached cover images
  certs/          generated TLS certificate and key
  encryption.key  credential-encryption key when not supplied by environment
```

Back up the whole data directory, or use Shelf's backup/export tools. Keep
`encryption.key` private: it is needed to decrypt credentials stored in the
database when `SHELF_ENCRYPTION_KEY` is not provided separately.

## Documentation

Full documentation lives in [`docs/`](docs/README.md).

| Area | Documentation |
|---|---|
| Install and operate | [Installation](docs/installation.md) · [Configuration](docs/configuration.md) · [HTTPS & reverse proxy](docs/https-and-reverse-proxy.md) · [Upgrading & backups](docs/upgrading-and-backups.md) |
| Use Shelf | [Getting started](docs/user-guide/getting-started.md) · [Scanning](docs/user-guide/scanning.md) · [Photo Intake](docs/user-guide/photo-intake.md) · [Browse & search](docs/user-guide/browse-and-search.md) · [Items](docs/user-guide/items.md) · [Series](docs/user-guide/series.md) |
| Collection workflows | [Lending](docs/user-guide/lending.md) · [Wishlist & Store Mode](docs/user-guide/wishlist-and-store-mode.md) · [Sharing](docs/user-guide/sharing.md) · [Stats & valuation](docs/user-guide/stats-and-valuation.md) · [Import & export](docs/user-guide/import-and-export.md) |
| Administration | [Integrations](docs/user-guide/integrations.md) · [Users & roles](docs/user-guide/users-and-roles.md) · [Troubleshooting](docs/troubleshooting.md) · [FAQ](docs/faq.md) |
| Development | [Development](docs/development.md) · [Architecture](docs/architecture.md) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) |

## Integrations

Core book cataloguing works without paid API keys. Optional integrations add
richer metadata or additional workflows.

| Service | Purpose |
|---|---|
| Open Library | Book metadata, covers and title search |
| Google Books | Fallback metadata, covers and synopses; optional API key |
| ISSN Portal | Serial publication identification for 977 magazine barcodes |
| Hardcover | Richer book metadata, series information and reading sync |
| DNB | German ISBN metadata |
| IGDB | Video-game metadata, covers and platforms |
| TMDb | Film/DVD/Blu-ray metadata and title search |
| MusicBrainz | Exact music release/pressing metadata, track listings and release-group identity |
| Cover Art Archive | Release-specific music cover artwork |
| ISBNdb | Collection valuation |
| Audiobookshelf | Link and sync selected audiobook libraries |
| Komga | Link and sync selected Comic and Manga libraries |
| Anthropic / OpenAI-compatible / Ollama | Photo Intake vision backends |

See [Integrations](docs/user-guide/integrations.md) for configuration and what
data each integration uses.

## Development

The development guide documents the project layout, local environment, tests,
lints and generated assets:

```bash
python -m venv .venv
source .venv/bin/activate
make setup
make dev
```

Common checks are:

```bash
make test
make test-e2e
make checks
make css
```

See [docs/development.md](docs/development.md) before making changes and
[CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

The test-count badges above are generated from pytest collection. Use
`make badges` after adding or removing tests; CI verifies that the committed
counts are current.

## License

[AGPL-3.0](LICENSE) — free to use, self-host and modify. If you offer a
modified version of Shelf as a network service, you must make the corresponding
source available under the same licence.
