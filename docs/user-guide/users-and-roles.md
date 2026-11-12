# Users & roles

Shelf is multi-user from the first install: one shared library, several
logins, three roles.

| Role | Can |
|---|---|
| **Admin** | Everything — settings, users, integrations, locations, borrowers, sync, bulk ops, backup/restore, logs, **delete permanently and empty expired Trash**, the Trash retention setting, **rename, scope and delete tags**, **find and merge duplicate records**, plus all editor rights |
| **Editor** | Add / edit items, delete them to Trash and restore them from Trash, all scan modes, covers, lend and return, apply tags (one item or a Browse selection), import / export CSV and archives |
| **Viewer** | Browse, search, Series, Stats, Store Mode, set an item's **shared catalogue status** (read, watched, played), export CSV (without the items in Trash); experimental Library, Sources overview and own My Activity/saved views when enabled |

Cover controls — **Find cover**, **Upload**, **Remove cover**, **Retry
cover** — are editor+; a viewer doesn't see them on the item page at all.
The same goes for **Lend** / **Check in** and **Push to Hardcover** on the item
page, the Series page's rename, merge and disband actions, and Discover's
add-to-wishlist buttons: viewers get the read-only view of each, with the
loan context and the Series list still shown.

On the experimental `new-test` branch, **Connected library** also gives every
role its own activity and saved views. **Needs Attention**, format-connection
review and **Keep local / Follow source** metadata decisions need editor+.
New-source configuration, sync and **Remove link only** are admin-only.

**Trash** (account menu → Library) is for editors and admins. Both can
restore; only an admin sees **Delete permanently**, **Empty expired** and the
banner that says when Trash holds rows past the retention window. Viewers
cannot delete, so they have no Trash.

The first account created in the setup wizard is an admin. There must always
be at least one admin.

## Managing users

Settings → **Users** (admin): add a user with username, display name, role
and initial password; change a role; reset a password; delete a user.
Deleting a user doesn't touch the catalog — items are shared, not owned. It
does delete that user's API tokens and, in the experiment, their personal
state/history and saved Library views. The **API tokens** card on the same tab
lists every user's tokens, and an admin can revoke any of them — the way to
stop a leaked token without its owner.

Every user changes their **own** display name and password from the account
menu in the nav. The menu's trigger shows your name and your role, and the
panel opens with both, so it is always clear which account you are signed in
as. Entries follow role and feature visibility: **Account** holds profile and
password, **Administration** holds Settings/Logs for admins, and **Sign out**
ends the session. The experiment also adds **Personal → My Activity** and
**Library → Sources / Needs Attention** (the latter editor+).

With the API tokens feature on, the account dialog also has an **API tokens**
tab, for every role. A token lets a dashboard or script read `/api/v1/`. It
is read-only, it never acts above **viewer** whatever your role, and it is
shown once, when you create it. A password or role change does not end a
token; revoke it from the same tab. See
[Homelab integration](../homelab-integration.md).

## Sessions

Login sets an HTTP-only cookie (marked `Secure` whenever you reached Shelf over
HTTPS) with a 7-day JWT that refreshes while
you're active, so a device you use regularly stays logged in. **Log out**
from the account menu; to force every device out, change your password.
Restoring a database backup also signs every device out.
Login attempts are rate-limited per IP.

## The log viewer

**Logs** — in the account menu under your name, beside Settings (admin) —
tails Shelf's application log in the browser:
auth events (logins, failures, role changes), sync runs, metadata and cover
lookups, errors. Handy for "why didn't that cover load" without touching
`docker compose logs`.

## What's per-user today

Your login, display name, password and API tokens belong to your account. With
the experimental **Connected library** feature enabled, so do saved Library
views and **My Activity**: personal status, edition-specific progress, ratings,
favourites, notes, history and My List / Up Next. These do not change other
accounts, shared status/history, ownership, wishlist or Hardcover. A viewer may
edit their personal activity without becoming a catalogue editor.

The separate **Shared catalogue status** remains writable by every role and
keeps its existing shared history/optional Hardcover sync. Tags, copies,
ownership, wishlist and the catalogue itself are shared. **All imported source
entries are visible to every Shelf user**; source credentials/library selection
do not create private Shelf libraries or household isolation.

Personal activity and saved views travel in a full database backup, **not** a
CSV or portable collection archive. See
[Connected library](connected-library.md#my-activity-is-account-local) and its
[backup limits](connected-library.md#backup-and-portability).
