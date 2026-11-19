# Add / Scan

Editors and admins can open **Add / Scan** in the navigation bar,
or visit `/add`. On smaller screens, open **Main menu** first. Search, compare
candidates, review details and save to your collection or wishlist on the same
page. Viewers and visitors gain no editing permission through this entry. It
remains available when Music is turned off.
One workspace contains **Search / Manual**, **Scan actions**, **Photo Intake** and
**Shelf Fill**. Only the selected tool is opened; ordinary Add does not start a
scan session, recognition or a camera. Optional tools retain their existing
feature, configuration and navigation-visibility rules. Hiding their old tabs
does not hide core manual Add. **Store** stays separate for offline use. The
workspace links and search/manual forms also work without JavaScript.

This is separate from searching your existing library. Opening the page, typing
or changing a choice does not contact metadata providers.

## Search, review and save

1. Choose **Add to collection** or **Add to wishlist**.
2. Choose a concrete format and search method. Enter the title or identifier,
   with the applicable artist, title hint, country or game platform under
   **Advanced search options** if known. Closing the dropdown retains its values.
3. Choose **Search or continue**. Compare the candidate's provider, format,
   country, year, publisher or label and catalogue number with your item.
4. Choose **Preview this result**, then review the actual edition or pressing.
   For music, choose which supplied catalogue fields to use; a new item needs
   Title selected. Non-music review is not a selective metadata editor.
5. Check optional location, tags and status where offered, then save. Choose an
   existing location from the picker; it is checked again when saving.
6. Read the **Save receipt** and check the saved item in your library. It describes
   stored state, not a promise that every provider fact replaced an existing item.

Search and preview do not add catalogue items. These are ordinary full-page forms
and also work without JavaScript. No artwork is loaded or selected in the review;
manual entry can attach a cover explicitly, or you can add artwork later.

### Barcode camera next to search

Choose the barcode button at the end of the search field to open the camera.
It fills a retail barcode and the corresponding ISBN/barcode method for review,
then stops. It does **not** search, add, lend or move anything. Choose a concrete
format and submit **Search or continue** yourself. An ISBN does not convert a
selected music or screen format into a book. Copy labels and operational scans
belong under **Scan actions**, not this lookup button. Camera access requires
HTTPS and permission; typing the identifier remains available if it fails.

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

Use **Add by hand instead** or the manual form below search immediately; no
preparation search is necessary. Choose Add/Wishlist, a concrete format and a
title. Creator and cover are optional. You can also choose Manual entry followed
by Search or continue, or use the form after a search/preview; that form retains
its displayed captured intent.

**Advanced options** exposes publisher/label, year, subtitle, description, notes,
ISBN and a separate UPC/EAN, series/position, pages, language, game platform,
audiobook narrator/duration/ASIN, value, location, tags, status and progress dates.
Both identifiers are checked independently. Leave unknown identifiers blank;
never invent a barcode or provider identity. Closed advanced controls keep their
values. With JavaScript, fields for other formats are disabled without clearing
them; without JavaScript, leave those inapplicable fields blank.

The optional **Copy** picker finds an existing item after an explicit **Find**
and copies only its existing template subset: creator, publisher, year, format
where editable, platform, series and location. It does not copy title, identifiers,
artwork, notes, value, provider IDs, personal activity or history.

**Take photo** next to Cover opens mobile capture or a desktop viewfinder. A
selected/captured image previews locally and is uploaded only on **Save manual
item**; it does not run recognition. Ordinary file selection works without the
camera or JavaScript. JPEG, PNG, GIF and WebP are accepted up to 10 MiB. An
alternative public HTTPS image URL is under Advanced options; use a file or a
URL, not both. The URL is downloaded only on explicit Save with public-address
and redirect checks. A cover failure can still leave a successfully saved item:
read the warning receipt rather than submitting it again.

Repeating an identifier-free manual save can create separate items. Duplicates
and wishlist promotions keep their stored metadata/artwork; restoring by an
identifier keeps stored metadata, copies and history. An explicitly supplied
cover may replace restored artwork, as with the existing manual flow.

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
permission changes, open Add / Scan again. Optional scripts manage field
applicability, invalidation, explicitly requested copy lookup and local capture;
there is no typing lookup, offline save queue or new Add-host preference storage.

## Scan actions, Photo Intake and Shelf Fill

**Scan actions** keeps all eight actions in its action dropdown: Add, Wishlist,
Lend, Return, Move, Inventory, Lookup and Quick Rate. Unlike the search-field
camera, this is the continuous operational scanner: scans perform the selected
action. Check that action, borrower and location first. Copy labels, audit
sessions, legacy barcode choices, default tags/status and recent results remain.

**Photo Intake** keeps explicit Read Photo, resolution/tiling/cost choices, review
and final confirmation. Taking a cover photo is not this recognition workflow.
**Shelf Fill** keeps physical shelf selection and ordered positions. See
[Scanning](scanning.md), [Photo Intake](photo-intake.md), [Shelf Fill](shelf-fill.md)
and [Music](music.md). `/scan`, `/intake`, `/shelf-fill`, their deep links and APIs
remain compatible views of the same workspace. Store stays outside it; see
[Wishlist and Store Mode](wishlist-and-store-mode.md).
