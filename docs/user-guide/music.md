# Music

Shelf can catalogue exact music releases rather than treating a title as one undifferentiated record.

Supported music formats are **Vinyl**, **Cassette**, **CD** and **Digital Music**. CD reuses Shelf's existing CD media type. There is deliberately no catch-all "other" format: when a provider reports a medium Shelf does not recognise, the add screen asks you to choose the format rather than guessing.

## Add a release

Open **Music** from the navigation bar. Shelf searches MusicBrainz first using:

- release title;
- artist;
- UPC/EAN barcode;
- catalogue number;
- a format filter (Vinyl, CD, Cassette or Digital Music) and optional two-letter
  country code, such as `GB` or `US`.

Barcode searches take priority over catalogue numbers and titles. Shelf tries
the equivalent UPC/EAN padding where applicable, then a catalogue-number or
title/artist fallback only if the identifier searches return no results. An
unreachable or rate-limited provider is reported rather than treated as an
empty search. **Matching facts** and **Check differences** explain the available
evidence; they are not a guarantee that two pressings are identical.

MusicBrainz searches use exact **releases**, not only release groups, so different pressings can retain their own country, date, label, catalogue number, packaging and medium format.

Use **Preview details and artwork** to inspect the available release details and track
list, choose artwork, and confirm which catalogue fields to use before adding.
Confirm the Shelf format; new items default to owned, while unticking **I own
this item** adds to the wishlist. MusicBrainz results also offer a quick **Add
release** action with an optional location. Locations and physical-copy details
can also be edited from **Catalogue item** afterwards.

### Add a cassette without a barcode

1. Set **Format** to **Cassette**, enter a **Release title** and **Artist**, and
   leave **Barcode** blank. For example, search for `Hits Out of Hell` by
   `Meat Loaf` using **Search MusicBrainz and cassette catalogue**.
2. MusicBrainz is searched first. Only a successful search with no cassette
   matches permits a public Discogs search by title, artist and cassette format.
   This is one page of at most 20 candidates, not an exhaustive catalogue search.
   No API key, provider account or Discogs token is needed for this public path.
3. Results show their provider, exact release ID and source link. Use **Preview
   details and artwork** on a possible pressing and compare its country, year,
   labels, catalogue number and track order with your cassette and sleeve.
   A matching album title alone does not identify your copy.
4. Check only the catalogue fields you want, optionally choose artwork, confirm
   **Cassette**, then choose **Add release**. A Discogs candidate saves those
   checked fields, selected artwork and its exact Discogs Release ID, not a
   MusicBrainz identity or a stored Discogs track list.

Leaving the barcode blank omits that search filter; it does not restrict results
to releases without barcodes. Do not relabel a CD result as a cassette. Search
and preview do not create catalogue items or physical copies. Provider rejection,
rate limits, unreachable services and unreadable responses are errors, not empty
matches, and do not trigger the fallback. Requests are paced, so allow time for
the lookup; retry later if a provider cannot complete it.

### When the pressing is unknown

If none of the candidates identifies your copy, expand **Add by hand — pressing
unknown**. The form keeps your searched title, artist and format; correct these
and choose **Add by hand**. It saves a manual item without a provider release ID,
barcode or track list rather than guessing a pressing. On Scan, the direct manual
form retains the selected Add or Wishlist mode.

The alternative manual-entry link opens the full Scan form at
`/scan?add=manual`. Choose **Cassette** and enter the title and artist there;
leave **ISBN or barcode (optional)** blank if unknown. Without a location,
neither manual path creates a physical-copy record. Add copy details later from
**Catalogue item**.

## Match an existing item

Open an existing vinyl, CD, cassette or digital-music item and follow **Music
details and matching**. Expand **Match or fix release metadata**, search using
its details, then preview a candidate. Existing non-empty catalogue fields and
artwork are kept by default; tick only the fields you want to replace.

For a MusicBrainz candidate, **Apply selected match** attaches the exact release
and replaces its provider-owned release details and track tree. For a public
Discogs candidate, it saves the pressing reference and changes only checked
catalogue fields and selected artwork, keeping any existing MusicBrainz release
and track tree.

Neither changes the item's Shelf format, copies, ownership, locations, collector
notes or private activity. A known contradictory format is refused rather than
attaching a CD pressing to a cassette. Matching does not merge catalogue items.
If a barcode or exact release is already catalogued, the operation is refused
without partial writes. If the selected release is in Trash, restore it instead
of adding another.

Reviews expire after 15 minutes. If the item or release changes while you are
reviewing or downloading artwork, Shelf refuses the stale review; preview it
again rather than overwriting the newer edit.

## Use a shop or release link

Paste a public HTTPS product URL into **Match a release or use a shop link**,
then choose **Preview link**. This works from Music for a new item and from an
existing item's matching panel. Exact MusicBrainz release URLs are supported.
Pasted Discogs release URLs still require a configured token and an existing
item; this is separate from the public cassette candidate previews above.
Artist, release-group, Discogs master and marketplace-listing links are not
exact release identities.

Shop previews read published JSON-LD Product/MusicAlbum/MusicRelease metadata,
with an OpenGraph fallback. Available fields, images and structured tracks are
shown for review; separate published variants are not silently combined.
Details absent from the page cannot be filled in reliably. Shop titles often
include merchandising text and shop metadata is **not proof of a pressing**:
check the sleeve, barcode, catalogue number and format yourself.

