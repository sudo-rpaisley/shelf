# Roadmap

What Shelf is likely to grow next, grouped by theme.

This page combines upstream direction with fork capabilities. Upstream version
links describe upstream releases, not published fork packages. For current
compatibility and unverified integration work, see [Integration](upstream-integration.md).

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

**Available in source; integration validation pending.** Shelf has grown a lot of surface, and not everyone wants all of it.
A setup step and a settings page to turn whole feature areas on and off, so an
install that only catalogues books does not carry lending, valuation, store mode
and the rest in its navigation. The settings page shipped in 0.55.0 as
Settings → **Features**, and the setup wizard now asks for a profile —
Minimal, Standard or Everything — which Features can change later.

Most of the groups below arrive switched off behind this, which is why it comes
early.

**Grouped navigation goes with it.** The fork groups collection views, intake
tools and account actions rather than adding everything to one row. Accessibility
and responsive behavior remain required as more features arrive.

## Homelab integration

**Planned.** Shelf should behave like the rest of your stack. The first part shipped in 0.74.0.

- **API tokens and a dashboard document — shipped in 0.74.0.** Read-only
  tokens you make from your account menu, `GET /api/v1/stats`, and tested
  recipes for Homepage and Home Assistant. See
  [Homelab integration](homelab-integration.md).
- **The rest of the read API** — items, loans, wishlist, series and upcoming
  releases under the same tokens, with a published schema. A stable
  read-mostly surface that will not move under you.
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

The fork already has first-class libraries, per-user membership and private
activity; this integration must preserve those boundaries across modern upstream
routes. Collections and physical locations are not permission containers.
Broader household invitations, secret gift claims and cross-household lending
remain separate design work, not implied by library membership support.

Only recognized fork/common-prefix databases and fresh installs are admitted.
No transparent upstream-database conversion is promised.

## More sources and languages

**Planned.** Shelf's metadata is good if your books are English and in Open
Library, and thinner otherwise.

- **More national sources.** German ISBNs already route to the Deutsche
  Nationalbibliothek, French ones to the BnF, Italian ones to Italy's national
  network, Dutch ones to
  the Koninklijke Bibliotheek, Hungarian ones to OSZK NEKTÁR and Portuguese
  ones to PORBASE, and Chinese-language ones to NeoDB. Other
  countries deserve the same.
- **A translated interface.** In progress. The translation machinery and the
  first catalog have landed, and each user can pick a language or follow the
  browser. Other languages come next. The page shell (navigation, sign-in,
  setup, the Account dialog), Browse, the item page, item edit, manual add, Scan,
  Photo Intake, Shelf Fill and Store Mode are translated; the other feature pages
  follow area by area. Until then it sits behind `SHELF_I18N`, off by default.

## Import and migration

LibraryThing/Libib (beta) and Audible export import join CSV, Goodreads,
StoryGraph and portable archives. Remaining integration work includes library
scoping, actor-owned history and precise copy-location round-trips. This does not
mean unsupported database histories can be converted by editing migration rows.

## Collectors and inventory

Copy metadata, Shelf Fill, ordering, labels, audits and duplicate tools are
present in source. Preserve node IDs and the last-owned-copy refusal while
integrating reversible Trash. MusicBrainz release identity, periodical issues,
provider series and the fork's Collections must coexist rather than replace one
another.

## Reading life

The fork retains per-user ratings, history, private notes, paused/dropped state,
My List and Continue / Up Next. Broader journaling and goals can build on that
private state; they are not a reason to reintroduce global reading activity.

## Alerts and discovery

**Exploring.**

- **New-release alerts for authors** you follow, beyond the series you watch
  (series watching and the Upcoming releases page shipped in 0.73.0).
- Price alerts and buy links for the shared acquisition list are present
  upstream features; they do not publish a user's personal wishlist or notes.

---

## Recently shipped

The last five **upstream** releases that changed something you can see. Some releases change
only security or foundations — 0.50.1 and 0.51.0 hardened sign-in and fixed a
security issue without changing what you see — and those are left out here
rather than listed as "nothing visible". Full detail on every release, visible
or not, is in the [changelog](../CHANGELOG.md).

| Version | What landed |
|---|---|
| [0.81.0](https://github.com/dgahagan/shelf/releases/tag/v0.81.0) | **Shelf on a Raspberry Pi.** The Docker image is published for `arm64` as well as `amd64`, so an ARM64 machine such as a Raspberry Pi pulls it directly instead of building it. Contributed by @Chrisser73. |
| [0.80.0](https://github.com/dgahagan/shelf/releases/tag/v0.80.0) | **Audiobook covers and details from Audible.** An audiobook with an ASIN, such as one from a Libation or audible-cli import, gets its cover from the Audible catalogue, and **Fill details from Audible** fills its empty fields. Choose your Audible marketplace in Settings. Also: Photo Intake, Shelf Fill and Store Mode in Italian, contributed by @vjandrea. |
| [0.79.1](https://github.com/dgahagan/shelf/releases/tag/v0.79.1) | **"Also available as" follows Related Media.** Linking or unlinking another format on an item updates its "Also available as" links straight away, without a reload. Contributed by @sudo-rpaisley. |
| [0.79.0](https://github.com/dgahagan/shelf/releases/tag/v0.79.0) | **The Scan page in Italian.** The mode tabs, the form, and every card and message a scan returns are translated, and the labels follow the mode and the media type. Contributed by @vjandrea. |
| [0.78.3](https://github.com/dgahagan/shelf/releases/tag/v0.78.3) | **Keyboard-shortcut help in every language.** In Italian or Chinese, the **?** button now opens the shortcut list, and its close button and backdrop close it. The list opens centred and fits a phone screen. Shortcuts also stop firing while Ctrl, Cmd or Alt is held, so Ctrl+F and the browser's other shortcuts work again. |
---

## Suggesting something

Open a [fork feature request](https://github.com/sudo-rpaisley/shelf/issues/new/choose).
Requests are read and answered, and the ones that fit get folded into a group
above — several already have. Saying what problem you are trying to solve helps
more than proposing a solution, because the fix that lands is often not the one
first suggested.

This page is not a voting board and requests are not ranked by how many people
ask. Shelf is one person's project, built for a real collection.
