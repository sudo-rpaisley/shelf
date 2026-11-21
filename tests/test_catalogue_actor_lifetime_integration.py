"""The authenticated actor spans streaming responses, not just call_next()."""

import asyncio

import pytest


@pytest.mark.asyncio
async def test_actor_remains_scoped_until_the_last_stream_chunk():
    from app.main import CatalogueActorMiddleware
    from app.services import catalogue_security

    user = {"id": 41, "role": "viewer", "username": "reader"}
    observed = []

    async def downstream(scope, receive, send):
        observed.append(catalogue_security.current_actor())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await asyncio.sleep(0)
        observed.append(catalogue_security.current_actor())
        await send({"type": "http.response.body", "body": b"last", "more_body": False})

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        assert catalogue_security.current_actor() == user

    assert catalogue_security.current_actor() is None
    await CatalogueActorMiddleware(downstream)(
        {"type": "http", "state": {"user": user}}, receive, send
    )
    assert observed == [user, user]
    assert catalogue_security.current_actor() is None


@pytest.mark.asyncio
async def test_actor_resets_after_a_response_exception():
    from app.main import CatalogueActorMiddleware
    from app.services import catalogue_security

    user = {"id": 42, "role": "editor"}

    async def downstream(scope, receive, send):
        assert catalogue_security.current_actor() == user
        raise RuntimeError("response failed")

    with pytest.raises(RuntimeError, match="response failed"):
        await CatalogueActorMiddleware(downstream)(
            {"type": "http", "state": {"user": user}}, None, None
        )
    assert catalogue_security.current_actor() is None


@pytest.mark.asyncio
async def test_concurrent_actors_do_not_mix_or_promote_bearer_capabilities():
    from app.main import CatalogueActorMiddleware
    from app.services import catalogue_security

    users = [
        {"id": 43, "role": "editor"},
        {"id": 44, "role": "viewer", "account_role": "admin", "api_token_id": 7},
    ]
    ready = [asyncio.Event(), asyncio.Event()]

    async def downstream(scope, receive, send):
        index = scope["index"]
        ready[index].set()
        await ready[1 - index].wait()
        assert catalogue_security.current_actor() == users[index]
        await asyncio.sleep(0)
        assert catalogue_security.current_actor() == users[index]

    middleware = CatalogueActorMiddleware(downstream)
    await asyncio.gather(*(
        middleware({"type": "http", "index": index, "state": {"user": user}}, None, None)
        for index, user in enumerate(users)
    ))
    assert catalogue_security.current_actor() is None


@pytest.mark.asyncio
async def test_unauthenticated_setup_does_not_manufacture_an_actor():
    from app.main import CatalogueActorMiddleware
    from app.services import catalogue_security

    calls = []

    async def downstream(scope, receive, send):
        calls.append(scope["path"])
        assert catalogue_security.current_actor() is None

    await CatalogueActorMiddleware(downstream)(
        {"type": "http", "path": "/setup", "state": {"user": None}}, None, None
    )
    assert calls == ["/setup"]
    assert catalogue_security.current_actor() is None
