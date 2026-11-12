# FAQ

**Does Shelf need an internet connection?**
For metadata and covers, yes — lookups go to Open Library and friends. The
catalog itself, Browse, Stats, lending and Store Mode's scan verdicts all work
offline. Nothing phones home.

**Is my data sent anywhere?**
Only the ISBN/UPC being looked up goes to metadata providers — plus the title,
where that is all Shelf has: a title search, or a Photo Intake row typed DVD or
Video Game, whose title goes to TMDb or IGDB when you confirm it. Photo Intake
also sends the photo to whichever vision provider you configured — choose
Ollama for fully local. Share links are served by *your* server.
If you configure connected sources on the experimental branch, Shelf also
contacts those servers with the configured credentials to read their catalogues.
Account-local My Activity is not sent to them or to Hardcover.

**Do I need any API keys?**
No. Books, covers, DVD titles (via UPC Item DB) and everything else core work
keyless. Keys unlock extras: Hardcover (sync, series checks), IGDB (games),
TMDb (discs), ISBNdb (valuation), a vision provider (Photo Intake). The
keyless tiers are metered per day, though — a heavy cataloguing session can
exhaust one, after which scans come back empty until it resets. A key raises
or removes that limit. Google Books is the one source that accepts a key
without needing one: it works keyless, and a key of your own only buys you a
quota that nobody else is sharing.

