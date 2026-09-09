from pathlib import Path
import subprocess

ASSIGNMENT_SHA = "d25b41bc6e28d72a31c0ff38b7640e27c885b2bd"
EXPECTED_CONFLICTS = {
    "README.md",
    "app/browse_filters.py",
    "app/routers/personal_browse.py",
    "app/templates/browse.html",
    "app/templates/item_detail.html",
}


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, text=True, check=check)


result = run("git", "cherry-pick", ASSIGNMENT_SHA, check=False)
if result.returncode:
    conflicts = set(
        subprocess.check_output(
            ["git", "diff", "--name-only", "--diff-filter=U"], text=True
        ).splitlines()
    )
    if conflicts != EXPECTED_CONFLICTS:
        raise SystemExit(f"unexpected assignment conflict set: {sorted(conflicts)}")

    # Generated badge and the current upstream UI/filter implementations are
    # authoritative. Re-apply only the Collection additions below.
    run(
        "git", "checkout", "--ours", "--",
        "README.md", "app/browse_filters.py", "app/templates/browse.html",
        "app/templates/item_detail.html",
    )
    # Current upstream folded Browse back into pages.py; do not resurrect the
    # deleted recovery-era router.
    run("git", "rm", "app/routers/personal_browse.py")

    p = Path("app/browse_filters.py")
    s = p.read_text()
    owned_anchor = "\ndef _owned(value):\n"
    if owned_anchor not in s:
        raise SystemExit("browse filter function anchor not found")
    collection_fn = '''\ndef _collection(value):
    try:
        collection_id = int(value)
    except (TypeError, ValueError):
        return _NEVER
    if not (_SQLITE_INT_MIN <= collection_id <= _SQLITE_INT_MAX):
        return _NEVER
    return (
        "i.id IN (SELECT ci.item_id FROM collection_items ci "
        "WHERE ci.collection_id = ?)",
        [collection_id],
    )

'''
    s = s.replace(owned_anchor, collection_fn + "def _owned(value):\n", 1)
    filter_anchor = '    BrowseFilter("q", prefix="Search", condition=_search),\n'
    if filter_anchor not in s:
        raise SystemExit("browse filter registry anchor not found")
    s = s.replace(
        filter_anchor,
        filter_anchor + '    BrowseFilter("collection", prefix="Collection", condition=_collection),\n',
        1,
    )
    p.write_text(s)

    # The cherry-picked item-detail projection merged cleanly into pages.py,
    # but current upstream does not bind request.state.user to a local name.
    # Browse also needs the visible Collection options previously supplied by
    # the now-deleted personal_browse router.
    p = Path("app/routers/pages.py")
    s = p.read_text()
    browse_anchor = '''    values = browse_filters.values_from(request.query_params)\n'''
    if browse_anchor not in s:
        raise SystemExit("Browse values anchor not found")
    s = s.replace(
        browse_anchor,
        '    user = dict(request.state.user)\n' + browse_anchor,
        1,
    )
    tags_anchor = '''        from app.routers.tags import get_all_tags\n        all_tags = get_all_tags(db)\n'''
    if tags_anchor not in s:
        raise SystemExit("Browse tags anchor not found")
    s = s.replace(
        tags_anchor,
        tags_anchor
        + '''\n        from app.services import collections as collection_service\n        browse_collections = collection_service.accessible_options(db, user)\n''',
        1,
    )
    ctx_anchor = '''        "media_types": MEDIA_TYPES,\n'''
    if ctx_anchor not in s:
        raise SystemExit("Browse context anchor not found")
    s = s.replace(
        ctx_anchor,
        ctx_anchor + '        "browse_collections": browse_collections,\n',
        1,
    )
    item_anchor = '''    back = nav.back_target(from_)\n    with get_db() as db:\n'''
    if item_anchor not in s:
        raise SystemExit("item detail user anchor not found")
    s = s.replace(
        item_anchor,
        '    back = nav.back_target(from_)\n    user = dict(request.state.user)\n    with get_db() as db:\n',
        1,
    )
    p.write_text(s)

    p = Path("app/templates/browse.html")
    s = p.read_text()
    type_anchor = '''            <!-- Media Type Filter -->\n'''
    if type_anchor not in s:
        raise SystemExit("Browse media type anchor not found")
    selector = '''            <label for="collection-filter" class="sr-only">Collection</label>
            <select id="collection-filter" name="collection" data-testid="collection-filter"
                    hx-get="/api/search" hx-trigger="change" hx-target="#item-grid"
                    hx-include="{{ filter_includes('collection') }}"
                    class="bg-shelf-card border border-shelf-border rounded-lg px-3 py-2 text-sm text-shelf-text focus:ring-2 focus:ring-shelf-accent outline-none">
                <option value="" {{ 'selected' if not f.collection else '' }}>All Collections</option>
                {% for collection in browse_collections %}
                <option value="{{ collection.id }}" {{ 'selected' if f.collection == collection.id|string else '' }}>{{ collection.name }} — {{ collection.library_name }}</option>
                {% endfor %}
            </select>

'''
    p.write_text(s.replace(type_anchor, selector + type_anchor, 1))

    p = Path("app/templates/item_detail.html")
    s = p.read_text()
    actions_anchor = '''                <!-- Actions -->\n'''
    if actions_anchor not in s:
        raise SystemExit("item detail actions anchor not found")
    card = '''                {% if item_collections or available_collections %}
                <div class="mb-6" data-testid="item-collections-card">
                    <div class="flex items-center justify-between gap-3 mb-2">
                        <p class="text-sm font-medium text-shelf-muted">Collections</p>
                        <a href="/collections" class="text-xs text-shelf-accent2 hover:text-shelf-accent">View all</a>
                    </div>
                    {% if item_collections %}
                    <div class="space-y-2" data-testid="item-collection-memberships">
                        {% for collection in item_collections %}
                        <div id="item-collection-{{ collection.id }}" class="flex items-center justify-between gap-2 bg-shelf-bg border border-shelf-border rounded-lg px-3 py-2">
                            <a href="/collections/{{ collection.id }}" class="text-sm text-shelf-accent2 hover:text-shelf-accent truncate">{{ collection.name }}</a>
                            {% if can_edit_collections %}
                            <button type="button" hx-delete="/api/items/{{ item.id }}/collections/{{ collection.id }}" hx-target="#item-collection-{{ collection.id }}" hx-swap="outerHTML" hx-confirm="Remove this item from '{{ collection.name }}'?" class="text-xs text-shelf-muted hover:text-shelf-error" aria-label="Remove from {{ collection.name }}">Remove</button>
                            {% endif %}
                        </div>
                        {% endfor %}
                    </div>
                    {% else %}
                    <p class="text-sm text-shelf-muted">Not in a collection yet.</p>
                    {% endif %}

                    {% if can_edit_collections and available_collections %}
                    <form action="/api/items/{{ item.id }}/collections" method="post" class="flex gap-2 mt-3" data-testid="add-to-collection-form">
                        <select name="collection_id" required class="min-w-0 flex-1 bg-shelf-bg border border-shelf-border rounded-lg px-2 py-1.5 text-sm text-shelf-text">
                            {% for collection in available_collections %}<option value="{{ collection.id }}">{{ collection.name }}</option>{% endfor %}
                        </select>
                        <button type="submit" class="px-3 py-1.5 bg-shelf-accent text-white rounded-lg text-xs">Add</button>
                    </form>
                    {% endif %}
                </div>

'''
    p.write_text(s.replace(actions_anchor, card + actions_anchor, 1))

    # The old recovery fixture automatically gave global editors Main Library
    # membership. Current upstream deliberately does not, so this test states
    # its permission prerequisite explicitly.
    p = Path("tests/test_collections_assignment_037.py")
    if p.exists():
        s = p.read_text()
        anchor = 'def test_editor_can_add_and_remove_from_item_detail(editor_client, editor_user, db):\n'
        if anchor not in s:
            raise SystemExit("assignment editor test anchor not found")
        s = s.replace(
            anchor,
            anchor + '    libraries.set_membership(db, 1, editor_user["id"], "editor")\n',
            1,
        )
        p.write_text(
            s.replace(
                "Collections assignment, Browse filtering and merge integrity on 0.37.",
                "Collections assignment, Browse filtering and merge integrity.",
                1,
            )
        )
        run("git", "mv", "tests/test_collections_assignment_037.py", "tests/test_collections_assignment.py")

    run("git", "add", "-A")
    run("git", "cherry-pick", "--continue")

run("git", "diff", "--check")
