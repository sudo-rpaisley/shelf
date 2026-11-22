from pathlib import Path
from html.parser import HTMLParser

from jinja2 import Environment, FileSystemLoader

from app.database import get_db
from app.services import romm_records, romm_sync
from app.services.item_write import insert_item


CARD = Path(__file__).resolve().parents[1] / "app" / "templates" / "fragments" / "item_card.html"


class _CardAnchors(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.nodes = []

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "parent": self.stack[-1] if self.stack else None}
        self.nodes.append(node)
        if tag not in {"img", "input", "br", "hr", "meta", "link"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1]["tag"] == tag:
            self.stack.pop()


def _seed_romm_item():
    with get_db() as conn:
        return romm_records.persist_candidate(conn, {
            "romm_id": "rom-browse-123",
            "romm_platform_id": "1",
            "title": "Chrono Trigger",
            "platform": "snes",
            "platform_name": "SNES",
        })["item_id"]


def test_romm_browse_card_has_a_separate_external_action(viewer_client, db):
    romm_sync.save_configuration(
        url="http://romm:8080", public_url="https://romm.example", token="rmm_secret"
    )
    item_id = _seed_romm_item()

    response = viewer_client.get("/browse?source_filter=romm")
    assert response.status_code == 200
    assert 'data-testid="romm-card-action"' in response.text
    assert f'href="/api/romm/items/{item_id}/open"' in response.text


def test_romm_browse_open_redirect_uses_browser_facing_root(viewer_client, db):
    romm_sync.save_configuration(
        url="http://romm:8080", public_url="https://romm.example", token="rmm_secret"
    )
    item_id = _seed_romm_item()

    response = viewer_client.get(
        f"/api/romm/items/{item_id}/open", follow_redirects=False
    )
    assert response.status_code == 302
    assert response.headers["location"] == "https://romm.example/rom/rom-browse-123"


def test_romm_browse_open_degrades_to_item_page_when_action_is_unavailable(viewer_client):
    with get_db() as conn:
        item_id = insert_item(conn, {"title": "Visible game without RomM", "media_type": "video_game"})
    response = viewer_client.get(f"/api/romm/items/{item_id}/open", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/item/{item_id}"


def test_romm_action_is_a_sibling_of_the_normal_item_link():
    env = Environment(loader=FileSystemLoader(CARD.parents[1]), autoescape=True)
    env.globals.update(_=lambda value, **kwargs: value % kwargs if kwargs else value,
                       feature_on=lambda key: True, cover_shape=lambda kind: "portrait")
    item = {"id": 42, "title": "Chrono Trigger", "media_type": "digital_game", "source": "romm",
            "personal_wishlist": True, "wishlisted": False, "lent_to": None, "reading_status": None,
            "created_at": None, "cover_path": None, "authors": None}
    rendered = env.get_template("fragments/item_card.html").render(item=item, seven_days_ago="2020-01-01")
    parsed = _CardAnchors()
    parsed.feed(rendered)
    anchors = [node for node in parsed.nodes if node["tag"] == "a"]
    assert len(anchors) == 2
    normal, action = anchors
    assert normal["attrs"]["href"] == "/item/42"
    assert action["attrs"]["href"] == "/api/romm/items/42/open"
    assert action["attrs"]["data-testid"] == "romm-card-action"
    assert action["parent"] is normal["parent"]
    assert normal["parent"]["tag"] == "div"
    assert action["attrs"]["target"] == "_blank"
    assert action["attrs"]["rel"] == "noopener"
    assert 'data-testid="personal-wishlist-badge"' in rendered
    assert "{% if item.source == 'romm' %}" in CARD.read_text()


def test_romm_open_route_is_registered():
    from app.main import app

    paths = {route.path for route in app.routes}
    assert "/api/romm/items/{item_id}/open" in paths
