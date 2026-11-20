# Browse & search

Open the **Library** menu and choose **Library** for the mixed catalogue, or
choose a family/format from the same menu. There is no separate Manage
dropdown, or duplicate Browse button when Library is enabled and visible. The
original Browse page remains available directly at `/browse`, with the grid/list,
filters, sorting and bulk tools described below; it remembers those choices
between visits. If Library is disabled or hidden,
the standalone **Browse** link remains as the core-catalogue fallback.
See [Connected library](connected-library.md) for mixed-format Library views.

Select a media-family **name**—Books, Comics, Music, Films, TV, Games or
Periodicals—to open all items in that family. Its separate **arrow** opens the
format submenu, where you can choose a specific format. The duplicate **All
items** submenu entry is gone. Mouse hover still reveals formats; keyboard and
touch users can activate the arrow independently. Native Music/Periodicals
overview links remain in their submenus when available.

**Search your library** is in the top navigation, outside the mobile menu—not
inside the Library filter panel. Use **Enter** or the magnifying glass inside the
field (named **Search** for screen readers). On Library it submits
the same filters, view and saved-view selection as **Apply filters**, including
the current search text; no JavaScript is required. From another page it opens
the eligible mixed-catalogue destination, using Browse when Library is hidden or
disabled. The original Browse page's local filters and bulk tools remain separate.

**Explore** opens a downward dropdown of its available destinations, such as
Series and Stats. Its links are stacked below the label, not alongside it.
On a small screen, open **Main menu** first.
**Add / Scan** is one workspace link, not separate Add, Scan, Intake and Shelf Fill
navigation entries. Its manual form and advanced options are described in
[Add / Scan](add-media.md); offline Store remains separate.

## Commands

Use the small round **Commands** (⌘) button just above **?** at the bottom left,
or **Ctrl+K** / **⌘K**, to find a destination from your available navigation or
search the catalogue. Type up to 200 characters,
use the arrow keys to choose, then Enter to follow the link. Escape closes the
palette and returns focus. The shortcut leaves typing, composition and other open
dialogs alone.

Typing does not contact a provider or run a catalogue query. Searching follows
the same mixed Library destination as your navigation, with the core Browse
fallback when Library is unavailable. The palette offers no edit, delete, sync or
external-media launch commands, and no command history is saved. Destination
pages retain their normal behaviour. Without JavaScript or native
dialog support, use the normal navigation; the Commands disclosure explains this.
Without native dialog support, use **Library → Library** (open **Main menu** first
on a small screen). When Library is unavailable, the Browse fallback stays directly
visible. Menus are not opened automatically. The **B** shortcut uses the same
Library-or-Browse destination.
Commands can also open the available **Add / Scan** workspace. This is navigation
only; it never submits a search, opens a camera or performs a scan action.

## Views

Catalogue covers reuse Recently added's rounded frame, proportional artwork and
bottom-left title/creator caption on hover or keyboard link focus. Library tiles
are cover-only, without an outer panel, format/creator rows, IDs, badges or holding
boxes. Open an item for its complete metadata, copies and source actions. Browse
keeps selection, wishlist and lending badges. Detail/list screens retain their own
metadata and controls.

### Follow an artist, author or series

On the main catalogue covers, select the **artist/creator name** to see items
associated with that creator in the existing local index. For example, **Meat
Loaf** opens your Meat Loaf releases, not every title mentioning his name and
not a MusicBrainz/Discogs search. Covers show the first linked creator and **…**
when there are more, rather than a scrolling list. Open the item for all credits;
the full raw list also remains in the tooltip. A stale or unindexed creator link
reports not found rather than opening an unrelated collection or repairing data.

The **cover or title** still opens its original item. A populated **series name**
opens just that local series, using the existing series identity, with **All
series** available to return to the full index. Series links stay plain when the
Series feature is off. Existing format and language labels in Browse list/detail
views link to their exact filters; unsupported metadata is not given a guessed
destination.

Use **Tab** to move from the cover to its title, creators and series. The caption
stays open while one of its links has focus. Touch/coarse-pointer screens show
interactive captions without requiring hover; long names are shortened visually
with an ellipsis inside the unchanged cover frame. Name links are native links, so normal new-tab and
modified-click actions work. In Browse **Select** mode, clicking a name still
selects that item instead of navigating. The selection button and lending/source
actions remain separate.

