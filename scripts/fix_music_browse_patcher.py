from pathlib import Path

p = Path("scripts/apply_music_browse_artwork_final.py")
text = p.read_text()
old = '''replace(\n    "app/routers/music.py",\n    '        return RedirectResponse("/music", status_code=303)\\n',\n    '        return RedirectResponse("/music/add", status_code=303)\\n',\n)\n'''
new = '''replace(\n    "app/routers/music.py",\n    ''' + "'''" + '''    release_id = release_id.strip()\\n    if not release_id:\\n        return RedirectResponse(\"/music\", status_code=303)\\n''' + "'''" + ''',\n    ''' + "'''" + '''    release_id = release_id.strip()\\n    if not release_id:\\n        return RedirectResponse(\"/music/add\", status_code=303)\\n''' + "'''" + ''',\n)\n'''
if text.count(old) != 1:
    raise RuntimeError(f"expected one generic music redirect patch, found {text.count(old)}")
p.write_text(text.replace(old, new, 1))
