# Connected library (experimental)

This guide describes the implementation on the experimental `new-test` branch.
It is not a released feature or an upstream design commitment. **Back up your
database, covers and external key files before trying it.** See
[Backup and portability](#backup-and-portability) below.

Connected library adds **Library**, **Available here**, **Sources**, **Needs
Attention** and **My Activity** alongside the existing catalogue. Browse remains
the place for collection administration and bulk changes. Shelf is still a
catalogue and front door to other services, not a media server or player.

## Enable or hide the experiment

An admin controls **Connected library** in Settings → **Features**. It belongs
to the Standard profile (and Everything); Minimal leaves it off. On upgrade,
migration 58 leaves it off when all seven pre-existing Standard features were
explicitly off, unless a Connected library choice was already saved.

Turning it off hides and refuses its pages and item panels **without deleting
their data**. Browse, scanning and the existing shared catalogue remain usable.
New source setup is also behind this switch; unlike the legacy integration
cards, it is not configurable while Connected library is off.

**One shared library, not separate libraries per person.** All imported
catalogue entries are visible to all Shelf users, including viewers. Source
credentials or a selected Jellyfin user limit what Shelf imports; they do not
restrict which Shelf accounts can see those entries. There is no household or
per-source catalogue privacy isolation. Personal activity and saved views are
account-local, not a permissions boundary around the catalogue.

## Find editions in Library

Open **Library** (`/library`). It searches the locally stored, live catalogue;
opening or filtering it does not query a remote service. Search covers titles,
creators, ISBN/UPC/ASIN, narrators, local series, publishers, tags and supported
stored external IDs, including validated source-snapshot IDs such as IMDb,
TMDb, TVDb and MusicBrainz album/release IDs. Search words must all occur on
the same item, though they can occur in different fields. Search is
Unicode-aware and case-insensitive; it does not remove accents.

### Groups are equivalent formats, not adaptations

**Grouped formats** stacks editions connected by `format` links, including
transitive links: print → eBook → audiobook is one stack. A singleton is still
one group. `related` and `adaptation` links do not join stacks, nor does a
matching title on its own. A film adaptation therefore stays separate from the
book. Trashed items are hidden and cannot bridge two live editions.

The first card is a matching edition. Expand **show other formats** to see the
full live stack. Nonmatching siblings are labelled **Other format — does not
match filters**. **Individual items** shows only matching editions instead.
Use an item's **Related Media** panel to manage explicit relationships.

### Filters and counts

Open **Filters and display** for Format, Media family, Local series, Holding source, Availability,
Catalogue ownership and Wishlist. Ownership and wishlist membership are
independent: an imported digital holding need not be owned or wishlisted.
An item's original import `source` is not evidence that it still has a holding.
Search and quick presets stay visible when this disclosure is closed; closing
it does not clear its selected filters. View, sort and page size are there too.

Two rules matter when combining filters:

- **All filters must match one item in a stack.** A print book cannot satisfy
  Format: Book while its linked eBook supplies Available digitally. Choose
  the eBook format, or leave Format unrestricted, to find that digital edition.
- **Source and availability must match one holding on that item.** An unavailable
  record on server A cannot borrow an available record on server B to match
  Source: A + Available.

Counts are computed across all matches **before pagination**. “Matching items”
counts matching editions; “groups” counts distinct format stacks with at least
one match. Showing nonmatching siblings does not increase either count. In
Grouped formats, Results per page counts whole stacks, never splits a stack
across pages and does not repeat it on another page. Individual items paginates
matching items. Local series paginates series names until you open a series,
then paginates its format stacks. The default page size is 24; the selector
also offers 48 and 96.

Availability filters mean:

| Filter | Meaning |
|---|---|
| **Available** | At least one recorded physical or cached digital holding is available |
| **Available digitally** | At least one cached digital holding is available |
| **On my shelves** | A live physical copy is neither marked missing nor on an item-level loan; it may still lack a location |
| **Lent out** | A physical copy's item has an open loan |
| **Missing copies** | A live physical copy is marked missing |
| **Unavailable holdings** | At least one holding has explicit recorded unavailability, not merely a failed connection |
| **Unknown availability** | An unknown holding, or no recorded holdings when no source is selected |

These are “has a holding” filters, not exclusive classifications: an item with
several holdings can match more than one availability filter.

### Local series and saved views

**Local series** groups the stored series names after trimming and
case-folding; punctuation, accents and scripts are preserved. It does not
consult remote series IDs or fetch a completeness report. Open a series to see
numeric position order, including fractional positions, with unknown positions
last. The existing **Series** page and its optional integrations remain separate.

Under **Personal saved views**, name and save the current filters, display
mode, sort and page size. Every role can save up to 50 views for its own
account; names are 1–80 characters. Saving the same name, case-insensitively,
replaces that account's view. Page number is not saved. Loading a view does not
change the catalogue; explicit URL filters, including empty values, override
the saved filters. Other accounts cannot load or delete your saved view by ID.

## Available here

An item's **Available here** panel combines holdings for that edition and its
live, format-linked editions. It does not include adaptations. It reads local
copies, the existing Audiobookshelf/Komga/RomM bindings, and new connected-source
records. **It is cached evidence, not a live reachability check.** A Read,
Listen, Watch, Play or Open link opens another service; that service may still
require login or refuse access.

- Physical holdings come from actual live copy records, not the **Owned** flag
  or a location field alone. Missing takes precedence over lent. Loans are
  item-level, not copy-specific, so an open loan conservatively marks every
  nonmissing copy of that item as lent.
- Digital holdings retain their source and last-seen information where
  recorded. A recent attempt is not proof that an item was seen. In particular,
  Audiobookshelf's old scheduler timestamp is not a per-item last-seen time.
- A failed or partial source check makes retained digital holdings **Unknown**,
  not absent, unless unavailability was already explicitly confirmed. A disabled
  new source likewise retains cached rows as Unknown and removes its open links;
  previously confirmed unavailability remains Unavailable.
  Turning off an existing integration's own feature hides its holdings.
- No recorded copy or source binding means availability is unknown. It does
  not mean that you own nothing or that a remote service is offline.

**Digital Video** (`digital_video`) and **TV Series** (`tv_series`) are separate
media types from physical **DVD / Blu-ray** (`dvd`). Jellyfin movies and whole
series use those digital types; importing one does not create a disc or a
physical copy. Their shared status controls use watched/watching wording.
They do not acquire a book title-search provider merely by being new types.

## Connect sources

Open **Sources** (`/sources`), also in the account menu under Library. All
users can see a redacted overview. Only admins can open source configuration,
add sources, test, sync, disable, attach or remove manual links. **Existing
integrations** shows Audiobookshelf, Komga and RomM health and cached-record
counts; admins get links to their original Settings cards. **Additional
sources** lists the new instances. Neither section probes a server on page load.

The new framework supports multiple independently configured **OPDS
catalogue**, **Jellyfin** and **External library links** instances. An external
record ID is scoped to its source instance, not globally unique.

1. Choose **Add source**, select Provider, and give it a Name and **Server or
   OPDS feed URL**. Manual External library links may leave the URL empty.
2. Open the source detail page and expand **Configuration** if closed. Set credentials and **Options JSON**, then
   **Save settings**. An optional **Browser URL prefix** supplies an address
   your browser can reach when Shelf uses an internal server address.
3. **Test connection** reads the catalogue without importing entries or changing
   sync timestamps. **Sync now** imports/updates cached records. Both contact
   the remote service; ordinary source pages do not.

Use credential-free HTTP(S) URLs, including any reverse-proxy base path. LAN
and localhost endpoints are intentional. Server/browser roots must have no
userinfo, query string or fragment. Put credentials only in the credential
fields, not a URL. Prefer HTTPS: an HTTP root sends its configured credentials
over an unencrypted connection, even though they are encrypted in Shelf's database.
Saved fields render blank: leave **all** credential fields
blank to keep them, or enter a complete replacement. **Clear saved credentials**
removes them. Credentials use the existing external encryption key and are
never rendered back as raw secrets.

### OPDS catalogue

Supply an **OPDS feed URL**, not an ebook file URL. Anonymous feeds and HTTP
Basic authentication (Username and Password) are supported. The reader accepts
OPDS 1 Atom and OPDS 2 JSON, follows same-origin `next` pages and bounded
subsection/navigation feeds within the configured scope. Search, facets,
up/start/related links and acquisition files are not fetched.

Supported Options JSON keys:

| Key | Default | Allowed values |
|---|---|---|
| `media_type` | `ebook` | `book`, `ebook`, `audiobook`, `comic`, `manga`; applies to this feed's imported entries |
| `max_pages` | `20` | Integer 1–50 |
| `max_entries` | `2000` | Integer 1–5000 |
| `max_depth` | `2` | Integer 1–4, for navigation/subsection traversal |

The root feed defines scope: navigation stays beneath its path, or its
directory when the root is file-like. Credential-free **open-access** and
generic **acquisition** links (including Calibre-style
`http://opds-spec.org/acquisition`) can become open actions. Buy, borrow, sample,
subscribe, priced and token-bearing offers are excluded; a generic relation
cannot override a paid/restricted declaration for the same target. Shelf does
not download those files, proxy authentication to the browser, serve an OPDS
feed itself, or host a reader. A feed can import entries without usable open
links, and an offered link may still require the source's own browser login.

For OPDS, the Browser URL prefix rewrites only links on the configured server
under the configured feed-root path boundary. An acquisition link elsewhere on
that server or on another origin is not automatically rewritten; check the
offered link from the browser you will use.

Traversal is also bounded by response/total byte limits and a time limit. A
limit, cycle, invalid entry, failed child feed or incomplete reported total
makes the result partial; it is not permission to mark unseen entries absent.
Compressed catalogue responses are refused; the server must honour identity
encoding for bounded response reading.

### Jellyfin

Supply the Jellyfin server root and **API or access token**. `user_id` in
Options JSON selects whose visible catalogue to enumerate. A user-owned token
can resolve the user through `Users/Me` when `user_id` is omitted; a server API
key needs an explicit `user_id`. Optional `library_ids` is a list of library/view
ID strings; omitted or empty means all views visible to that Jellyfin user.
Selected libraries must be visible to that user, even with an admin API key.
Use the final server URL: redirects are refused.

Only these Jellyfin types are imported:

| Jellyfin type | Shelf type |
|---|---|
| `Movie` | `digital_video` |
| `Series` | `tv_series` |
| `MusicAlbum` | `digital_music` |
| `Book` | `ebook` |
| `AudioBook` | `audiobook` |

Series and albums stay whole. Episodes, seasons, individual audio tracks
(including audiobook files reported as `Audio`), playlists, collections and
other types are not imported. Browser links open Jellyfin's web details page;
they carry no token and do not start native playback in Shelf. There is no
image/media download, playback, transcoding, progress sync or remote write.
A pass imports at most 5000 records, with additional page, byte and time bounds;
narrow the selected libraries for a larger catalogue. Detected pagination
overlap or changing totals also prevents complete-absence reconciliation.
Jellyfin must honour identity encoding; compressed catalogue responses are
refused rather than automatically decompressed.

### External library links

This provider is **manual only**: there is no catalogue discovery, connection
test or sync. Create a source, then use **Attach external availability** on its
detail page. Supply an existing live **Catalogue item ID**, a stable **External
record ID**, a credential-free **Open URL**, and optionally **Format**.

Attaching records access without changing the item's metadata, ownership,
wishlist or copies. Reusing the same external ID updates its attachment to
the same item; it cannot move that identity onto another item. There is no
automated check that the URL still works. Disable the source to stop offering
its links, or choose **Remove link only** beside a record to delete that manual
binding without deleting the source or catalogue item. Removal is an admin-only,
CSRF-protected POST to `/api/sources/{source_id}/records/{record_id}/detach`;
it cannot detach OPDS/Jellyfin machine-synced records. This is not a universal
connector for commercial libraries or DRM.

### Import, sync and absence rules

For new OPDS/Jellyfin sources, a sync first uses the existing source binding.
Without one, it can attach to a unique live item with a validated matching
ISBN **in the same media type**. Ambiguous ISBN matches are skipped. Title
similarity never adopts or merges a local item, and a matching ISBN in another
format does not automatically add a format link.

Otherwise Shelf creates a catalogue item **neither owned nor wishlisted**,
with no physical copy. It stores title, creator, media type and a valid ISBN
when supplied, plus validated descriptive fields the adapter supplies:
publisher, publication year, series/position, description, language, page count
and duration (seconds converted to whole minutes). A supplied subtitle is
creation-only. Not every adapter provides every field.

Repeat syncs refresh the source snapshot/holding and use the three-way policy
below for descriptive catalogue fields. Existing nonempty manual values and
known local clears survive; genuinely missing fields may be filled, and
source-applied values may follow later corrections. The new-source refresh
does not reclassify the local media type or replace its ISBN. Bounded,
validated `provider_ids` are cached for supported identifier searches, not
used to merge titles automatically. “Updated” may still mean only a snapshot
update when no catalogue value changes.

| Outcome | What it means |
|---|---|
| **Complete** | The configured scope was fully enumerated without warnings. Previously attached live records not seen in that scope may be marked Unavailable; catalogue items are retained |
| **Partial** | Valid returned records can be imported, but unseen records are not marked unavailable |
| **Error** | The failed fetch does not remove existing records or replace the last complete success |

**Last attempt** and **Last complete success** are separate. Partial results
and errors do not advance the latter. A later complete sync that sees a record
again restores its cached availability. Changing the configured feed or library
scope changes what a complete enumeration covers: review that scope before
syncing. **Nothing here automatically restores items from Trash.**

### Change configuration, disable or re-enable

Changing a syncable source's server root, Browser URL prefix, effective
scope/traversal limits or credentials resets its cached holdings to **Unknown**
(including old Unavailable verdicts), clears attempt/success/report state and
returns its status to Never. A new observation is needed before those old
holdings can be treated as available or absent again. A display-name change,
unchanged settings, explicit default options or a reordered identical Jellyfin
library selection preserves health. A nonblank credential replacement resets
health even if you re-enter the same secret; blank fields keep it.

**Use a new source instance for an unrelated server.** Editing a root retains
the instance's external-ID bindings and is not an identity reset. Narrowing a
scope never hides or deletes already imported catalogue entries from other
Shelf users; a later complete enumeration can mark their old holdings absent.

Disabling a source removes its opening actions, but keeps its items, bindings
and health. Re-enable with **Enabled → Save settings**. With no material change,
the previous cached verdict returns; this does not perform a live check.
Material changes still require a new sync. Manual links have no enumeration
lifecycle: re-enabling makes their saved links usable again without sync.

New sources sync manually; there is **no new scheduler**. Audiobookshelf keeps
its existing optional schedule, while Komga and RomM remain manual. Those
three legacy integrations remain singletons with their original settings and
bindings; they are not converted into writable `source_instances` rows. Set
them up through their existing Settings → Integrations cards:
[Audiobookshelf](integrations.md#audiobookshelf), [Komga](../komga.md) and
[RomM](../romm.md).

## Review catalogue quality

Editors and admins can open **Needs Attention** (`/needs-attention`) from the
account menu. Reading it makes no remote requests and repairs nothing. Its
queues cover undismissed missing covers, missing meaningful creators,
unlocated physical copies, incomplete periodical issues, source checks,
confirmed unavailable source records and proposed format connections.

Source checks include configured, enabled legacy integrations as well as new
syncable sources. “Stale” means over seven days since a successful complete
check, **not** since the last attempt. Manual links need no sync and are not
flagged merely for never syncing. Quality counts describe the whole queue,
but detail rows are bounded (50 by default, at most 100 per queue).

### Review format connections

Suggestions are computed from compatible prose formats (`book`, `ebook`,
`audiobook`) or music releases, using validated shared identifiers or a whole
title **and subtitle** with a shared named creator. A title alone, unknown
creator, shared translator or narrator is not enough. Conflicting known work
IDs veto a suggestion. Comics/manga, films, games and periodicals are not
guessed as prose equivalents or adaptations.

Review both records and the displayed evidence before choosing **Accept
format connection** or **Not the same work**. Acceptance adds only a `format`
link: it never merges items or changes metadata. Undo with **Unlink** under
**Related Media** on either item. Dismissals apply to the current evidence;
changes to relevant titles, creators or identifiers can make a proposal appear
again. **Restore suggestion** removes an effective dismissal. A stale review
submission is refused so you can reload and review the new evidence.

The evidence scan is bounded to 200 suggestions and a candidate budget, not an
exhaustive duplicate total; a `+` indicates a scan limit. Existing direct
related/adaptation links are not converted. The older Audiobookshelf/Audible
format-link path still exists, restricted to compatible book formats using a
valid ISBN or an exact meaningful title with a real shared creator; it is not
an automatic record merge.

### Local metadata edits survive provider refresh

Audiobookshelf, Komga, RomM and new OPDS/Jellyfin refresh paths record per-field,
per-source provenance. They compare the incoming observation, the known
provider-applied baseline and the current local value. Missing fields can be
filled; fields the source actually applied may follow later corrections until
a local override. Local edits, including an explicit clear to blank or NULL
after a known observation, are preserved on repeated syncs. Empty provider
values do not erase metadata.

For older items without an applied baseline, a nonempty local field is
preserved even if the first remote observation happens to match it. An
unobserved empty legacy field cannot be distinguished from a genuine gap.
Komga's adopted manual items are fill-only by default; an explicit **Follow
source** decision can opt a field in that adapter's refresh set into later updates.
Covers are fill-only rather than replaced on metadata refresh; metadata
provenance does not grant permission to change ownership, copies, locations,
tags, shared status or personal activity. Merging records preserves source
observations without transferring a losing source's permission to overwrite
the kept record's manual fields, even when the old values happened to agree.

### Review Metadata sources

Open **Metadata sources** on the item page. All roles can compare saved local
and source-observed values and their observation time; opening the panel makes
no provider request. Editors/admins can resolve a saved descriptive field:

- **Keep local** locks the current value for that source, including a blank.
- **Follow source** immediately applies that saved observation and opts into
  later updates where the adapter refreshes that field, while unmodified. A new
  local edit becomes an override again. This is also the explicit way to opt
  back in after a merge. Adopted Komga titles remain local by default, but an
  explicit Follow source decision lets an unmodified title follow later syncs.

Review supports title, authors, narrator, description, publisher, publication
year, page count, duration, language and series name/position. Identity fields
(ISBN/ASIN/UPC, media type and platform) are read-only here. A source title must
be nonempty to follow; an explicit Follow can clear an optional field when its
saved source value is blank. No observation means no decision to apply.

The panel is bounded to 80 saved fields and shortened display text. Each
decision rechecks the full saved observation and current local value: stale
forms are refused, so reload and review again. Its session routes are GET
`/api/library/items/{item_id}/metadata` and editor POST
`/api/library/items/{item_id}/metadata/resolve`, with `source_key`, `field_name`,
`fingerprint` and `decision` (`keep_local` / `follow_source`). The form never
supplies a replacement source value and does not contact the provider.

### Removed covers and source artwork

**Remove cover** returns the item to human cover review and clears any old
**Not available** queue dismissal. Separately, it records a durable source-cover
block: Audiobookshelf, Komga and RomM sync will not refill that removed artwork,
even if its old file is later cleaned up. This is not a dismissal of the review
queue. New OPDS/Jellyfin sources do not download artwork in the first place.

Editors/admins can still use **Find cover** or upload a cover by hand. To permit
source refill again, choose **Allow missing-cover sync again** on the item page
(POST `/api/items/{item_id}/cover-sync/allow`, with CSRF). This records an
explicit allowance and clears queue dismissal; it does not itself fetch a
cover. The next eligible source sync may fill a missing cover, never replace
one already chosen. These cover controls remain core when Connected library
is off.

This policy is specific to source artwork. The existing book-cover startup
queue and explicit retry tools are separate paths, not a universal ban on all
cover lookups.

Digital Video artwork search uses the existing TMDb movie-poster provider and
requires its configured key. TV Series has no artwork-search adapter yet; its
picker explains that limit and still offers an image URL or upload. Neither
format falls back to book artwork.

## My Activity is account-local

Every signed-in role, including viewers, can use an item's **My activity**
panel and **My Activity** (`/activity`, account menu → Personal):

- Status: Planned, In progress, Finished, Paused or Abandoned, or no personal
  status.
- Optional whole-number progress (0–100%) and rating (1–5), Favourite,
  **My List / Up Next**, and up to 5000 characters of personal notes.
- **Continue** shows personally In progress items. **My List / Up Next** shows
  items explicitly on My List or personally Planned; it is not inferred from
  shared ownership, the shared wishlist, series order or remote progress.
- Finished sets progress to 100%; reopening clears the finished timestamp.
  Blank progress/rating clears the value. Saving a real change records a
  personal history event; an identical submission does not add another.
- **Reset my activity for this item** clears your current state but keeps your
  personal history. Trash hides the item and its activity until restored.

The item panel starts with a compact summary. Open **Update my activity** to
edit or reset it. Controls are temporarily disabled during Save or Reset, so a
second action cannot race the first; they become available again when it ends.

This state belongs to the signed-in account **and the specific catalogue item**.
Progress on an audiobook does not transfer to its print/eBook siblings, and no
cross-format page/time conversion is attempted. Saving it does not change the
shared catalogue status, ownership, wishlist, other users' activity, reading
log or Hardcover. It does not sync progress to/from external services.

The existing **Shared catalogue status** (read/watched/played) remains writable
by every role, including viewers, with its existing shared history and optional
Hardcover sync. Choose that shared control deliberately; it is not the personal
panel. Account-private Home/item HTML is marked `Cache-Control: no-store`;
Store Mode keeps its separate offline behaviour.

## Backup and portability

These are different operations:

| Method | Connected-library data |
|---|---|
| **Full database backup** (Settings → Data → Backup & Restore) | Retains new source instances/records, encrypted credentials, personal state/history, saved views, connection dismissals, metadata provenance and cover-sync suppression/opt-in decisions. Contains **no covers or external key files** |
| **Copy the data directory** | Also carries covers and key files stored there; stop Shelf first for a consistent copy, and separately preserve any environment-supplied keys |
| **Portable collection archive** | Carries supported shared catalogue/copy data and covers, but **excludes new connection configuration, credentials, source bindings, personal state/history (including ratings), saved views, connection dismissals, metadata provenance and cover-sync suppression/opt-in decisions** |

The additive tables are `source_instances`, `source_records`, `user_item_state`,
`user_item_events`, `library_saved_views`, `connection_dismissals`,
`item_metadata_provenance` and `cover_sync_blocks`, all owned by
`MIGRATION_TABLES` in `app/database.py`. They exist even with the feature off;
an upstream upgrade does not invent personal activity or new source configuration
from shared legacy catalogue data.
Migration 59 adds `user_item_state.last_event_id` (default 0) so personal merge
recency belongs to the saved state, not to later reset/merge history events.

**Older fork databases:** the 0.37/0.39 fork already had private
`user_item_state` rows with a different layout. Startup now recognizes that
layout before creating the new indexes, adds the missing fields, and maps each
account's existing status, dates, notes and personal wishlist into the new
fields. Ratings, favourites and timestamps stay intact. Valid progress with a
known total (or an explicit percent unit) becomes a whole percentage; unscaled
progress is not guessed. Finished entries become 100%.

Original columns, precise progress values/units and `user_reading_log` remain
in the database; the old reading-history table is not replayed into the new
activity timeline. Original overlapping migration records are archived in the
`connected_library_legacy_migrations_v1` setting before current migrations run.
This bridge is safe to retry after the `no such column: status` startup error
and does not reapply old values over later edits. Unknown migration namespaces
are refused rather than guessed. **Keep the existing data volume and keys;
do not delete the database to work around this error.** Back up the stopped
data directory before retrying an experimental upgrade.

**Pre-0.37 fork databases:** a separate bridge handles the older migration
namespace where version 24 is **Add komga_id column**, including the reported
versions through 66. This is not just a different private-state table: physical
copies reference `location_nodes` instead of `locations`. The bridge projects
the hierarchy, rebuilds that copy foreign key while retaining copy IDs,
barcodes, positions, notes, missing marks, dependent rows and ID high-water,
and retains the original node/holding/music/library tables. Known Komga/RomM
identities become current cached bindings without contacting either server;
the old digital comic/manga/game vocabulary is translated without merging
items. Unknown schema extensions, binding conflicts and vocabulary collisions
stop the upgrade rather than discarding or merging data. The entire pre-0.37
conversion rolls back together on failure.

The experiment still has **one shared catalogue**: retained old library and
membership tables do not make their former visibility restrictions active.

After building `shelf-rpaisley:test`, verify the stopped `shelf-test` database
on a disposable copy **before** redeploying:

```bash
docker run --rm --network none --volumes-from shelf-test:ro \
  --entrypoint python shelf-rpaisley:test -m app.verify_upgrade
```

Keep all writers to that data directory stopped while checking. The command
copies the database/WAL into temporary storage, runs initialization twice and
checks foreign keys and SQLite integrity. It never opens the original database
through SQLite, changes its files, or prints catalogue/private/credential
values. Continue only if it prints **Upgrade verification passed**. This tests
the migrations with the image's actual Python/SQLite runtime; it does not test
live integrations or replace the pre-upgrade backup.

Preserve the matching `data/encryption.key` and `data/signing.key`, or the
`SHELF_ENCRYPTION_KEY` and `SECRET_KEY` environment values, separately from a
downloaded database backup. The matching encryption key is needed to reuse
saved credentials; database ciphertext cannot recreate it. Restore requires a
restart and signs out existing sessions. An optional backup passphrase protects
the download, not a replacement for the external credential-encryption key.

**Do not use a portable archive as a personal-state or source-configuration
backup.** It cannot recreate those excluded records on another install. A full
database backup contains personal notes/history and password hashes as well
as encrypted credentials, so protect it accordingly. See
[Upgrading & backups](../upgrading-and-backups.md) for the general restore steps.

## Intentional limits

This experiment does not add isolated household libraries, a general commercial
provider connector, DRM handling, native playback, transcoding, a hosted reader,
an OPDS server or external progress sync. Adapter fixtures and regression tests
are present; final branch verification is separate, and a fixture is not proof
of compatibility with every live OPDS or Jellyfin server.
