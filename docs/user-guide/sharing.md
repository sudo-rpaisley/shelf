# Sharing

Settings → Data → **Sharing** creates public, read-only links — no login
needed by the viewer.

| Link type | Shows |
|---|---|
| **Wishlist** | Shared acquisition-list items: title, author, cover, series. Not each user's personal wishlist |
| **Collection** | Your owned items, same fields. For "what do you have?" |

What a link **never** shows: locations, loans, values, notes, ISBNs, tags,
reading status, or anything about users. It's a cover wall with titles.

A public link is an intentional disclosure to anyone holding the URL, not
authenticated library membership. Private personal wishlist, status, history,
ratings and notes are not the acquisition-share source. Library selection and
anonymous-share scoping still require final [integration verification](../upstream-integration.md);
do not publish a link from this candidate on the assumption that a hidden nav
tab or feature flag provides a privacy boundary.

## Properties

- URLs carry an unguessable 128-bit token (`/share/<token>`).
- Pages are `noindex` so search engines skip them, and rate-limited per IP.
- **Revoke** any link from the same card; the URL dies immediately. Create a
  new one whenever you like.
- Links are live: the page reflects your library at view time, so a
  wishlist link you sent in November is still right in December.
- With Sharing turned off in Settings → **Features**, every link answers
  "Not found" until you turn it back on. The links are kept, and they work
  again as soon as Sharing is on. Meanwhile the **Sharing** card still lists
  them, but cannot create, copy, open or revoke one.

## Outside the LAN

A share link is only useful if the recipient can reach your server. If
Shelf is LAN-only, the usual answers are a reverse proxy with a real
certificate for just this host, or a tunnel (Cloudflare Tunnel, Tailscale
Funnel) — see [HTTPS & reverse proxy](../https-and-reverse-proxy.md). The
self-signed certificate will make a recipient's browser complain, so a real
cert matters more for sharing than for your own use.
