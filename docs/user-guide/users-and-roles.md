# Users & roles

Shelf is multi-user from the first install: one shared library, several
logins, three roles.

| Role | Can |
|---|---|
| **Admin** | Everything — settings, users, integrations, locations, borrowers, sync, bulk ops, backup/restore, logs, plus all editor rights |
| **Editor** | Add / edit / delete items, all scan modes, covers, lend and return, tags, import / export CSV and archives |
| **Viewer** | Browse, search, Series, Stats, Store Mode, set reading status on items, export CSV |

Cover controls — **Find cover**, **Upload**, **Remove cover**, **Retry
cover** — are editor+; a viewer doesn't see them on the item page at all.

The first account created in the setup wizard is an admin. There must always
be at least one admin.

## Managing users

Settings → **Users** (admin): add a local user with username, display name,
role and initial password; change a locally managed role; reset a local
password; delete a user. Deleting a user doesn't touch the catalog — items are
shared, not owned.

Every local user can change their **own** display name and password from the
account menu in the nav.

## OpenID Connect (OIDC) single sign-on

Shelf can use any standards-compliant OpenID Connect identity provider while
keeping Shelf's existing Admin, Editor and Viewer authorisation model. OIDC is
configured in Settings → **Users**.

Shelf uses the Authorization Code flow with PKCE. Enter the provider's exact
**issuer URL**, Shelf's **client ID** and, for a confidential client, its
**client secret**. The secret is stored encrypted and is never shown again.
Register the callback URI displayed by Shelf as an allowed redirect URI at the
identity provider.

The default scopes are `openid profile email`. Your provider must also expose
the configured group claim if group-based access is enabled. The default group
claim name is `groups`; dotted paths such as `realm_access.groups` are
supported for providers that nest claims.

Use **Save & test configuration** before relying on SSO. The test retrieves
OIDC discovery metadata, verifies that the discovered issuer exactly matches
the configured issuer, validates the advertised HTTPS endpoints and checks for
Authorization Code and PKCE S256 support. A real sign-in additionally validates
the client, signed ID token and returned claims.

### Group access and Shelf roles

OIDC groups can map directly to Shelf roles. For example:

| Identity-provider group | Shelf role |
|---|---|
| `Shelf-Admins` | Admin |
| `Shelf-Editors` | Editor |
| `Shelf-Users` | Viewer |

If several mappings match, Shelf grants the highest role: Admin, then Editor,
then Viewer. Group names are matched exactly.

An optional **Required access group** is a separate hard gate. A useful setup
is to require `Shelf-Users`, then also map `Shelf-Admins` and `Shelf-Editors`
to the higher roles. A valid identity-provider account that is not in the
required group is denied before a Shelf account is created.

For the safest default, leave **If no role group matches** set to **Deny
access**. Viewer or Editor can be selected when the identity provider already
applies an equivalent access policy.

### Provisioning and role synchronisation

With automatic provisioning enabled, the first approved OIDC sign-in creates a
Shelf user. The external identity is linked using the OIDC issuer and stable
subject (`iss` + `sub`), not an email address or username. If an external
username collides with an existing local Shelf username, Shelf creates a
distinct username rather than silently linking the accounts.

With **Synchronise Shelf role on every OIDC sign-in** enabled, the identity
provider becomes the source of truth for that user's Shelf role. The local role
selector is disabled and group changes take effect at the next OIDC sign-in.
Shelf will not allow role synchronisation to demote the last administrator.

OIDC accounts do not use Shelf passwords. Local password login, password reset
and local profile-name changes are blocked for an OIDC identity; make those
changes in the identity provider instead.

### Break-glass access

Keep a separate local Shelf administrator with a strong unique password even
when OIDC is the normal sign-in method. Local sign-in remains available so an
identity-provider outage or configuration mistake does not lock you out of
Shelf.

Do not try to reuse an OIDC-managed account as the break-glass account. Shelf
deliberately keeps local and external identities separate.

### Authentik example

Authentik works with Shelf as a normal OIDC provider. A typical deployment uses
three Authentik groups named `Shelf-Users`, `Shelf-Editors` and
`Shelf-Admins`, with `Shelf-Users` also configured as Shelf's required access
group. Register the callback URI displayed on Shelf's OIDC settings card in the
Authentik provider, then copy the provider's exact issuer URL, client ID and
client secret into Shelf.

After configuration, test at least one account at each role and then remove or
change a test user's group membership to confirm that the next OIDC sign-in
updates or denies access as intended.

## Sessions

Local login sets an HTTP-only, secure cookie with a 7-day JWT that refreshes
while you're active, so a device you use regularly stays logged in. OIDC
sessions are intentionally different: they have a fixed 24-hour maximum and do
not slide. This forces Shelf to revisit identity-provider access and role claims
regularly, while an existing provider SSO session will normally make the next
OIDC authentication transparent.

**Log out** from the account menu. Changing a local password invalidates that
local user's existing Shelf sessions; a synced OIDC role change also invalidates
existing Shelf sessions for that user. Login attempts are rate-limited per IP.

Shelf logout clears the Shelf session. It does not currently terminate the
identity provider's own SSO session, so choosing OIDC sign-in again may sign you
back in without another password prompt.

## The log viewer

**Logs** in the nav (admin) tails Shelf's application log in the browser:
auth events (logins, failures, role changes), sync runs, metadata and cover
lookups, errors. Handy for "why didn't that cover load" without touching
`docker compose logs`.

## What's per-user today

Your authentication identity, display name and role are user-specific. Reading
status, tags, wishlist and everything else are shared across the household.
Per-user reading tracking and per-household libraries are on the roadmap (see
[FUTURE_FEATURES in the repo issues](https://github.com/dgahagan/shelf/issues)).
