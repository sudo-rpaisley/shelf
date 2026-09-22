# Roadmap

What Shelf is likely to grow next, grouped by theme.

**This is direction, not a schedule.** There are no dates and no order here. Items
move, merge and get dropped as the project learns things — a group appearing on
this page is not a commitment that it ships, and its absence is not a refusal.
For what actually shipped, read the [changelog](../CHANGELOG.md).

Some groups say what they wait on. Where that appears it is a technical fact —
the work genuinely reuses something built earlier — not a queue position.

**Status words:** *Planned* means the shape is settled and it is a matter of
building it. *Exploring* means the idea is accepted but the shape is not.

---

## Choose your features

**Planned.** Shelf has grown a lot of surface, and not everyone wants all of it.
A setup step and a settings page to turn whole feature areas on and off, so an
install that only catalogues books does not carry lending, valuation, store mode
and the rest in its navigation.

Most of the groups below arrive switched off behind this, which is why it comes
early.

**A navigation rework goes with it.** The tab bar has grown one tab at a time
and is now long enough that the next feature makes it worse. The plan is to
group the tabs by what you are actually doing — the views onto your collection
in one place, the scanner-and-camera tools in another — rather than keep adding
to a single row. Turning feature areas off and grouping what is left are two
halves of the same problem, so they are being designed together.

## Homelab integration

**Planned.** Shelf should behave like the rest of your stack.

- **API tokens and a documented JSON API.** Shelf is FastAPI, so a schema exists
  in principle, but every endpoint today wants a browser session cookie — no use
  from `curl`, a script, or a dashboard. A revocable token you paste into another
  tool, and a stable read-mostly surface that will not move under you.
- **Dashboard widgets** — Homepage and Homarr — and a **Home Assistant** recipe.
  Both are thin layers over the token-authed API, so they follow it directly.
- **SSO via OIDC** ([#89](https://github.com/dgahagan/shelf/issues/89)) — sign in
  with the identity provider you already run instead of Shelf keeping its own
  user list. This one builds on the account rework in *Multi-user & households*,
  so it follows that work.

If what you actually need is your reverse proxy handling the login (Authelia,
Authentik and friends), **trusted proxy-header auth** is part of the households
work below and arrives well before full OIDC.

## Multi-user & households

**Planned.** [#48](https://github.com/dgahagan/shelf/issues/48) — the largest
thing on this page, and it will arrive over several releases rather than one.

Today every account shares one library. The goal is that one Shelf install can
host several households — family, friends — each with its own complete library,
and each able to share it with chosen people at chosen access levels. Individual
items and whole locations can be marked private so they never appear to an
outside viewer. On top of that: borrow requests with a lending ledger, wishlists
visible for gift-buying with secret claims, and "who owns this?" search across
the households you can see.

Existing installs upgrade without noticing. Your data becomes the first
household, and nothing visibly changes until you invite someone.

## More sources and languages

**Planned.** Shelf's metadata is good if your books are English and in Open
Library, and thinner otherwise.

- **More national sources.** German ISBNs already route to the Deutsche
  Nationalbibliothek and Italian ones to Italy's national network. Other
  countries deserve the same.
- **A translated interface.** The UI is English-only today. Translating the
  templates and letting the browser or a per-user setting pick the language.

## Import and migration

**Planned.** Goodreads, StoryGraph, CSV and a portable archive are supported
today. Adding **LibraryThing** and **Libib**, and an importer for an
**Audible/Libation library export** so an audiobook collection can be catalogued
without retyping it. Shelf stores the catalogue record, never the audio.

## Collectors and inventory

**Planned.** For collections where the individual copy matters, not just the
title: printable accession labels, a reconciliation report for a shelf audit,
and a duplicate audit across the whole collection. Per-copy condition,
acquisition and provenance fields have shipped — see the item page.

## Reading life

**Planned.** Ratings, did-not-finish and paused states, re-reads, a reading
journal, yearly goals and an Up Next list. This needs per-user state to mean
anything, so it follows the households work.

## Alerts and discovery

**Exploring.**

- **New-release alerts** for the series and authors you follow, plus a calendar
  of what is coming.
- **Price alerts** on wishlist items.
- **Buy links** on wishlist and series-gap rows — off by default, with
  Bookshop.org first because it supports independent bookshops. If you turn them
  on you enter *your own* affiliate tags, not the project's, and the link says
  what it is. Shelf takes nothing from it.

---

## Recently shipped

The last five releases that changed something you can see. Some releases change
only the foundations — 0.42.4 and 0.42.5 rearranged how Shelf reads its own
data, ahead of a Trash you can restore from — and those are left out here
rather than listed as "nothing visible". Full detail on every release, visible
or not, is in the [changelog](../CHANGELOG.md).

| Version | What landed |
|---|---|
| [0.45.0](https://github.com/dgahagan/shelf/releases/tag/v0.45.0) | Browse can tell you where a row came from. A **Source** filter sits in the filter bar beside the others, cross-filtered the same way and listing only the sources your library actually holds — a RomM, Komga or Audiobookshelf sync, a metadata provider, a CSV import, Photo Intake, or by hand. RomM games also get a **RomM ↗** badge on the card that opens the game where it lives. Settings moves its four sections into a sidebar, and a Komga series that arrived split into one series per volume is repaired by the next sync |
| [0.44.0](https://github.com/dgahagan/shelf/releases/tag/v0.44.0) | A novel, its audiobook and the film made from it stay three records, but you can now say they belong together. Link an item to another as a Format, a Related item or an Adaptation, and every item in the group shows the whole group — including the ones it reaches only through a third item. Editors build the links by hand from a panel on the item page; Shelf never guesses from similar titles. The account menu also says who you are, and groups what it holds |
| [0.43.0](https://github.com/dgahagan/shelf/releases/tag/v0.43.0) | Kids books are no longer a media type of their own. Every one becomes an ordinary book carrying a `Kids` tag on the first boot after the upgrade, so a filter for books finally finds all of them, and one that shares an ISBN or barcode with a book you already own is merged into it rather than left beside it. Tags now travel through the CSV export and the portable archive, so the label survives a backup. The conversion is one-way — read the upgrade note first |
| [0.42.3](https://github.com/dgahagan/shelf/releases/tag/v0.42.3) | An older children's paperback is no longer catalogued as a DVD. Scholastic-era books carry a price-point barcode shared across a whole price band, with the title in a separate five-digit block beside it; scanners that read only the first part used to send it down the retail path, where the shared code looks like a disc. Shelf now stops the scan and asks you to type the five digits |
| [0.42.2](https://github.com/dgahagan/shelf/releases/tag/v0.42.2) | An item whose stored ISBN or UPC is not valid — usually an ASIN an older Audiobookshelf sync left behind — can be edited again. A stored identifier you do not touch is left alone and marked on the form, so changing the title or the location no longer means fixing an identifier first |

---

## Suggesting something

Open a [feature request](https://github.com/dgahagan/shelf/issues/new?template=feature_request.yml).
Requests are read and answered, and the ones that fit get folded into a group
above — several already have. Saying what problem you are trying to solve helps
more than proposing a solution, because the fix that lands is often not the one
first suggested.

This page is not a voting board and requests are not ranked by how many people
ask. Shelf is one person's project, built for a real collection.
