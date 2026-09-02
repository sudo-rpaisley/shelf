from pathlib import Path


def replace(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f"anchor not found in {path}")
    p.write_text(text.replace(old, new, 1))


replace(
    "app/routers/pages.py",
    '''            data["provider_url"] = None\n            data["provider_name"] = None\n            if data["id"] in abs_link_map:\n                data["provider_url"] = abs_link_map[data["id"]]\n                data["provider_name"] = "Audiobookshelf"\n            elif data["id"] in komga_link_map:\n                data["provider_url"] = komga_link_map[data["id"]]\n                data["provider_name"] = "Komga"\n            elif data["id"] in romm_link_map:\n                data["provider_url"] = romm_link_map[data["id"]]\n                data["provider_name"] = "RomM"\n''',
    '''            # A related item may legitimately be represented in more than one\n            # external service. Keep every deep link rather than choosing the first.\n            data["provider_links"] = []\n            if data["id"] in abs_link_map:\n                data["provider_links"].append({\n                    "name": "Audiobookshelf", "url": abs_link_map[data["id"]]\n                })\n            if data["id"] in komga_link_map:\n                data["provider_links"].append({\n                    "name": "Komga", "url": komga_link_map[data["id"]]\n                })\n            if data["id"] in romm_link_map:\n                data["provider_links"].append({\n                    "name": "RomM", "url": romm_link_map[data["id"]]\n                })\n            # Preserve the old singular fields for any downstream template/plugin\n            # code while the built-in UI consumes provider_links.\n            data["provider_url"] = (\n                data["provider_links"][0]["url"] if data["provider_links"] else None\n            )\n            data["provider_name"] = (\n                data["provider_links"][0]["name"] if data["provider_links"] else None\n            )\n''',
)

replace(
    "app/templates/fragments/related_media.html",
    '''                {% if related.provider_url %}\n                <a href="{{ related.provider_url }}" target="_blank" rel="noopener"\n                   class="px-2 py-1 bg-shelf-accent/15 text-shelf-accent2 rounded text-xs hover:bg-shelf-accent/25 transition-colors">\n                    Also in {{ related.provider_name }} ({{ related.media_label }})\n                </a>\n                {% endif %}\n''',
    '''                {% for provider in related.provider_links %}\n                <a href="{{ provider.url }}" target="_blank" rel="noopener"\n                   class="px-2 py-1 bg-shelf-accent/15 text-shelf-accent2 rounded text-xs hover:bg-shelf-accent/25 transition-colors">\n                    Also in {{ provider.name }} ({{ related.media_label }})\n                </a>\n                {% endfor %}\n''',
)

p = Path("tests/test_related_media_ui.py")
text = p.read_text()
text = text.replace(
    '''    pc = _insert_item(\n        db, title="Example Quest", isbn=None, media_type="video_game",\n        platform="pc", publish_year=1996,\n    )\n''',
    '''    pc = _insert_item(\n        db, title="Example Quest", isbn=None, media_type="digital_game",\n        platform="pc", publish_year=1996, romm_id="13",\n    )\n''',
    1,
)
text = text.replace(
    '''    assert response.text.count('data-testid="related-media-item"') == 2\n    assert "Also in RomM" in response.text\n\n\ndef test_cross_media_manual_group_renders_formats''',
    '''    assert response.text.count('data-testid="related-media-item"') == 2\n    # The current SNES item has its own Open in RomM action; both other\n    # platform versions retain independent RomM deep links in the group.\n    assert response.text.count("Also in RomM (Digital Game)") == 2\n\n\ndef test_multiple_abs_narrator_editions_keep_separate_links(admin_client, db):\n    book = _insert_item(\n        db, title="Example Novel", isbn=None, media_type="book",\n        authors="Example Author",\n    )\n    _insert_item(\n        db, title="Example Novel", isbn=None, media_type="audiobook",\n        authors="Example Author", narrator="Narrator One", abs_id="abs-one",\n    )\n    _insert_item(\n        db, title="Example Novel", isbn=None, media_type="audiobook",\n        authors="Example Author", narrator="Narrator Two", abs_id="abs-two",\n    )\n    media_groups.auto_link_family(db, "book")\n    db.execute(\n        "INSERT INTO settings (key, value) VALUES ('abs_url', 'http://abs:13378')"\n    )\n    db.execute("COMMIT")\n\n    response = admin_client.get(f"/item/{book}")\n    assert response.status_code == 200\n    assert "Narrator One" in response.text\n    assert "Narrator Two" in response.text\n    assert response.text.count("Also in Audiobookshelf (Audiobook)") == 2\n    assert "abs-one" in response.text\n    assert "abs-two" in response.text\n\n\ndef test_cross_media_manual_group_renders_formats''',
    1,
)
p.write_text(text)

# Temporary helper files should not survive in the feature diff.
Path("scripts/provider_links_patch.py").unlink(missing_ok=True)
Path(".github/workflows/provider-links-build.yml").unlink(missing_ok=True)

print("Multiple provider links applied")