Applying a shop preview keeps an existing MusicBrainz release and track list,
changes only checked catalogue fields and selected artwork, and saves a
**Reviewed shop source** link. For items with no stored release, or already
using shop metadata, available shop release details and tracks can be stored
separately as shop metadata. They do not acquire a MusicBrainz identity
automatically.

Shelf does not run shop scripts, play/download audio, use account cookies or
bypass login/anti-bot pages. Fetches are time- and size-bounded, public-HTTPS
only, and revalidate/pin public DNS for each redirect. Tracking parameters are
removed; product/variant selectors are retained, but credential-bearing URLs
and unsupported query parameters are refused. Previews of shop images are
served through Shelf, not loaded directly by your browser from the shop.
If a shop is unreadable or publishes no useful metadata, use manual editing,
an image URL or an upload instead. Compatibility varies by shop and page.

## Track lists and multi-disc releases

For MusicBrainz releases, the Music detail page keeps the medium hierarchy. Multi-disc releases remain separated by medium and vinyl-style track positions such as `A1`, `A2`, `B1` are stored as text instead of being forced into simple integers.

Each track can retain its MusicBrainz recording identity, artist credit and duration.

Discogs track lists are shown in previews and **Load fresh details**, not saved
into this MusicBrainz hierarchy. Manual unknown-pressing items have no track list.

## Artwork

After adding an exact MusicBrainz release, Shelf asks Cover Art Archive for its scans and
prefers a front image. If a front is unavailable, Release Group artwork may be
offered, explicitly labelled **another edition**. Existing Shelf artwork is
not automatically overwritten, including a cover chosen while an automatic
download was running.

Editors can use **Choose artwork** on a Music item, even when it already has
a cover. A larger, responsive gallery shows the current image and alternative
front/back/other scans. Choose a candidate, search again, paste an image URL or
upload your own. Close the window or press Escape to cancel; closing without
choosing changes nothing. Choosing artwork is independent of release matching:
it never changes the pressing, track list or other catalogue fields. Music
artwork selections use fresh local image URLs so a previous cached cover does
not obscure the new choice.

The generic item's **Find cover** opens the same large chooser for other media.
Its sources remain media-specific: book editions, TMDb film posters, IGDB game
artwork, or manual URL/upload where a provider is unavailable. Music no longer
falls through to book-cover searches.

Music artwork is shown square and uncropped wherever Shelf shows a cover: Browse, Home, share pages, the list view and other thumbnails, the cover review queue, the cover picker, scan cards, and the Music item page, which shows the cover in its header. Books, games and films keep their taller portrait box. In a Browse grid showing all types, a record sits beside taller book covers, so a row can be uneven; pick a music type in the **Type** filter and the grid is uniform.

The creator field is labelled **Artist** for music when you add or edit a release, in Photo Intake, and in Browse's list view when a music type is picked in the **Type** filter.

## Alternate formats

MusicBrainz Release Group identity is stored separately from the exact release identity. If two catalogued releases share a Release Group, Shelf links them using its existing `format` item relationship while preserving each exact release as its own catalogue item.

## Refresh metadata

Use **Review latest metadata** on a MusicBrainz release or a Discogs-only item
with a selected pressing. It opens the same selective preview rather than
immediately overwriting your catalogue fields. Check the release/track details
and choose which fields and artwork to apply. Discogs tracks remain live details,
not a stored track list. The legacy MusicBrainz refresh API still exists for
existing clients; the Music page uses review.

## Physical copies

Release metadata does not store condition, acquisition details or provenance.
Those belong to Shelf's physical-copy model. Use **Catalogue item** to manage
multiple copies of the same pressing without duplicating the release identity.
Adding a release without a location does not create a physical-copy record.

## Discogs

Discogs supplies public cassette candidates when MusicBrainz has no matching
cassette release, as well as optional exact-pressing enrichment for existing
Music items. A Discogs-only add saves checked generic catalogue fields, any
explicitly chosen artwork and the selected Discogs Release ID. It does not
assign a MusicBrainz release or Release Group identity or permanently copy the
Discogs response into canonical release/track tables.

Editors can use **Load fresh details** for any saved Discogs pressing reference,
including one on a MusicBrainz-backed or manual item. This explicit public
action needs no token and shows fresh pressing details, notes, identifiers and
track order with attribution. It does not change saved catalogue fields or an
existing MusicBrainz release and track list. Public requests can still fail or
be rate-limited. If a configured token is rejected, Shelf reports the failure
rather than silently retrying anonymously.

The separate **Search Discogs** → **Use this pressing** panel workflow still
requires a personal access token under **Settings → Integrations → Discogs**.
Search concrete releases by title, artist, barcode or catalogue number.
**Use this pressing** validates the concrete release and remembers only its
Release ID. Choosing or clearing a reference does not replace the MusicBrainz
release or change catalogue fields.

**Preview details and artwork** can show a pressing's available images. Public
candidate reviews can apply checked catalogue fields; a pasted exact Discogs
link uses the token-based enrichment path and saves only the pressing reference
and artwork you explicitly choose. Neither replaces existing MusicBrainz
metadata. Artwork from the selected pressing is also available in **Choose
artwork** when the Discogs token is configured. Condition, acquisition details
and provenance remain copy-specific.

## Back up music details

Keep a full database backup and the cover-image files for a complete restore.
The portable collection archive carries generic catalogue fields and artwork,
but not the music-specific release/track tables or Discogs/shop references.
