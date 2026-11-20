# Komga integration

Shelf can catalogue digital comics and manga that are available in a Komga server while keeping that service availability separate from physical ownership and location.

## Configure

Open **Settings → Integrations → Komga** and provide:

- **Komga URL** — the URL Shelf itself can reach, such as a Docker/LAN address.
- **Browser URL** — optional; use the reverse-proxy/public address if it differs from the server URL.
- **API key** — stored encrypted at rest. Leaving the field blank keeps an existing key.

Use **Test Connection** before loading libraries.

## Libraries

Choose **Manage Libraries** after saving a working connection. Every Komga library can be:

- included or excluded from Shelf; and
- classified explicitly as **Comics** or **Manga**.

Shelf suggests Manga only when the library name clearly contains “manga”; the saved user choice always wins.

## Identity and physical copies

Komga is treated as a digital holding, not as a physical location.

Shelf uses the stable Komga book ID as the integration identity. On first import it may attach the Komga holding to an existing item only when there is an exact canonical ISBN match of the same content type. It never adopts an existing item from title/series similarity alone.

Consequently:

- a physical comic/manga and a Komga copy can share one catalogue item when a strong identifier proves the edition;
- ambiguous editions remain separate instead of being silently merged;
- Komga sync never assigns a room/shelf location or creates a physical copy.

## Sync and covers

**Sync Now** reads every included library with bounded pagination, persists new/changed holdings, and reports progress in Settings.

## Personal Continue Reading

In **My source progress → Digital Continue**, separately link your own Komga API
key and confirm its authenticated user ID. Refresh reads that user's **In
progress** books with native page/total counts, newest read-progress modification
first; it does not treat **On Deck** (next unread book) as currently reading.
Excluded libraries stay excluded. Native snapshots appear on Home/My Activity
without replacing manual activity or creating physical copies. Automatic cache
refresh requires separate consent; the catalogue API key is never substituted.
See [Digital Continue](user-guide/connected-library.md#digital-continue) for cache,
privacy, limits and the current unexecuted qualification status.

If an imported item has no cover, Shelf requests the book thumbnail from the configured Komga server and stores it using Shelf’s normal local cover handling.

## Open in Komga

Items with a Komga holding get a compact **Open in Komga** action on the main item card. The action uses the optional Browser URL when configured, so Docker-internal addresses do not leak into browser links.

## Series names

Komga can report a series as `Name (N)` — one series per volume — when its *append volume to series title* option is on. Shelf groups by series name, so it strips a trailing volume number on sync and files `One Piece (21)` under **One Piece**. A four-digit year is kept, because `Batman (2016)` names a distinct run. Rows that were already split are repaired by the next Komga sync; a series name you edited by hand on a non-Komga item is never touched.