The scan card tells you which of these happened rather than making you guess:
no key configured, a key the provider rejected, a provider that is
rate-limiting you right now, a format Shelf has no metadata source for (CDs,
today), or a genuine miss. See
[Troubleshooting](troubleshooting.md#a-scan-comes-back-empty-and-the-log-says-a-provider-asked-for-a-long-wait).

**Why HTTPS with a self-signed certificate?**
Phone cameras and offline mode both require a secure origin, so Shelf
serves HTTPS by default; without a domain, self-signed is the only way to do
that out of the box. Behind a reverse proxy that terminates TLS you can set
`SHELF_TLS=off` and let the proxy supply HTTPS instead. (The one exception is Photo Intake's **Take photo** button on a
phone, which opens the native camera app rather than using `getUserMedia`
and works over plain `http://`; everything else needs the secure origin,
from either Shelf's built-in certificate or your HTTPS proxy.)
With the built-in certificate you can trust it on your devices or front it
with a real one —
see [HTTPS & reverse proxy](https-and-reverse-proxy.md).

**Can I use a USB barcode scanner?**
Yes — any scanner that acts as a keyboard and sends Enter. No setup.

**Which browsers does Shelf need?**
Since 0.68.0: Safari 16.4, Chrome 111, Firefox 128 or newer (Edge and other
Chromium browsers follow Chrome). An older browser still loads the pages, but
some colours and styles are missing.

**Does camera scanning work on iPhone?**
Yes, since 0.10.0 (iOS Safari uses a ZXing decoder). It needs HTTPS and a
camera permission.

**What barcodes are supported?**
EAN-13 (ISBN-13 and UPC-like), EAN-8, UPC-A, UPC-E. ISBN-10s can be typed.

**What about books with no barcode?**
Add it by hand: the Scan page's **Add by hand** panel needs nothing but a
title, and covers every media type. Title search (Open Library) and Photo
Intake of the cover face-up are the other two routes — the model recognizes
the cover, and reads the printed ISBN if the back cover is showing.

**Can several people use it?**
Yes: admin / editor / viewer roles on one shared library. The experimental
`new-test` branch also has account-local progress, ratings, favourites, notes,
history and saved Library views. These do not partition the catalogue: every
Shelf user can see imported entries, and there is no household isolation. See
[Connected library](user-guide/connected-library.md).

**Can I import from Goodreads / StoryGraph / LibraryThing / Libib / Audible?**
Yes — all five: upload the export as-is and the format is detected. The
LibraryThing and Libib importers are labelled beta. Audible comes in through
an export from audible-cli or Libation; Shelf never signs in to Audible. See
[Import & export](user-guide/import-and-export.md#reading-trackers-and-catalogue-apps).
Anything else: CSV with at least a `title` column.

**How do I move Shelf to another machine?**
Copy the `data/` directory — or export a portable archive and import it on
the new instance for shared collection data. In the connected-library
experiment, an archive **does not move source configuration/bindings, personal
state/history, saved views, connection dismissals, metadata provenance or
cover-sync suppression/opt-in decisions**.
Use a full database backup plus covers and matching external keys for a
full-instance move. See [Connected-library backup limits](user-guide/connected-library.md#backup-and-portability)
and [Upgrading & backups](upgrading-and-backups.md).

**How do I back up?**
Copy `data/`, or Settings → Data → Backup & Restore (optionally
passphrase-encrypted). Portable archive for a credential-free copy with
covers, not for the experiment's personal or connection data. A downloaded
database backup keeps those tables but includes neither covers nor external
encryption/signing keys; preserve those separately.

**Does “Available here” mean a service is online?**
No. In the connected-library experiment it combines actual local copies and
cached Audiobookshelf, Komga, RomM and new-source holdings. A failed or partial
check is not proof of absence; an open link can still need the service's own
login. Ownership alone does not create a physical holding. See
[Available here](user-guide/connected-library.md#available-here).

**Where are the covers stored?**
`data/covers/`. They're downloaded once and served locally.

**Why did a book get the wrong cover / edition?**
Metadata sources key on ISBN but sometimes collapse editions. On the item
page use **Find cover** to pick another — it works even when the item
already has a cover, not just a blank one — or **Edit** to fix the record.

**I removed a cover by mistake — can I get it back?**
Not the exact file, but the item goes straight back into **Review covers
needing attention** (Settings → Data → Maintenance), where you can search or
upload a replacement without hunting for the item first. Removing a cover also
clears any previous **Not available** verdict, so it is the way to un-dismiss
something. On `new-test`, removal separately pauses Audiobookshelf/Komga/RomM
source artwork until an editor chooses **Allow missing-cover sync again**.
You can still choose/upload a cover manually; existing book-cover startup and
retry paths remain separate. See
[Removed covers](user-guide/connected-library.md#removed-covers-and-source-artwork).

**I marked something "Not available" by mistake.**
There is no undo button yet. Two routes back: give the item a cover and then
remove it (removal clears the verdict), or run **Retry missing covers**, which
deliberately ignores the flag for exactly this reason.

**Is the valuation a resale value?**
No — ISBNdb list price, i.e. replacement cost. Right for insurance, not for
selling.

**Can I change the currency?**
Yes, Settings → Library → Collection. Formatting only; no conversion.

**Is there an API?**
A small one. Read-only API tokens and `GET /api/v1/stats` — item counts,
loans, collection value and upcoming releases — feed dashboards such as
Homepage and Home Assistant; see [Homelab integration](homelab-integration.md).
The rest of `/api/v1` (items, loans, wishlist, series) is on the roadmap.
Every other route serves the UI and needs the session cookie.

**Kobo / Kindle / OPDS / reading ebooks in the browser?**
The experimental `new-test` branch can **ingest** bounded OPDS 1/2 catalogues
and offer safe generic acquisition/Calibre or open-access links, alongside
Jellyfin and manual external links. That is not **serving** an OPDS feed, device delivery, DRM handling or
hosting an ebook reader. Reading/playback stays with the external service.
See [Connected sources](user-guide/connected-library.md#connect-sources).

**Can I choose whether local metadata follows a source?**
On `new-test`, open an item's **Metadata sources**. Viewers can inspect saved
observations; editors/admins can **Keep local** (including blank) or **Follow
source** for a descriptive field. It does not fetch a new observation or change
identity fields. See [Metadata review](user-guide/connected-library.md#review-metadata-sources).

**What's the license?**
AGPL-3.0. Free to use, self-host and modify; if you run a modified version as
a network service you must publish your changes under the same license.

**Something else?**
[Troubleshooting](troubleshooting.md), then
[open an issue](https://github.com/dgahagan/shelf/issues).
