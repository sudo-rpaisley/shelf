"""Provider-neutral OIDC identity and role-policy regressions (#89)."""

import pytest

from app.oidc import (
    OIDCAccessDenied,
    OIDCConfig,
    OIDCError,
    groups_from_claims,
    identity_from_claims,
    parse_groups,
    role_from_groups,
    validate_issuer_url,
)


def _config(**overrides):
    values = dict(
        issuer="https://idp.example/application/o/shelf/",
        client_id="shelf-client",
        group_claim="groups",
        required_group="Shelf-Users",
        admin_groups=("Shelf-Admins",),
        editor_groups=("Shelf-Editors",),
        viewer_groups=("Shelf-Users",),
        default_role="deny",
        auto_provision=True,
        sync_roles=True,
    )
    values.update(overrides)
    return OIDCConfig(**values)


def test_parse_groups_deduplicates_without_changing_case():
    assert parse_groups("Shelf-Admins\nShelf-Users, Shelf-Admins") == (
        "Shelf-Admins",
        "Shelf-Users",
    )


def test_nested_group_claim_path_is_supported():
    claims = {"realm": {"groups": ["Shelf-Users", "Shelf-Editors"]}}
    assert groups_from_claims(claims, "realm.groups") == (
        "Shelf-Users",
        "Shelf-Editors",
    )


def test_role_mapping_uses_highest_matching_shelf_role():
    config = _config()
    assert role_from_groups(("Shelf-Users",), config) == "viewer"
    assert role_from_groups(("Shelf-Users", "Shelf-Editors"), config) == "editor"
    assert role_from_groups(
        ("Shelf-Users", "Shelf-Editors", "Shelf-Admins"), config
    ) == "admin"


def test_unmapped_groups_default_to_deny():
    config = _config(required_group="", viewer_groups=())
    assert role_from_groups(("Other",), config) is None


def test_default_role_can_grant_viewer_or_editor_but_not_admin():
    assert role_from_groups((), _config(required_group="", default_role="viewer")) == "viewer"
    assert role_from_groups((), _config(required_group="", default_role="editor")) == "editor"
    with pytest.raises(OIDCError, match="Invalid default"):
        _config(default_role="admin")


def test_required_group_is_a_hard_access_gate():
    claims = {
        "sub": "123",
        "preferred_username": "alice",
        "name": "Alice",
        "groups": ["Shelf-Editors"],
    }
    with pytest.raises(OIDCAccessDenied, match="required Shelf access group"):
        identity_from_claims(claims, _config())


def test_identity_projects_claims_into_existing_shelf_roles():
    identity = identity_from_claims(
        {
            "sub": "subject-123",
            "preferred_username": "Alice Example",
            "name": "Alice Example",
            "email": "alice@example.test",
            "groups": ["Shelf-Users", "Shelf-Editors"],
        },
        _config(),
    )
    assert identity.subject == "subject-123"
    assert identity.username == "Alice-Example"
    assert identity.display_name == "Alice Example"
    assert identity.email == "alice@example.test"
    assert identity.role == "editor"


def test_missing_subject_is_rejected():
    with pytest.raises(OIDCError, match="subject"):
        identity_from_claims({"groups": ["Shelf-Users"]}, _config())


def test_issuer_requires_https_by_default(monkeypatch):
    monkeypatch.delenv("SHELF_OIDC_ALLOW_INSECURE_HTTP", raising=False)
    with pytest.raises(OIDCError, match="HTTPS"):
        validate_issuer_url("http://idp.example/issuer")
    validate_issuer_url("https://idp.example/issuer")


def test_issuer_rejects_credentials_query_and_fragment():
    for value in (
        "https://user:pass@idp.example/issuer",
        "https://idp.example/issuer?tenant=a",
        "https://idp.example/issuer#fragment",
    ):
        with pytest.raises(OIDCError, match="invalid"):
            validate_issuer_url(value)
