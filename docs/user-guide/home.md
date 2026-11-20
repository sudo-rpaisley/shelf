# Home

Shelf opens on **Home**, a collection overview. It is deliberately separate from **Browse**: Home answers “what is happening in my library?”, while Browse remains the place for searching, filtering, sorting and bulk editing.

**Search your library** is in the top navigation bar, including on Home. Enter a
query and press **Enter** or the magnifying glass inside the field to open matching catalogue results. It
stays available when the mobile menu is closed; it does not search remote services.

Home shows:

- catalogue, owned and wishlist totals;
- items currently lent out;
- missing-cover count — items you have marked **Not available** in the
  [cover review queue](items.md#the-review-queue) are excluded, so this
  agrees with the same figure on Settings;
- the media types currently present in the collection;
- recent additions;
- your combined Continue row when Connected Library is enabled.

Use the main navigation and account menu for Library, My Activity, Add / Scan,
Series, Sources and Statistics; Home does not repeat those shortcuts
in its header or a Useful places panel.
**Explore** opens downward to Series, Stats and other available destinations;
on small screens, open **Main menu** first.
Scanning and adding items remain restricted to editors and administrators.
**Add / Scan** opens one workspace for search, full manual entry, scan actions,
Photo Intake and ordered Shelf Fill; Store remains a separate offline destination.
See [Add / Scan](add-media.md).

The collection mix by media type and location is available on
[Statistics](stats-and-valuation.md#stats), rather than repeated on Home.

## Continue

With Connected Library enabled, **Continue** combines native digital resume
entries, your In progress items and My List / Up Next in one horizontal cover row,
using the same cover sizing and hover/focus captions as Recently added.
Native snapshots come first, with their source, supplied position/completion and
timestamp. Local in-progress items precede your local plans; local duplicates
appear once. A native card with an exact Shelf edition binding replaces its
duplicate local preview, not its saved state. Different native sources and
episodes remain distinct. This is your account's activity,
not the shared wishlist or another user's plans.

Use **Connect or refresh digital Continue** to open **My source progress → Digital
Continue**. Supply your own personal credential and confirm the remote user ID;
then explicitly refresh, or separately opt into automatic cache refresh. Jellyfin,
Audiobookshelf, Komga and RomM have native adapters. OPDS and external links do not
have a standard personal Continue feed. RomM can supply recently played games
without a completion percentage; Shelf never guesses one.

Remote-only entries can appear without adding catalogue items or physical copies.
Only exact existing whole-edition bindings supply local cover artwork and a
**Shelf item** link; other entries use a placeholder, not authenticated remote
image requests. The cover opens the native app's item page, where you log in
normally; Shelf does not launch a player or send a resume position in the URL.
Episodes and tracks do not become series/album progress.

Home reads a cache, never contacts remote servers, and never changes your manual
activity/history. Notices show failures, old snapshots, disabled/changed sources
and bounded results. Each connection retains up to 24 entries from up to 8
selected libraries; Home previews up to 12 native entries. An empty or failed
native feed does not reset local progress. See [Digital Continue](connected-library.md#digital-continue).

Swipe, scroll or use the row's previous/next buttons. **My Activity** in the main
navigation and the last **View more** tile open the full Activity page, where
Digital Continue, manual Continue and Up Next remain separate. An empty row shows one explanation instead
of two empty panels.
All cards and links remain usable without JavaScript.
The repeated “Swipe or scroll to explore” hint is omitted from both Home rows;
scrolling and the previous/next controls remain available.

## Recently added

Browse up to **24** recent additions in one horizontal row, newest first. Swipe,
scroll, or use the previous/next buttons; nothing advances automatically. The
heading has no item-count or “Across your collection” subtitle. Select the
**Recently added** heading or the last **View more** tile to open the complete
collection. The heading keeps its usual appearance and underlines on hover,
keyboard focus or activation; there is no separate View all button.

Artwork keeps its original proportions. Hover a local catalogue cover, or focus
one of its links, to reveal the title and artist/creator over the bottom-left of
the image. The **artist/creator** has its own link to that indexed creator's local
collection; an available **series name** opens that particular series, not the
whole Series index. The cover/title still opens the item. Captions remain visible
while tabbing through their links and are shown directly on touch/coarse-pointer
screens. Multiple creators are compressed to the first linked name and **…**;
long names use a one-line ellipsis instead of a scrollbar. The tooltip and item
page retain the complete credit list. Cover dimensions do not change.
There is no separate Details toggle or media-type caption. Native remote-only
Continue labels are not assumed to identify local creators. See
[Follow an artist, author or series](browse-and-search.md#follow-an-artist-author-or-series).

The slider between the **small and large cover icons** fits more or fewer complete
items across without shrinking text. The icons indicate direction, not extra buttons.
The **Item size** label and percentage are available to screen readers rather than
displayed beside the slider.
The default **100%** keeps the normal cover size and sits at the slider's midpoint;
the ends are **50%** and **200%**. Move the slider back to the center for 100%.
The slider sits on the same line as the **Home** heading at the right, with the
description below. On narrow screens it wraps beneath the heading rather than
squeezing the controls. It is not inside Recently added. **Commands** is the
small round ⌘ button just above **?** at the bottom left. Other catalogue
pages, including Browse, Library, Music, Series and My
Activity, use the same framed cover and hover/focus caption, not just the same
width. Library tiles show only that cover; open an item for metadata, holdings
and source actions. Other pages retain their own progress, selection badges and
controls. Narrow screens constrain large covers consistently; artwork keeps its
original proportions.

Your choice is saved for your account in this browser and shared across pages,
reloads and other open tabs. Another account has its own choice; it does not sync
between devices. If browser storage is blocked, the control still works for the
current page and says that it could not save. **Back/Forward needs no refresh**:
sizing works immediately on the page while a read-only sign-in check restores
saving for the original account. If the sign-in changed or the check fails,
changes remain page-only; **Retry saving** checks again without reloading. Any
choice made before confirmation stays on the page, not automatically in saved
storage; move the slider again after confirmation to save the next change.
Print/export artwork, public
shares and scanning/cover-picker previews do not use this setting.

Without JavaScript, the row shows up to eight recent items. Item links, hover/focus
captions and the **Recently added** heading link still work.

Recently added omits Komga-only imports confirmed missing by a complete sync.
Their records and personal Continue/Activity/history remain; stored-catalogue
totals and operational review counts still include retained records. Partial scans
do not create new absence verdicts. See [missing Komga books](../komga.md#books-that-disappear-or-acquire-a-different-id).

Catalogue totals, checkouts, covers and local activity are read when Home opens.
Opted-in native Continue uses a separately stored account-private snapshot.
