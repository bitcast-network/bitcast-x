"""Bounded HTTP response reading shared by every outbound client."""

import httpx

from bitcast_x.errors import ResponseTooLargeError


async def read_bounded(response: httpx.Response, limit: int, *, source: str) -> bytes:
    """Read a streamed response body, failing as soon as it exceeds ``limit`` bytes."""

    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > limit:
            raise ResponseTooLargeError(f"{source} response exceeds configured byte limit")
        chunks.append(chunk)
    return b"".join(chunks)
