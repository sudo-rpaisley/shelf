# Integrations

Shelf works fully with no accounts anywhere. Each integration below adds
something specific. Existing provider cards live under Settings → Integrations,
with inline setup guides; credentials are encrypted and shown write-only.

## Connected sources (experimental)

On the experimental `new-test` branch, **Sources** (`/sources`, account menu →
Library) adds multi-instance OPDS, Jellyfin and manual **External library
links**. Admins configure and sync these there, not in the legacy integration
cards. Enable **Connected library** in Settings → Features first. See the
[Connected library guide](connected-library.md#connect-sources) for exact
options, supported Jellyfin media types, OPDS traversal/open-link limits and
manual setup.

These are read-only catalogue/availability connections, not playback or progress
sync. New imports are neither owned nor wishlisted and create no physical
copies. **Every imported catalogue entry is visible to every Shelf user**;
the source account's access scope does not provide Shelf privacy isolation.
Availability is cached, not a live reachability test. New sources sync manually;
there is no new scheduler.

Audiobookshelf, Komga and RomM remain their existing **singleton** integrations,
with their original settings, IDs and schedules (Audiobookshelf only). Their
holdings participate in **Available here** without being converted into new
writable source instances. Sources also shows their last-attempt/last-complete
health and cached-record counts. Hardcover's existing shared status sync is
unchanged; the new account-local My Activity does not send anything to it.

Back up before experimenting. A full database backup retains connection and
personal tables and cover-sync decisions, but the portable archive intentionally
excludes them and does not make personal/source state portable. See
[Backup and portability](connected-library.md#backup-and-portability).

### Refreshes and local edits

The experiment's Audiobookshelf, Komga, RomM and new OPDS/Jellyfin refresh paths
use per-source, per-field three-way provenance. Nonempty legacy fields without
a known applied baseline are preserved; an observation equal to a local value
does not claim it.
Genuinely missing fields can be filled, and source-applied fields may follow
corrections until manually overridden. Known local edits, including explicit
blanks, survive repeated syncs. Komga's adopted manual rows are fill-only unless
an editor explicitly follows a field in its adopted-item refresh set.
Covers are filled when missing rather than replaced by metadata refresh.
Removing one pauses Audiobookshelf/Komga/RomM source artwork independently of
queue dismissal; **Allow missing-cover sync again** explicitly re-enables it.
See [Removed covers](connected-library.md#removed-covers-and-source-artwork)
for the separate book-cover queue/retry boundary.

New sources project validated descriptive metadata and retain bounded provider
IDs for supported Library searches, without refreshing local identity fields.
Editors can choose **Keep local** / **Follow source** under an item's
**Metadata sources**. Merges retain observations without granting a losing
provider authority over kept manual fields. See
[Metadata refresh policy](connected-library.md#local-metadata-edits-survive-provider-refresh).

Changing a new syncable source's roots, scope/limits or credentials resets
health and cached availability to Unknown; cosmetic/no-op changes preserve
them. Use a new instance for an unrelated server. Disabling retains data and
removes open actions; re-enabling alone is not a live check. Manual sources
offer **Remove link only** without deleting the catalogue item. See
[Configuration changes](connected-library.md#change-configuration-disable-or-re-enable).

## Hardcover

[hardcover.app](https://hardcover.app) — free, community-run Goodreads
alternative with an excellent data model.

**Adds:** bidirectional reading-status sync, richer metadata and synopses on
lookup, series completeness checks and one-click "add missing volumes to
wishlist", import of your Hardcover library, export of your Shelf library to
Hardcover, the **Discover** tab (recommendations), and the **release radar**
— watch a series and get a daily digest when a new volume is announced (see
[Series](series.md#watching-for-new-volumes)).

**Setup:** Hardcover → Settings → API → copy the token → paste into the
Hardcover card. Choose a sync schedule for reading status; run **Import
library** once if you have history there.

An ISBN that arrives from Hardcover — or from an Open Library title search —
and fails its check digit is dropped rather than stored, with a line in the
server log naming it. The item is still added; it just arrives without an
ISBN, and you can type the right one into **Edit**.

## Audiobookshelf

[audiobookshelf.org](https://www.audiobookshelf.org) — self-hosted
audiobook/podcast server.

**Adds:** sync of selected ABS libraries into Shelf as audiobook / eBook
items, each with its ASIN, cross-linking with physical copies (the item page shows both and
deep-links into ABS), periodic re-sync.

**Setup:** in ABS, Settings → Users → your user → API Token. Enter the ABS
URL and token, **Test**, then choose which libraries to include. Set an
interval for automatic sync or run it by hand. Items removed from ABS can be
cleaned up from the same card.

**Move Excluded Items to Trash** saves the library selection, then moves every
Shelf item that came from an unchecked library to [Trash](items.md#trash).
Audiobookshelf itself is not touched, and nothing is lost: restore any of them
from Trash. **A sync does not bring them back on its own.** Re-checking the
library and syncing again skips every item that is in Trash, and the summary
counts them under **In Trash** — restore the ones you want first, then sync.

**If Shelf reaches ABS at a different address than your browser does, fill in
the Browser URL.** Shelf commonly talks to Audiobookshelf over a Docker network
or a LAN hostname — `http://audiobookshelf:80` — while you open it through a
reverse proxy at `https://audiobooks.example.com`. The **Listen on
Audiobookshelf** and **Read on Audiobookshelf** links on an item page are built
from the URL you configured, so with only the internal address set they point
somewhere your browser cannot follow. The optional **Browser URL** field is used
for those links and nothing else — sync, library discovery and cleanup keep
using the Audiobookshelf URL above it. Leave it blank and the links behave
exactly as before.

**Repeat syncs are cheap and safe to run.** An item whose metadata has not
changed since the last sync is left alone — not rewritten, and its cover is
not downloaded again — and the summary on the card counts it under
**Unchanged**, beside Added, Updated and Skipped. An audiobook or eBook whose
ISBN you had already catalogued by hand in the same format is adopted and
linked rather than added a second time; a duplicate of the same ISBN inside
ABS is skipped with a reason. If one library times out, the others still sync
and the timeout is reported for that library alone.

**The sync records each item's ASIN** (Audible's product id) in its own
field, shown in the item page's Integration IDs panel and searchable in
Browse. The ASIN is never stored as an ISBN — an ASIN isn't one, and storing
it there broke the edit form and the duplicate checks — so an ABS item that
has only an ASIN syncs **without** an ISBN. A row that got an ASIN in its
ISBN field from an earlier sync is cleared on the next sync and counted
under **Updated**. An ASIN that is not ten letters or digits is left out,
and an item whose ISBN in ABS fails its check digit is left without it; each
gets a line in the server log naming the item. An ASIN you set yourself, or
one an Audible import stamped, stays when ABS sends none.

**The first sync after upgrading may count items as Updated.** Every ABS
item whose record has an ASIN gains it on that sync, which is a change. Later
syncs go back to counting them as **Unchanged**. ABS fills the ASIN from its
Audible metadata provider; a library matched another way has none, and the
sync changes nothing.

**Libation → Audiobookshelf → Shelf.** If you download your Audible library
with [Libation](https://github.com/rmcrackan/Libation) into a folder
Audiobookshelf serves, sync Audiobookshelf first, then import Libation's
[library export](import-and-export.md#audible). The import matches the
synced audiobooks by ASIN, or by title and author where the sync has not
recorded one yet, instead of adding each title a second time.

A scan that comes back thin tells you **on the card** which of five things
happened: no credential configured, a credential the provider rejected, a
provider that is rate-limiting you right now, a format Shelf has no metadata
source for, or a provider with no match. IGDB makes the same distinctions as
TMDb — a rejected Twitch credential says so rather than reading as a miss.
See [Troubleshooting](../troubleshooting.md#a-scan-added-only-a-title).

## IGDB (video games)

[IGDB](https://www.igdb.com) via Twitch developer credentials — free.

**Adds:** video-game metadata, cover art, platform and series on UPC scan;
title search for retro cartridges; and the title lookup a Photo Intake row
typed Video Game runs when you confirm it.

**Setup:** [dev.twitch.tv/console](https://dev.twitch.tv/console) → Register
Your Application (category "Application Integration", any redirect URL) →
copy Client ID and generate a Client Secret → paste both.

## TMDb (DVDs / Blu-rays)

[themoviedb.org](https://www.themoviedb.org) — free API key.

**Adds:** film metadata and posters from UPC scans, movie title search, and
the title lookup a Photo Intake row typed DVD runs when you confirm it.

**Setup:** TMDb account → Settings → API → request access → paste **either**
credential the API page shows: the 32-character **API Key (v3 auth)** or the
long **API Read Access Token (v4 auth)**. Shelf detects which one you pasted
and authenticates accordingly. Use **Test key** to confirm before saving — it
now probes TMDb exactly the way a real lookup does.

## ISBNdb (valuation)

[isbndb.com](https://isbndb.com) — paid.

**Adds:** list-price valuation per item and in bulk, the insurance report's
numbers, value-over-time stats, and nightly wishlist
[price alerts](wishlist-and-store-mode.md#price-alerts). See
[Stats & valuation](stats-and-valuation.md).

## Google Books (optional API key)

[Google Books](https://books.google.com) — free, and used with no key at all
by default. The key is optional in a way the credentials above are not:
nothing stops working without it.

**Adds:** nothing new. It authenticates the Google Books requests Shelf
already makes, which raises the request quota and makes those requests
answer reliably.

**Why you might want one.** Anonymous Google Books requests are rate-limited
per source IP address, and that budget is shared with everyone else calling
from the same address — your ISP's NAT pool, a VPN exit, a cloud host. Shelf
paces its own outbound calls, but it cannot see the neighbours it is sharing
the quota with, so an anonymous request can come back rate-limited on a
perfectly idle Shelf. A key gives you a quota of your own.

You will notice this only where Google Books is actually reached, which is
less often than it sounds. It is the **last** book source tried on an ISBN
scan — behind the national bibliographies (the Deutsche Nationalbibliothek for
978-3 ISBNs, the Servizio Bibliotecario Nazionale for Italian 978-88 and
979-12 ones), Open Library, and Hardcover — so it answers for the books the
others missed. It also backs
synopsis lookups and book cover search. If Open Library is answering your
scans, a key will change nothing you can see; if you regularly scan books
that come back thin, or you run bulk operations like the synopsis backfill or
a large Photo Intake, it is worth having.

**Setup:** the key comes from Google Cloud, not from a Google Books account.

1. Open the [Google Cloud console](https://console.cloud.google.com) and
   select a project, or create one — a personal project is fine and the Books
   API has no billing requirement.
2. Enable the **Books API** for that project
   ([direct link](https://console.cloud.google.com/apis/library/books.googleapis.com)).
3. Go to **APIs & Services → Credentials → Create credentials → API key** and
   copy the key it shows you.
4. Optional but recommended: **Edit API key → API restrictions → Restrict
   key → Books API**, so a leaked key can do nothing else.
5. Paste it into the Google Books card under Settings → Integrations and
   press **Test Key** before saving. A key with a stray character or a
   missing API restriction reports *"Google Books rejected the API key"*
   rather than failing quietly later.

Application restrictions (HTTP referrer, IP address) are a poor fit here —
Shelf calls the API from the server, not the browser, so a referrer
restriction will reject every request. Leave the key unrestricted by
application, or restrict by the IP address your server calls out from.

**How the key is handled.** It is stored encrypted, shown write-only (the
field renders blank once saved; leave it blank to keep the stored value), and
sent only in the `X-Goog-Api-Key` request header — never in a request URL,
which keeps it out of URLs that might be logged. Remove it at any time with
**Remove saved key**; Shelf falls straight back to anonymous requests.

## LibraryThing (English cover fallback)

[LibraryThing](https://www.librarything.com/developer) — a free API token,
non-commercial use only unless LibraryThing grants permission.

**Adds:** an English cover for a book whose own ISBN has no cover anywhere
Shelf looks. LibraryThing's thingISBN service lists the other ISBNs of the
same work; Shelf tries the English ones against Open Library and Google Books
and keeps the first verified cover. The catalogued ISBN, title, authors and
language are untouched, and the item page says the cover came from another
edition. It runs in **Retry Missing Covers** and adds up to five tiles to
**Find cover**; it never replaces a cover you already have. See
[Settings](settings.md#librarything-english-cover-fallback) for the limits.

## Vision providers (Photo Intake)

Anthropic, any OpenAI-compatible endpoint, or Ollama. See
[Photo Intake](photo-intake.md#setup).

## Notifications (ntfy / webhook)

Not an integration card — lives under Settings → Library → Lending — but the
same idea: an ntfy topic or JSON webhook URL for the overdue-loan digest, the
wishlist price-drop digest and the release-radar digest. See
[Lending](lending.md#reminders),
[Price alerts](wishlist-and-store-mode.md#price-alerts) and
[Series](series.md#watching-for-new-volumes).

## Always-on sources (no key)

Open Library, Google Books (anonymous by default), Amazon cover images, UPC Item DB,
the Deutsche Nationalbibliothek for German ISBNs, the Bibliothèque nationale
de France for French ones, and the Servizio Bibliotecario Nazionale for Italian
ones. Apart from credentials you
explicitly configure, lookups send only the ISBN or UPC — never your account,
collection or personal data. Requests to every provider are paced to its
published rate limit. UPC Item DB's free tier is
the tightest of them at six lookups a minute, so Shelf leaves ten seconds
between consecutive barcode lookups: scanning a stack of discs or games is
deliberately unhurried. One scan on its own never waits, and ISBNs are not
paced this way.

Some of these meter you per day rather than per second — UPC Item DB's free
tier allows 100 lookups a day, and keyless Google Books has a per-day project
quota. Once one is spent it rejects every request until it resets. Shelf does
not wait a daily limit out; it gives up at once, says on the scan card that a
source is rate-limiting you, and names the provider in the log — the card
names no provider, because a book lookup consults up to four and any subset
can be starved at once. See
[Troubleshooting](../troubleshooting.md#a-scan-comes-back-empty-and-the-log-says-a-provider-asked-for-a-long-wait).

## Supplying keys by environment instead

The legacy integration credentials listed here can come from environment variables
(`HARDCOVER_TOKEN`, `GOOGLE_BOOKS_API_KEY`, `ABS_URL`/`ABS_TOKEN`, `ISBNDB_API_KEY`, `TMDB_API_KEY`,
`IGDB_CLIENT_ID`/`IGDB_CLIENT_SECRET`, `LIBRARYTHING_TOKEN`), which overrides whatever is stored. The
secret field stays blank in Settings — Shelf never echoes a secret back — but
**Test key** still works against it, so you can confirm the key without pasting
a second copy in. See [Configuration](../configuration.md#credential-overrides).
New multi-instance OPDS/Jellyfin credentials are saved on **Sources**, not
supplied through these overrides.
