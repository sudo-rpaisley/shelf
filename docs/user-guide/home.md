# Home

Shelf opens on **Home**, a collection overview. It is deliberately separate from **Browse**: Home answers “what is happening in my library?”, while Browse remains the place for searching, filtering, sorting and bulk editing.

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

## Recently added

Browse up to **24** recent additions in one horizontal row, newest first. Swipe,
scroll, or use the previous/next buttons; nothing advances automatically. The
count describes items in the row, not how many fit on screen. Select the
**Recently added** heading or the last **View more** tile to open the complete
collection. The heading keeps its usual appearance and underlines on hover,
keyboard focus or activation; there is no separate View all button.

Artwork keeps its original proportions. Hover a cover, or focus its link with the
keyboard, to reveal the title and artist/creator over the bottom-left of the image.
There is no separate Details toggle or media-type caption. Open the item itself
to read complete metadata, including on touch devices.

**Item size** fits more or fewer complete items across without shrinking text.
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
current page and says that it could not save. After a cached Back/Forward restore,
refresh the page before editing the saved size. Print/export artwork, public
shares and scanning/cover-picker previews do not use this setting.

Without JavaScript, the row shows up to eight recent items. Item links, hover/focus
captions and the **Recently added** heading link still work.

Catalogue totals, checkouts, covers and local activity are read when Home opens.
Opted-in native Continue uses a separately stored account-private snapshot.
