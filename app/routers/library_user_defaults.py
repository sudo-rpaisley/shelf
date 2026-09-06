"""Compatibility defaults for user creation after first-class libraries.

Until the per-library membership matrix is exposed in Settings, creating a
normal Viewer/Editor through the existing Users UI must not create an account
that can see an empty Shelf. New non-admin users therefore receive matching
access to Main Library at creation time. Explicit membership removal remains
fully authoritative afterwards.
"""

from __future__ import annotations

import logging
import sqlite3

from fastapi import Depends, Form, Request

from app.auth import hash_password, require_role
from app.routers import auth_routes
from app.services import libraries

logger = logging.getLogger(__name__)


def _remove_route(path: str, method: str) -> None:
    method = method.upper()
    auth_routes.router.routes[:] = [
        route
        for route in auth_routes.router.routes
        if not (
            getattr(route, "path", None) == path
            and method in (getattr(route, "methods", None) or set())
        )
    ]


_remove_route("/api/users", "POST")


@auth_routes.router.post("/api/users")
async def create_user_with_default_library(
    request: Request,
    username: str = Form(...),
    display_name: str = Form(""),
    password: str = Form(...),
    role: str = Form("viewer"),
    _=Depends(require_role("admin")),
):
    """Create a user and seed their initial Main Library membership atomically.

    Resolve ``get_db`` through ``auth_routes`` rather than capturing a second
    module-level reference. The adapter replaces auth_routes' historical route,
    so it must preserve that route's database boundary for diagnostics/tests
    and for any future instrumentation applied there.
    """
    username = username.strip()
    display_name = display_name.strip() or username
    if role not in ("admin", "editor", "viewer"):
        return {"ok": False, "message": "Invalid role"}
    if len(password) < 8:
        return {"ok": False, "message": "Password must be at least 8 characters"}
    if not username or len(username) < 2:
        return {"ok": False, "message": "Username must be at least 2 characters"}

    try:
        with auth_routes.get_db() as db:
            cursor = db.execute(
                "INSERT INTO users (username, password, display_name, role) VALUES (?, ?, ?, ?)",
                (username, hash_password(password), display_name, role),
            )
            user_id = int(cursor.lastrowid)

            if role in ("viewer", "editor"):
                # Migration 60 creates this row on every upgraded/current Shelf
                # install. Keep the existence check so isolated historical-schema
                # tests never gain an implicit schema/data backfill.
                main = db.execute(
                    "SELECT id FROM libraries WHERE id = ?",
                    (libraries.DEFAULT_LIBRARY_ID,),
                ).fetchone()
                if main:
                    libraries.set_membership(
                        db,
                        libraries.DEFAULT_LIBRARY_ID,
                        user_id,
                        role,
                    )
    except sqlite3.IntegrityError:
        logger.warning("Failed to create user '%s': username already exists", username)
        return {"ok": False, "message": "Username already exists"}

    logger.info(
        "User '%s' created with role '%s' and default library access",
        username,
        role,
    )
    return {"ok": True, "message": f"User '{username}' created"}