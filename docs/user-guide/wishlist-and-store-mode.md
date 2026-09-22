# Wishlist & Store Mode

## Wishlist

The wishlist is a list of items you want. Owning and wishing are separate
things, so an item is in one of three states: **owned**, **on your
wishlist**, or **neither** — tracked in your catalogue (say, a book you read
from the library) without being owned or wanted. An owned item is never on
the wishlist.

Put an item on the wishlist by:

- **Wishlist** scan mode — in a bookstore, scan what you want.
- **Series → Check completeness → Add to wishlist** for missing volumes. A
  volume you already track but neither own nor want counts as missing here,
  and the button adds that item to the wishlist rather than a second copy of
  it.
- Ticking **On my wishlist** on an item's edit page.
- **Browse → select items → Wishlist: Add to wishlist**.
- Importing from Goodreads / StoryGraph with "Import 'to read' books as
  wishlist" on, or re-importing Shelf's own CSV export.

Take one off with the same controls: untick **On my wishlist**, or **Browse →
select → Wishlist: Remove from wishlist**. Removing an item from the wishlist
does not delete it or its reading history.

Browse → **Owned: Wishlist** shows the list, and **Owned: Not owned or
wishlisted** shows the neither items. Buying one later? Scan it in **Add**
mode: Shelf marks the existing record owned and takes it off the wishlist
(the scan card says *Now owned — was on your wishlist*) instead of
duplicating it. Scanning it in **Wishlist** mode changes nothing.

Share it as a gift list with a public link — see [Sharing](sharing.md).

## Store Mode (offline)

Standing in a shop with no signal and a stack of second-hand books, you want
one answer per barcode: *do I already have this?* Store Mode gives it
instantly, offline.

### How it works

Open **Store** in the nav (or `/store`) while online. Shelf caches your
library's ISBNs — owned and wishlist — on the device. From then on each scan
answers from that cache:

| Verdict | Meaning |
|---|---|
| **Owned** | Already on your shelf (shows where) |
| **On wishlist** | You wanted it — buy it |
| **Not in library** | New to you. It's queued on the device |

The cache holds what you own and what you want. An item you track but
neither own nor want reads **Not in library** too, and scanning it queues it
like any unknown: on the next sync Shelf adds that existing item to your
wishlist instead of creating a new one.

Queued unknowns are looked up and **added to your wishlist** (with metadata
and cover) the next time you open the page online. Nothing is lost if you
close the tab; the queue lives in the browser.

A barcode the scanner misreads is kept too. If a queued code fails its check
digit it can't be looked up, so instead of dropping it Shelf saves a wishlist
row titled **Unreadable barcode — <code>** with no ISBN, and the sync line
says how many couldn't be read. Open that row later and type the right ISBN
into **Edit**. Scanning the same bad barcode again matches the row it already
made rather than queueing a second one.

### Installing it as an app

On the store page use the browser's **Add to Home Screen**. It then launches
full-screen like a native app and works with no connection at all.

### The one requirement

Offline support uses a service worker, which browsers only run on an origin
they trust. `localhost` always qualifies; your server over LAN HTTPS does not
until you either trust Shelf's self-signed certificate on the phone or put a
real certificate in front of it. Both are covered in
[HTTPS & reverse proxy](../https-and-reverse-proxy.md). Until then Store
Mode still works — just not with the signal off.

### Keeping the cache fresh

The cache refreshes each time you open Store Mode online. Scanned a box of
books at home this morning? Open the store page once before you leave.
