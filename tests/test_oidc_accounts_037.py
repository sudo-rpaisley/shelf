"""ACL-aware OIDC account provisioning on the Shelf 0.37 rebuild."""

from pathlib import Path

import pytest

from app.auth import hash_password
from app.oidc import OIDCAccessDenied, OIDCConfig, OIDCError, OIDCIdentity
from app.services import libraries, oidc_accounts


ISSUER = "https://id.example.test/application/o/shelf/"


def _config(*, auto_provision: bool = True, sync_roles: bool = True) -> OIDCConfig:
    return OIDCConfig(
        issuer=ISSUER,
        client_id="shelf",
        auto_provision=auto_provision,
        sync_roles=sync_roles,
    )


def _identity(subject: str, role: str, *, username: str = "alice") -> OIDCIdentity:
    return OIDCIdentity(
        issuer=ISSUER,
        subject=subject,
        username=username,
        display_name=username.title(),
        email=f"{username}@example.test",
        groups=(f"shelf-{role}",),
        role=role,
    )


def test_identity_schema_is_centrally_owned(db):
    table = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='user_identities'"
    ).fetchone()
    index = db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_user_identities_user'"
    ).fetchone()
    assert table is not None
    assert index is not None

    source = Path("app/services/oidc_accounts.py").read_text()
    assert "CREATE TABLE" not in source
    assert "ALTER TABLE" not in source


@pytest.mark.parametrize(
    ("role", "expected_membership"),
    [("viewer", "viewer"), ("editor", "editor"), ("admin", None)],
)
def test_auto_provision_maps_only_main_library(db, role, expected_membership):
    user = oidc_accounts.provision_or_sync(
        _identity(f"subject-{role}", role), _config()
    )

    linked = db.execute(
        "SELECT issuer, subject, user_id FROM user_identities WHERE user_id = ?",
        (user["id"],),
    ).fetchone()
    assert linked["issuer"] == ISSUER
    assert linked["subject"] == f"subject-{role}"

    membership = db.execute(
        "SELECT role FROM library_memberships WHERE library_id = ? AND user_id = ?",
        (libraries.DEFAULT_LIBRARY_ID, user["id"]),
    ).fetchone()
    if expected_membership is None:
        assert membership is None
    else:
        assert membership["role"] == expected_membership


def test_stable_subject_not_username_or_email_is_identity_key(db):
    first = oidc_accounts.provision_or_sync(
        _identity("stable-subject", "viewer", username="alice"), _config()
    )
    changed = OIDCIdentity(
        issuer=ISSUER,
        subject="stable-subject",
        username="renamed-at-provider",
        display_name="Provider Display Changed",
        email="new-address@example.test",
        groups=("shelf-viewer",),
        role="viewer",
    )
    second = oidc_accounts.provision_or_sync(changed, _config())

    assert second["id"] == first["id"]
    assert second["username"] == first["username"]
    assert second["display_name"] == "Provider Display Changed"
    assert db.execute("SELECT COUNT(*) AS c FROM user_identities").fetchone()["c"] == 1


def test_matching_local_username_does_not_auto_link(db):
    db.execute(
        "INSERT INTO users (username, password, display_name, role) VALUES (?, ?, ?, 'viewer')",
        ("alice", hash_password("local-password"), "Local Alice"),
    )
    db.commit()

    oidc_user = oidc_accounts.provision_or_sync(
        _identity("different-human-proof", "viewer", username="alice"), _config()
    )
    assert oidc_user["username"] == "alice-oidc"
    assert db.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 2


def test_auto_provision_can_be_disabled(db):
    with pytest.raises(OIDCAccessDenied):
        oidc_accounts.provision_or_sync(
            _identity("not-preprovisioned", "viewer"),
            _config(auto_provision=False),
        )
    assert db.execute("SELECT COUNT(*) AS c FROM user_identities").fetchone()["c"] == 0


def test_role_sync_updates_main_library_but_preserves_custom_memberships(db):
    user = oidc_accounts.provision_or_sync(
        _identity("role-sync", "editor"), _config()
    )
    custom = libraries.create_library(db, "Private Research")
    libraries.set_membership(db, custom["id"], user["id"], "editor")
    db.commit()
    before_tv = user["token_version"]

    changed = oidc_accounts.provision_or_sync(
        _identity("role-sync", "viewer"), _config(sync_roles=True)
    )
    assert changed["role"] == "viewer"
    assert changed["token_version"] == before_tv + 1
    assert libraries.membership_role(db, changed, libraries.DEFAULT_LIBRARY_ID) == "viewer"
    assert libraries.membership_role(db, changed, custom["id"]) == "editor"


def test_sync_roles_off_preserves_shelf_role_and_main_membership(db):
    user = oidc_accounts.provision_or_sync(
        _identity("no-sync", "editor"), _config(sync_roles=True)
    )
    unchanged = oidc_accounts.provision_or_sync(
        _identity("no-sync", "viewer"), _config(sync_roles=False)
    )
    assert unchanged["role"] == "editor"
    assert libraries.membership_role(db, unchanged, libraries.DEFAULT_LIBRARY_ID) == "editor"
    assert unchanged["token_version"] == user["token_version"]


def test_role_sync_cannot_demote_last_shelf_admin(db):
    admin = oidc_accounts.provision_or_sync(
        _identity("only-admin", "admin"), _config()
    )
    assert admin["role"] == "admin"

    with pytest.raises(OIDCError, match="last Shelf administrator"):
        oidc_accounts.provision_or_sync(
            _identity("only-admin", "viewer"), _config(sync_roles=True)
        )

    row = db.execute("SELECT role FROM users WHERE id = ?", (admin["id"],)).fetchone()
    assert row["role"] == "admin"
