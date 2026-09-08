from pathlib import Path

path = Path('/tmp/patch.py')
text = path.read_text()
start = text.index('# Personal state is private')
end = text.index('# Related-media graph traversal', start)
section = r"""# Personal state is private, but only for catalogue rows the account may see.
replace_once(
    "app/routers/personal_state.py",
    "from app.services import user_state\n",
    "from app.services import libraries, user_state\n",
)
replace_once(
    "app/routers/personal_state.py",
    '''def _render(request: Request, item_id: int, *, status_code: int = 200):\n    with get_db() as db:\n        item = db.execute(\n''',
    '''def _render(request: Request, item_id: int, *, status_code: int = 200):\n    with get_db() as db:\n        user = dict(request.state.user)\n        if not libraries.has_item_role(db, user, item_id, "viewer"):\n            return HTMLResponse("Item not found", status_code=404)\n        item = db.execute(\n''',
)
replace_once(
    "app/routers/personal_state.py",
    '''    user_id = _user_id(request)\n\n    try:\n        with get_db() as db:\n            if not db.execute(\n''',
    '''    user = dict(request.state.user)\n    user_id = int(user["id"])\n\n    try:\n        with get_db() as db:\n            if not libraries.has_item_role(db, user, item_id, "viewer"):\n                return HTMLResponse("Item not found", status_code=404)\n            if not db.execute(\n''',
)

"""
path.write_text(text[:start] + section + text[end:])
