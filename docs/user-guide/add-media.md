# Add media

Editors and admins can open **Add / Scan → Add media** in the navigation bar,
or visit `/add`. On smaller screens, open **Main menu** first. Search, compare
candidates, review details and save to your collection or wishlist on the same
page. Viewers and visitors gain no editing permission through this entry. It
remains available when Music is turned off.
Hiding the other Add / Scan tabs does not hide this entry. Store stays a separate
quick destination. The dropdown and link also work without JavaScript.

This is separate from searching your existing library. Opening the page, typing
or changing a choice does not contact metadata providers.

## Search, review and save

1. Choose **Add to collection** or **Add to wishlist**.
2. Choose a concrete format and search method. Enter the title or identifier,
   with the applicable artist, title hint, country or game platform if known.
3. Choose **Search or continue**. Compare the candidate's provider, format,
   country, year, publisher or label and catalogue number with your item.
4. Choose **Preview this result**, then review the actual edition or pressing.
   For music, choose which supplied catalogue fields to use; a new item needs
   Title selected. Non-music review is not a selective metadata editor.
5. Check optional location, tags and status where offered, then save. Location
   uses an existing numeric Location ID, not a location-name picker.
6. Read the **Save receipt** and check the saved item in your library. It describes
   stored state, not a promise that every provider fact replaced an existing item.

Search and preview do not add catalogue items. These are ordinary full-page forms
and also work without JavaScript. No artwork is loaded or selected in the review;
you can add artwork later from the item page.

## Choose the right input

- **Title or keyword:** search for known words rather than guessing an identifier.
- **ISBN:** use a valid ISBN-10 or 978/979 ISBN-13 for a book-family format.
- **Retail barcode:** use a check-digit-valid UPC-A or non-ISBN EAN-13. For 978/979
  book codes, choose ISBN. A non-music lookup
  supplies product facts for an explicit title search or manual entry; it is not
  an automatic save or hidden second search.
- **Catalogue number:** enter the music code as printed. Known artist/title
  refinements can help distinguish editions.
- **Manual entry:** prepare a form for known details without a provider lookup.

ISBNs and retail barcodes accept ASCII digits and ordinary spaces or hyphens
between digits; ISBN-10 may end in X. Invalid identifiers are refused before
lookup. When entered as a **Catalogue number**, a short code such as `4026156`
is not a barcode or provider release ID. Shelf does not pad it, rewrite it as
`40-26156`, make it a manual title, or infer a pressing from it.

TV Series, Digital Video and Magazine have no catalogue lookup on this page;
choose Manual entry. Bibliographic/movie results do not prove a particular
recording, digital holding or DVD/Blu-ray edition. Provider failures and missing
credentials are not evidence that no editions exist.

## Music providers

Choose Vinyl, Cassette, CD or Digital Music, then select:

- **Automatic:** MusicBrainz first. Only a successful, genuinely empty cassette
  search with both title and artist permits the existing narrow Discogs fallback.
  A catalogue-only query or provider error does not trigger that fallback.
- **MusicBrainz only:** no Discogs fallback.
- **Discogs only:** search Discogs directly across all four music formats,
  including catalogue-number-only queries, without MusicBrainz fallback. Results
  are bounded candidates, not an exhaustive catalogue or availability guarantee.

For a cassette without a barcode, try **Hits Out of Hell**, artist **Meat Loaf**,
and compare the returned pressings. This example does not identify your copy.
Check available country, label, date, catalogue number, format and track facts.
An incompatible provider format is refused rather than relabelled as a cassette.

A MusicBrainz save uses the exact selected release. A Discogs save keeps the
selected catalogue fields and exact pressing reference; its live track details
are not copied into the canonical MusicBrainz track tree or invented MB IDs.
When Music is disabled, use honest manual entry rather than provider matching.

## Manual entry and existing items

Choose Manual entry followed by Search or continue, or **Enter known details
manually** after a search/preview. Check prefills and enter the real title and
known details. Choose No identifier and leave Identifier blank when none is
known; never invent a barcode or provider identity. Repeating an identifier-free
manual save can create separate items.

Native duplicates, wishlist promotion and identifier-based restoration are
different outcomes. Music release-review saves do not promote or restore an
already catalogued release; open the existing item or restore it from Trash
separately. The receipt's Owned, Wishlisted or Neither state describes the saved
item, including when a duplicate did not adopt the submitted ownership/format.

If a save outcome is unknown, check your library before submitting again. A
normal receipt refresh is not another save POST, but there is no exactly-once
resubmission guarantee.

## Changed or expired reviews

Prepared actions retain their displayed captured intent. With JavaScript,
changing search details disables older actions; changing the text back does not
revive them. Submit Search or continue again. Without JavaScript, edits in the
search form do not change an already prepared action: submit a new search and
use its new form. Leave inapplicable refinements blank and use Automatic for
non-music formats.

Expired/stale reviews need a fresh search and preview. If sign-in or editing
permission changes, open Add media again. The optional script manages field
applicability and invalidation only: no typing lookup, offline save queue or
Add-host localStorage preferences are introduced.

## Existing tools remain

`/scan`, existing APIs and specialist Music routes remain available. Scan retains
Add, Wishlist, Lend, Return, Move, Inventory, Lookup and Quick Rate. See
[Scanning](scanning.md), [Music](music.md) and
[Wishlist and Store Mode](wishlist-and-store-mode.md) for their separate workflows.
