from pathlib import Path

path = Path("app/auth.py")
text = path.read_text()
old = '''        from app.services import libraries
        with get_db() as db:
            allowed = libraries.has_item_role(
                db, dict(user), item_id, minimum_role
            )
        if not allowed:
            _raise_item_not_found(request)
        return user
'''
new = '''        from app.services import libraries
        with get_db() as db:
            actor = dict(user)
            allowed = libraries.has_item_role(db, actor, item_id, minimum_role)
            visible = allowed or libraries.has_item_role(db, actor, item_id, "viewer")
        if not allowed:
            # An item the actor may already see is not secret; preserve Shelf's
            # normal 403 surface when they simply lack edit rights. Only an
            # inaccessible/unmapped item is hidden as a 404.
            if minimum_role == "editor" and visible:
                _raise_insufficient_role(request)
            _raise_item_not_found(request)
        return user
'''
if old not in text:
    raise SystemExit("item-role semantics anchor not found")
path.write_text(text.replace(old, new, 1))
