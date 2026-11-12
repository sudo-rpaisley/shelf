"""All item-page paths use the same safe legacy-provider URL boundary."""

import pytest

from app.database import set_setting
from app.services import media_groups
from tests.conftest import _insert_item
from tests.test_features_gate import off


@pytest.mark.parametrize("connected", [True, False])
@pytest.mark.parametrize("linked", [True, False])
def test_no_full_page_abs_block_exposes_configured_url_credentials(viewer_client, db, connected, linked):
    item_id = _insert_item(db, title="Print", isbn=None)
    audio = _insert_item(db, title="Audio", isbn=None, media_type="audiobook", abs_id="audio-entry")
    if linked:
        media_groups.link_items(db, item_id, audio, link_type="format")
    else:
        item_id = audio
    set_setting(db, "abs_url", "https://internal.abs.example")
    set_setting(db, "abs_public_url", "https://reader:private-server-password@abs.example?token=private-url-token")
    db.commit()
    if not connected:
        off("connected_library")
    response = viewer_client.get(f"/item/{item_id}")
    assert response.status_code == 200
    assert "private-server-password" not in response.text
    assert "private-url-token" not in response.text


@pytest.mark.parametrize("connected", [True, False])
def test_legacy_abs_opaque_id_cannot_inject_query_or_fragment(viewer_client, db, connected):
    item_id = _insert_item(db, isbn=None, media_type="audiobook", abs_id="part/one?view=other#frag")
    set_setting(db, "abs_url", "http://internal.abs.example")
    set_setting(db, "abs_public_url", "https://public.abs.example/base")
    db.commit()
    if not connected:
        off("connected_library")
    response = viewer_client.get(f"/item/{item_id}")
    assert response.status_code == 200
    assert 'href="https://public.abs.example/base/item/part%2Fone%3Fview%3Dother%23frag"' in response.text