This applies to local Home/Library/Music shared cards, Browse grid and list
metadata, Activity, Series item covers and catalogue/music details. Operational,
public/print/export previews and untyped remote-only Continue captions are not
turned into guessed creator identities. Navigation reads local data only and
never edits items, account activity, progress, source bindings or the creator
index. The new interaction/rendering regressions are authored but not executed
for this source-only checkpoint.

The **small/large cover icons and slider** are at the top right; on Home they share
the **Home** heading row and wrap below the heading when space is limited. The
icons are direction cues, not buttons; **Item size** and its current percentage
remain available to screen readers without visible text. One native
slider controls catalogue cover widths across Home, Browse (grid and list), all
Library views, local Music, Series, My Activity and catalogue detail/list pages.
Cards use a common base width, not different percentage bases on each page;
smaller covers fit more across and large covers have a shared narrow-screen cap.
The default is 100% at the slider's center, with 50% and 200% at its ends. Text
sizes, artwork proportions, item order and results per page do not change.

The choice is saved for your account in this browser, survives navigation/reload
and updates other open tabs. It is not a server-side or cross-device preference.
Other accounts and mounted Shelf instances use separate numeric storage keys.
Blocked browser storage leaves current-page sizing usable with an explanation.
Back/Forward works without refreshing: the control checks the current sign-in
before resuming saved sizing. You can still resize the page during that check.
If the sign-in changed or cannot be confirmed, changes stay on this page; use
**Retry saving** to check again. Edits made before confirmation are not saved
retroactively—move the slider again after confirmation to save your next choice.
JavaScript is needed to change size; without it the common 100% layout is usable.
Print/export artwork, public shares and provider/scanning/editor previews stay
separate from this catalogue presentation setting.

On phones, Library uses the same cover-only tiles and hover/keyboard captions.
**About this Library** holds explanations without hiding search, presets or live
controls. **Filters and display** keeps its stable caption and a separate count of
active filter/display options; that count is not the number of matching items.

On an item's physical-copy list, **Find this copy** opens the exact copy's saved
location and a small window of catalogue neighbours. **Selected copy** identifies
the target. This is recorded information, not confirmation that the copy is still
there. Unknown position or location stays unknown; an old link shows the current
record and warns when its location hint has changed. Use **Open full location in
Arrange** for the complete shelf—this limited window is not an editable order.
Native links and return anchors also work without JavaScript.

- **Grid** — cover tiles, best on phones and for "what do I have?"
- **List** — a table with a **Columns** picker (see below).

### Choosing columns (List view)

List view's toolbar has a **Columns** button. It opens a checklist of columns
plus a **Reset to defaults** button.

With a single type picked in the **Type** filter, the **Author** column's
header uses that type's word — **Artist** for music, **Developer** for video
games, **Director** for DVDs — and reads **Author** otherwise. The checklist
itself always calls it **Author**.

The selection checkbox, cover thumbnail, and **Title** are always shown and
aren't offered in the picker — Title is the row's link to the item, so a row
without it would go nowhere.

| Column | Default |
|---|---|
| Author | On |
| Type | On |
| Location | On |
| Status | On |
| Value | Off |
| Series | Off |
| Publisher | Off |
| Year | Off |
| Pages | Off |
| Language | Off |
| Added | Off |
| Platform | Off |
| ISBN/UPC | Off |

**Value** shows your [manual value](stats-and-valuation.md) where you've set
one, otherwise the ISBNdb estimate, in your display currency.

Your choice is **remembered per browser, not per account** — a phone and a
desktop often want different columns, so a new browser or device starts back
at the defaults above. Turn on more columns than fit and the table scrolls
sideways within itself rather than widening the page. **Reset to defaults**
puts it back to Author, Type, Location, Status.

Your selection applies at **every screen width**. Earlier releases hid some
columns automatically on narrow screens — Author, Type, Location and Status
each vanished below a width of their own — which is why the list looks busier
on a phone than it used to. A column that hid itself could not also be one you
switched on, so the picker replaces that behaviour rather than working around
it. On a phone, untick what you do not need there; because the choice is
per-browser, it will not follow you back to the desktop.

## Filters

Filter chips along the top, all combinable:

