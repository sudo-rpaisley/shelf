# Import & export

All under Settings → Data. Four mechanisms, each for a different job.

| | Best for | Covers included? | Credentials/users? |
|---|---|---|---|
| **CSV** | Spreadsheets, other apps, quick bulk entry | No (re-fetched) | No |
| **Goodreads / StoryGraph import** | Migrating a reading history | No (re-fetched) | No |
| **Portable archive** | Moving Shelf to a new server, giving someone your library | **Yes** | No |
| **Database backup** | Disaster recovery of *this* instance | No | Yes (hashed/encrypted) |

**Trash is carried by the database backup only.** The CSV export and the
portable archive contain what is in your library, not what is in
[Trash](items.md#trash); a later release will carry deleted items through both.

## CSV export

**Import / Export → Export CSV** writes one row per item:

`title, authors, isbn, media_type, platform, publisher, publish_year,
page_count, series_name, location, source, estimated_value, manual_value,
owned, wishlisted, tags`

`owned` and `wishlisted` are each `1` or `0`, so the file records all three
states: owned (`1`, `0`), on your wishlist (`0`, `1`) and neither (`0`, `0`).
Re-importing the file into an empty library brings all three back.

`tags` holds the item's tags, separated by `; `.

Items in [Trash](items.md#trash) are not exported.

## CSV import

Upload a CSV. Headers are matched case-insensitively (spaces → underscores),
so Shelf's own export round-trips, and any file with at least a `title`
column imports. Rows that already exist are skipped, or refreshed — see
**Duplicate mode** below — and either way reported.

A row is matched against your library by `isbn` + `media_type` when it has an
ISBN, and by title + author + media type when it doesn't — so games, DVDs and
ISBN-less books are recognised as duplicates too, and re-importing your own
export adds nothing. A row without an ISBN is only ever matched against other
items that also lack one: it will not be folded into an edition you own that
*does* have an ISBN, because those are different copies.

**ISBN form doesn't matter.** `0441172717`, `9780441172719` and
`978-0-441-17271-9` are the same book, so a file carrying any of them matches
the copy you already own — whichever form Shelf stored it under.

**Tags.** Import is **additive**: a `tags` cell adds tags, and never removes
one the item already has. A file with no `tags` column and a row with an
empty `tags` cell are the same thing — both leave existing tags alone, so
there is no way for a CSV row to clear an item's tags.

**A row whose `media_type` is `kids_book`** — an export from Shelf 0.42.x or
earlier — imports as a `book` **and gains the `Kids` tag**, matching what the
upgrade did to rows already in your library. It also matches an existing
`book` with the same ISBN rather than adding a second row.

**Owned and wishlist columns.** A row's `owned` and `wishlisted` values
(`1`/`0`, `true`/`false` or `yes`/`no`) are applied as given, in this order:

1. An owned row is never put on the wishlist — `owned=1, wishlisted=1`
   imports as owned, without an error.
2. Otherwise the row's own `wishlisted` value decides.
3. With no `wishlisted` value, the **Import "to read" books as wishlist**
   option decides (see Options).

A file with no `owned` column is treated as an export from an earlier Shelf,
where `wishlisted=1` meant *not owned*: those rows arrive on the wishlist and
not owned, and every other new row arrives owned.

**Duplicate mode** — what happens to a row that matches something you own:

- **Skip** (the default) — the row is counted as skipped and nothing changes.
- **Update** — the matched item's metadata is refreshed from the row. Owned
  and wishlist state change only where the file has a value for them: a
  file without an `owned` or `wishlisted` column leaves that part of every
  matched item as it was.

**A row that matches an item in Trash restores it** — it comes back as it
was, is counted as restored in the summary, and then Skip or Update applies to
it like any other match. Re-importing a file never adds a second copy of
something you deleted.

Those are the only two. Any other value is refused outright: the whole file is
rejected with an error, before it is read, and nothing is written.

Options:

- **Fetch covers after import** — look each ISBN up in the background and
  fill in covers, publishers, descriptions. Only book-ish
  media types are enriched: discs and games are left alone, because a
  title-only lookup for one can match a novel of the same name.
- **Import "to read" books as wishlist** — a row with a to-read status that
  is not owned goes on the wishlist. With the option off, it arrives neither
  owned nor wishlisted. It never changes whether a row is owned.

Errors are reported per row (missing title, over-long fields, an ISBN whose
check digit doesn't add up, a media type Shelf doesn't know); the rest of
the file still imports. An ISBN-10 in the file stores both forms.

## Goodreads & StoryGraph

Export from Goodreads (My Books → Import and export) or StoryGraph (Manage
account → Export) and upload the file **as-is** to the same import card. The
format is auto-detected from the headers. Shelf maps:

- shelves / statuses → want-to-read, reading, read (+ dates)
- owned copies (Goodreads) / "Owned?" (StoryGraph) → owned or not owned
- to-read + not owned → wishlist, with the option above on
- everything else not owned — a book you read or are reading but don't own
  → neither: it keeps its status and dates, stays out of the Owned and
  Wishlist filters, and isn't valued
- ISBN / title / author → lookup and covers

In **Update** mode a Goodreads or StoryGraph file always sets owned and
wishlist state on the items it matches.

### Cleaning up a wishlist after a Goodreads import

Earlier versions of Shelf put every book a Goodreads or StoryGraph export
listed as not owned on the wishlist, including the ones you had already read.
To take the read ones off in one go:

1. **Browse → Owned: Wishlist**.
2. **Reading status: Read**.
3. **Select**, then **Select All**.
4. **Wishlist… → Remove from wishlist → Apply**.

**Select All** picks the items loaded on the page, so on a long list repeat
steps 3–4 until the filter is empty. The books stay in your catalogue with
their reading history, now neither owned nor wishlisted. Nothing does this
automatically: a book you read and then want to buy belongs on the wishlist,
so the choice is yours.

Ratings are **not** imported yet (Shelf has no ratings; that's on the
roadmap) and the import summary says so. LibraryThing and Libib importers
are planned.

## Portable archive

**Portable archive → Export** produces a zip of your items, tags, locations,
series, reading log, checkouts, **your physical copies**, **which items are
on your wishlist** and **the cover images**. No users, passwords, API
credentials, settings or certificates — so it's safe to hand to someone else
or keep in a shared drive. Items in [Trash](items.md#trash), and copies
removed on their own, are left out.

Three things to know about how copies come back:

- **An archive taken before 0.38.0 imports as one copy per item**, from the
  item's own location, exactly as it did then. Nothing is lost that was not
  already lost.
- **An item you already have keeps its own copies.** The archive updates the
  record's fields but does not touch your copies — reconciling two sets of
  copies would be guesswork, and it would duplicate them on every repeat
  import.
- **A copy barcode already in use is imported without the barcode**, and the
  import's errors list names both items so you can sort it out. Copy barcodes
  are unique across your whole collection.

And two about tags:

- **Each tag carries an optional media-type scope.** An archive written by an
  older Shelf has no scope on its tags, and still imports — those tags arrive
  global, which is what they were. A scope your library does not recognise is
  imported as global rather than losing the tag. A tag you already have keeps
  its own scope; the archive never overwrites it.
- **An item whose `media_type` is `kids_book`** — from Shelf 0.42.x or earlier
  — imports as a `book` carrying the `Kids` tag. If the same archive also holds
  the real `book` for that ISBN or barcode, the two become one item once the
  retired type is translated, so the kids-book record is refused and named in
  the import's errors; the `book` imports normally.

**Import** is a two-step: upload, then a **preview** shows how many items are
new, how many you already have, and how each duplicate was matched
(exactly on ISBN, or heuristically on title + author). You can uncheck parts
of the archive — leave out loans, say — before **Apply** writes anything.
Covers come from the zip, so a 2,000-item import doesn't make 2,000
requests to Open Library.

An archive from a newer Shelf than yours is refused with a clear message —
upgrade first.

A row whose ISBN isn't valid — an archive exported before Shelf stopped
storing Audiobookshelf ASINs as ISBNs will carry some — is imported
**without** its ISBN and listed in the import's report, so nothing is
silently dropped. A row with a media type or platform Shelf doesn't know is
refused and named in the same report; the rest of the archive still applies.
An item is checked in full before any of it is written, so a refused item is
skipped whole — it never leaves a half-written record, copy or location behind
while the report says it failed.

## Database backup & restore

See [Upgrading & backups](../upgrading-and-backups.md#three-kinds-of-backup).

## Hardcover

Importing *from* Hardcover and exporting *to* it live on the Hardcover card
under Integrations; see [Integrations](integrations.md#hardcover).
