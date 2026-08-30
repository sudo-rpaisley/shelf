# Security Policy

## Reporting a Vulnerability

Please **do not** open a public issue for security problems. Use GitHub private
vulnerability reporting from the repository's Security tab when it is enabled;
otherwise contact the maintainer privately using the email address on their
GitHub profile.

This is a personal project with no formal SLA, but security reports should be
handled before routine feature work.

## Supported Versions

Security fixes for this fork target the current `main` branch. Until this fork
publishes its own releases, stable release packages and images remain those of
the upstream project.

## Security Posture

Shelf is designed to run on a private home network, but it is hardened as if
it were exposed more broadly:

- Strict Content-Security-Policy — no `unsafe-inline`, no `unsafe-eval`, no
  CDNs (all assets vendored)
- CSRF protection on all mutating requests
- bcrypt password hashing; JWT sessions in HTTP-only, secure cookies
- Role-based access control (admin / editor / viewer)
- Third-party API credentials encrypted at rest (key kept outside the DB, so
  database backups contain ciphertext only) and write-only in the UI
- Credential values redacted from request logs, so container logs can be shared
  when reporting a bug
- Optional passphrase-encrypted (AES) backup downloads
- HTTPS by default (self-signed certs generated on first run)
- Container runs as a non-root user

If you're exposing Shelf beyond your LAN, put it behind a reverse proxy with
a real certificate and set `SHELF_TRUST_PROXY=1` only when that proxy
correctly overwrites the trusted forwarding headers.
