"""Small-form streaming limits without consuming an unbounded request body."""

from starlette.requests import Request


METADATA_FORM_BYTES = 4096


class BodyTooLarge(ValueError):
    pass


async def bounded_body(request: Request, limit: int) -> bytes:
    """Cache a bounded body for normal form parsing, stopping before its tail.

    Starlette's Request.body() buffers first and can only be checked afterwards.
    Caching the accepted bytes here preserves the same replay/form contract.
    """
    cached = getattr(request, "_body", None)
    if cached is not None:
        if len(cached) > limit:
            raise BodyTooLarge
        return cached
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise BodyTooLarge
        body.extend(chunk)
    request._body = bytes(body)
    return request._body
