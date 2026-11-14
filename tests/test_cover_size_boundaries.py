"""Explicit view-level boundary: selected series contains cards but no control."""

from urllib.parse import urlencode

import pytest

from app.services import library_catalog
from tests.conftest import _insert_item
from tests.test_connected_layout import _Markup


@pytest.mark.parametrize("client_name", ("viewer_client", "editor_client", "admin_client"))
def test_series_drilldown_keeps_the_declared_no_control_boundary_even_for_shared_cards(request, db, client_name):
    item = _insert_item(db, title="A series boundary", isbn=None, series_name="Series boundary")
    db.commit()
    key = library_catalog.series_key("Series boundary")
    response = request.getfixturevalue(client_name).get(
        "/library?" + urlencode({"reset": "1", "view": "series", "series": key}),
    )
    assert response.status_code == 200
    markup = _Markup(response.text)
    markup.one("article", **{"data-item-id": str(item)})
    assert not any("data-cover-size-control" in node.attrs for node in markup.elements)
    assert not any("data-cover-size-panel" in node.attrs for node in markup.elements)
