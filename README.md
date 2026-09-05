# Shelf

[![Release](https://img.shields.io/github/v/release/dgahagan/shelf)](https://github.com/dgahagan/shelf/releases)
[![Docker Pulls](https://img.shields.io/docker/pulls/dangahagan/shelf)](https://hub.docker.com/r/dangahagan/shelf)
[![CI](https://github.com/sudo-rpaisley/shelf/actions/workflows/test.yml/badge.svg)](https://github.com/sudo-rpaisley/shelf/actions/workflows/test.yml)
[![Unit tests](https://img.shields.io/badge/unit%20tests-2578%20passing-brightgreen)](https://github.com/sudo-rpaisley/shelf/actions/workflows/test.yml)
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

- **Barcode scanning** — scan ISBN/UPC/EAN barcodes with a phone camera or USB scanner.
- **Photo Intake** — photograph whole shelves and use AI vision to prepare many books for import at once.
- **Automatic metadata** — fetch book, film, game, comic, magazine and music details from multiple providers.
- **Physical + digital holdings** — track copies, locations and connected digital services.
- **Series, collections and tags** — organise related media without flattening everything into one list.
- **Lending** — track borrowers, due dates and returns.
- **Reading and media state** — track progress, ratings, favourites and wishlists.
- **Responsive self-hosted UI** — FastAPI + HTMX + Alpine.js with SQLite and a single-container deployment.
- **Offline/PWA support** — install Shelf on a phone and keep the scanner available in bookstore mode.

## Screenshots

<p align="center">
  <img src="screenshots/browse.png" width="48%" alt="Shelf browse page">
  <img src="screenshots/item.png" width="48%" alt="Shelf item detail page">
</p>

## Quick start

### Docker Compose

```yaml
services:
  shelf:
    image: dangahagan/shelf:latest
    container_name: shelf
    environment:
      TZ: Europe/London
      SECRET_KEY: change-me
      SHELF_ENCRYPTION_KEY: change-me-too
    volumes:
      - ./data:/data
    ports:
      - "8000:8000"
    restart: unless-stopped
```

Open `http://localhost:8000` and follow the setup flow.

> The image above is the upstream release image. Development builds from this fork should be built locally from the repository checkout.

## Development

```bash
git clone https://github.com/sudo-rpaisley/shelf.git
cd shelf
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest
```

For frontend assets:

```bash
npm install
npm run build:css
```

Run the development server:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Testing

The project maintains broad unit/integration coverage plus Playwright browser tests. Run the Python suite with:

```bash
pytest
```

Run browser tests with:

```bash
npm install
npx playwright install --with-deps chromium
npm run test:e2e
```

The CI workflow also rebuilds generated CSS and verifies that committed output is current.

## Documentation

- [User guide](docs/user-guide/README.md)
- [Configuration](docs/configuration.md)
- [Architecture](docs/architecture.md)
- [Contributing](CONTRIBUTING.md)

## License

Shelf is licensed under the [GNU Affero General Public License v3.0](LICENSE).
