# Users, libraries & roles

Shelf separates site administration, library authority and personal activity.
See the [integration status](../upstream-integration.md) for pending independent
review and runtime validation.

## Site roles and library membership

| Authority | Can |
|---|---|
| **Global Admin** | Manage settings, accounts, integrations, backups and logs; access libraries; permanently purge / empty expired Trash; manage global administrative features |
| **Library Editor** | Read and change catalogue items in that library: add/edit, scan, manage covers/copies, lend/return, apply tags, delete to Trash and restore, subject to feature gates |
| **Library Viewer** | Browse/search accessible items, read enabled feature pages and manage their own personal activity; no shared catalogue writes |
| **No membership** | No access to that library's catalogue, except for a global administrator |

For a non-admin, **library membership is authoritative**, not the site's old
Editor/Viewer label. A global Viewer can be an Editor of one library; a global
Editor with only Viewer membership cannot edit that library. A move between
libraries needs authority over both source and destination. Bulk operations,
copies, restore, imports and exports do not bypass this boundary.

Libraries are not Collections, locations or external connector libraries.
Collections organize items, precise locations describe physical placement, and
provider-library IDs identify remote holdings; none grants catalogue access.
Missing, unmapped and inaccessible items are not disclosed as collision details.

**Trash** is the normal deletion lifecycle. Restoring needs Editor authority in
the parent library. Only a global admin can permanently purge or empty expired
rows. No timer permanently deletes them. The last live copy of an owned physical
item cannot be removed through ordinary copy controls.

## Managing users

The first setup account is a global admin; at least one must remain.
Settings → **Users** lets admins add accounts, set site roles, reset passwords
and delete users. Manage library memberships separately. Deleting an account
does not delete shared catalogue items; it removes that user's account-bound
personal state and API tokens. Admins can revoke tokens from the Users tab to
stop a leaked token without its owner.

Each user changes their own display name/password from the account menu, whose
trigger identifies the signed-in account. Administrative controls remain
admin-only even when someone has Editor membership in a library.

## Shared catalogue vs personal state

Shared data includes title/edition metadata, physical and digital holdings,
copies, lending, tags, Collections and series membership. Writes require the
appropriate library authority.

Personal data belongs to the signed-in account:

- reading / watching / listening / playing status, start/finish dates and history;
- progress, optional total and unit;
- rating, favourites and private notes;
- personal wishlist, Want-to / My List and Continue / Up Next activity.

A library Viewer can update these personal values without gaining catalogue
write permission. One user's activity never replaces another's. The personal
**wishlist** can include an item already owned by the household. The **shared
acquisition wishlist** is a different list (`wishlisted`); it excludes owned
items. See [Wishlist & Store Mode](wishlist-and-store-mode.md).

Recognized fork upgrades run any genuinely pending legacy activity snapshot
once. Accounts created afterwards start clean. Restarting, reading a page or
calling a schema helper does not copy legacy activity to later users or regrant
revoked memberships. Goodreads/StoryGraph reading activity belongs to the
importing user; duplicate Skip can preserve shared metadata while applying that
user's personal state. Import/privacy paths still require final integration
verification; an archive is not a full private-state backup.

## API tokens

With the feature enabled, every account can manage tokens from its account
dialog. The plaintext is shown once; only its hash is stored. A token permits
read-only **GET under `/api/v1/`**, never catalogue writes or UI mutation routes.
The owning account determines library read visibility, but neither an admin
identity nor Editor membership lifts the token above its viewer capability cap.

Password/role changes do not revoke a token: revoke it explicitly. Database
backups contain token hashes; portable archives do not. See
[Homelab integration](../homelab-integration.md).

## Sessions and logs

Login uses an HTTP-only cookie, marked Secure over HTTPS, with a sliding expiry
(7 days by default; `SHELF_SESSION_DAYS` changes it). Log out from the account
menu; changing your password signs out existing browser sessions. A database
restore invalidates earlier sessions but does not itself revoke restored API
token rows. Login attempts are rate-limited per IP.

**Logs** (admin account menu) shows auth events, sync activity, lookups and
errors. Review/redact logs before sharing; do not attach credentials, private
activity or databases to a public issue. See [Support](../../SUPPORT.md).