| Filter | Values |
|---|---|
| **Search** | Free text over title, author, ISBN, series, publisher |
| **Type** | Book, audiobook, eBook, DVD / Blu-ray, CD, comic, Manga, video game |
| **Location** | Any location, or "no location" |
| **Status** | Follows the Type filter: Want to read / Reading / Read for books, Want to watch / Watching / Watched for DVDs, Want to play / Playing / Played for video games. With no type chosen it shows Want to / In progress / Finished, and one Finished choice spans every type |
| **Owned** | All / Owned / Wishlist / Not owned or wishlisted |
| **Lent out** | Items currently checked out |
| **Tag** | Any custom tag |
| **Author** | One author, set by clicking their name on an [item page](items.md#whats-on-the-page) |
| **Language** | Edition language (captured on lookup) |
| **Source** | How the item arrived — a sync (RomM, Komga, Audiobookshelf), a metadata provider, a CSV import, Photo Intake, or Manual. Only sources your library actually contains are listed |

The **Author** filter has no dropdown — a library can hold hundreds of names.
You set it from an item page: each author there is a link, and clicking one
opens Browse showing everything by that person. Its chip names the author, and
takes the same word the Type filter gives the author column (**Artist** for
music, and so on). It combines with every other filter, so "this author's
books on the living-room shelf" is two clicks. It matches one person, where
**Search** matches text: searching `Ken Liu` still finds every item whose
author text contains those words.

Counts next to each value update as you narrow down, and they tell you what
you would get if you picked that value — counted against your other active
filters, but not against the filter the count sits under. That last part is
why "All Types" can show a bigger number than the grid below it: with a type
filter applied, "All Types" is telling you how many items you would see if you
cleared it. The same is true of every filter's "All" entry.

Filters persist across tabs and page reloads, and the URL carries them, so a
filtered view is bookmarkable — and a bookmarked filtered view shows the same
counts it will show after you touch a filter.

If a bookmark stops naming anything real — a location you have since deleted,
or a link that arrived truncated or hand-edited — Browse shows no items rather
than an error, and leaves the filter listed so you can clear it.

## Sorting

Title, author, date added, publish year, value — ascending or descending.
Like the column header, the author sort option takes the picked type's word:
with **Type** set to a music format it reads **Artist**, and so on.
Sort by "date added, newest first" is the quickest way to check a scanning
session.

## Tags

Tags are free-form labels you invent: `signed`, `first-edition`, `book-club`,
`to-sell`. Add them as chips on the item page or its edit page, to many items
at once from the [bulk bar](#bulk-editing), or set
[default tags](scanning.md#default-tags) while scanning so each new item
arrives tagged; filter by them here. An admin renames, scopes and deletes
them in [Settings](settings.md#tags). Tags are yours alone — they aren't
synced anywhere.

**Tags column.** In list view, the column picker offers an optional **Tags**
column, off by default. It shows each item's tags as chips; tapping one
filters Browse by that tag.

## Bulk editing

Tick the checkbox on items (or **Select all** for the current filter), and a
bar appears with actions:

- **Move** to a location
- **Change type**
- **Set status** — Want to / In progress / Finished, or clear it
- **Add to / remove from wishlist**
- **Set series** (or clear it)
- **Set language** (or clear it) — pick from the same list as the item edit
  page
- **Add tag** / **Remove tag** — type a tag name or pick one, then choose the
  button. Removing a tag from the last item that carries it deletes the tag.
- **Delete Selected** — moves the items to Trash

Anyone can use **Select**; what happens next depends on your role. Move,
Change type, Set status, the wishlist action, Set series and Set language
are **admin** only — and only an admin sees Set language at all. Add tag and Remove tag are for **editors and admins** — a
viewer does not see them. Delete Selected moves items to Trash and needs an
**editor** or admin. Bulk actions are immediate — filter carefully first.
Only Delete is undoable (restore from Trash); a tag you added by mistake is
removed the same way it was added. A bulk action whose target no longer exists — a location
deleted from another tab while the bar was open, say — is refused with a
message and changes nothing.

## Selecting across pages

Browse paginates (60 per page). "Select all" selects the current page; narrow
the filter to operate on a whole set.

## Search tips

- Search is substring, case-insensitive: `tolk` finds Tolkien.
- Type an ISBN to jump straight to that item.
- Series filter lives on the [Series](series.md) page rather than here.
