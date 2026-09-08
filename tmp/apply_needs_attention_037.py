from pathlib import Path

# Register the dedicated router explicitly. app/routers/__init__.py must stay
# completely empty on Shelf 0.37.
path = Path("app/main.py")
lines = path.read_text().splitlines(keepends=True)
updated = []
import_done = False
for line in lines:
    if (
        not import_done
        and line.startswith("from app.routers import ")
        and "my_list" in line
        and "attention" not in line
    ):
        line = line.rstrip("\n") + ", attention\n"
        import_done = True
    updated.append(line)
text = "".join(updated)
if not import_done and "attention" not in text:
    raise SystemExit("main router import anchor not found")

if "app.include_router(attention.router)" not in text:
    if "app.include_router(continue_home.router)\n" in text:
        anchor = "app.include_router(continue_home.router)\n"
    elif "app.include_router(my_list.router)\n" in text:
        anchor = "app.include_router(my_list.router)\n"
    else:
        raise SystemExit("main router registration anchor not found")
    text = text.replace(
        anchor,
        anchor + "app.include_router(attention.router)\n",
        1,
    )
path.write_text(text)

print("Needs Attention 0.37 integration applied")
