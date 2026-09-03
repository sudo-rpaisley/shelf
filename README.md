# Shelf

[![Release](https://img.shields.io/github/v/release/dgahagan/shelf)](https://github.com/dgahagan/shelf/releases)
[![Docker Pulls](https://img.shields.io/docker/pulls/dangahagan/shelf)](https://hub.docker.com/r/dangahagan/shelf)
[![CI](https://github.com/sudo-rpaisley/shelf/actions/workflows/test.yml/badge.svg)](https://github.com/sudo-rpaisley/shelf/actions/workflows/test.yml)
[![Unit tests](https://img.shields.io/badge/unit%20tests-2387%20passing-brightgreen)](https://github.com/sudo-rpaisley/shelf/actions/workflows/test.yml)
[![E2E tests](https://img.shields.io/badge/e2e%20tests-212%20passing-brightgreen)](https://github.com/sudo-rpaisley/shelf/actions/workflows/test.yml)
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
  titles, then import them in bulk.
- **Automatic metadata and covers** — book metadata can cascade through Open
  Library, Hardcover, Google Books and DNB; games and films can use IGDB and
  TMDb; music releases can use MusicBrainz and the Cover Art Archive.
- **More than books** — catalogue audiobooks, eBooks, DVDs/Blu-rays, vinyl,
  cassettes, CDs, digital music, comics, children's books and video games
  alongside physical books.
- **Collection management** — locations, tags, bulk editing, series tracking,
  reading status, lending, wishlist management and collection statistics.
- **Import, export and backup** — CSV migration, Goodreads/StoryGraph import,
  full archive backup/restore, and OPDS feeds.

## Quick start

### Docker

The published Docker image currently comes from upstream:

```yaml
services:
  shelf:
    image: dangahagan/shelf:latest
    ports:
      - "8000:8000"
    volumes:
      - ./data:/data
    restart: unless-stopped
```

Open `http://localhost:8000` and create the first administrator account.

### Development

```bash
git clone https://github.com/sudo-rpaisley/shelf.git
cd shelf
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Then visit `http://localhost:8000`.

Useful development commands:

```bash
make test          # unit/integration tests
make test-e2e      # Playwright browser tests
make css           # rebuild Tailwind CSS and service-worker version
make badges        # update README test-count badges
```

## Data

Shelf stores application data in `/data` by default. The SQLite database,
covers, generated files and other persistent state all live below that path.
Back up the whole data directory rather than only the database file.

## Licence

Shelf is licensed under the GNU Affero General Public License v3.0. See
[LICENSE](LICENSE) for details.
