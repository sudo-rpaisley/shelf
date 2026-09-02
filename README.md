# Shelf

[![Release](https://img.shields.io/github/v/release/dgahagan/shelf)](https://github.com/dgahagan/shelf/releases)
[![Docker Pulls](https://img.shields.io/docker/pulls/dangahagan/shelf)](https://hub.docker.com/r/dangahagan/shelf)
[![CI](https://github.com/sudo-rpaisley/shelf/actions/workflows/test.yml/badge.svg)](https://github.com/sudo-rpaisley/shelf/actions/workflows/test.yml)
[![Unit tests](https://img.shields.io/badge/unit%20tests-2307%20passing-brightgreen)](https://github.com/sudo-rpaisley/shelf/actions/workflows/test.yml)
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
> Docker image badges above still point to upstream until this fork publishes
> its own releases/images.

## Features

- Barcode/ISBN/UPC scanning with metadata lookup
- Photo Intake for recognising multiple books from a shelf photo
- Books, ebooks, audiobooks, comics, films, music and video games
- Covers, descriptions, authors, series and editions
- Series tracking and completion views
- Reading status and progress
- Lending and borrower tracking
- Locations, tags and bulk actions
- CSV import/export and backup/restore
- Hardcover, Audiobookshelf, Komga and RomM integrations
- Responsive browser UI with PWA support
- Multi-user roles (admin/editor/viewer)

## Documentation

The complete documentation is in [`docs/`](docs/README.md), including installation,
configuration, usage, development and troubleshooting guides.

## Quick start

The simplest supported setup is Docker Compose:

```yaml
services:
  shelf:
    image: dangahagan/shelf:latest
    container_name: shelf
    volumes:
      - ./data:/data
    ports:
      - "443:443"
    restart: unless-stopped
```

Then open `https://<server-address>` and complete first-run setup.

For this fork's source checkout and development workflow, see
[`docs/development.md`](docs/development.md).

## Configuration

Shelf stores runtime settings in SQLite, with secrets encrypted at rest. Environment
variables can override or provide sensitive integration values. See
[`docs/configuration.md`](docs/configuration.md) and [`.env.example`](.env.example).

## Development

```bash
git clone https://github.com/sudo-rpaisley/shelf.git
cd shelf
make setup
make test
make test-e2e
```

The application is FastAPI + SQLite with server-rendered Jinja2/HTMX/Alpine.js and
Tailwind CSS. See [`CLAUDE.md`](CLAUDE.md) and [`docs/architecture.md`](docs/architecture.md)
for project conventions and architecture.

## Security

Please report vulnerabilities according to [`SECURITY.md`](SECURITY.md).

## Licence

Shelf is licensed under the GNU Affero General Public License v3.0. See [`LICENSE`](LICENSE).
