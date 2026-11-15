"""Resolve only X short links without ever fetching their destinations."""

import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx

LOGGER = logging.getLogger(__name__)
_SHORT_LINK = re.compile(r"https://t\.co/[A-Za-z0-9]+")
_REDIRECT_STATUSES = {301, 302, 307, 308}


class ShortLinkResolutionError(Exception):
    """A short link could not be safely resolved into a destination."""


def _is_destination(value: str) -> bool:
    try:
        url = urlsplit(value)
        return url.scheme in {"http", "https"} and bool(url.hostname)
    except ValueError:
        return False


class ShortLinkResolver:
    """Perform one HEAD per uncached t.co URL and persist bounded successful results."""

    def __init__(
        self,
        cache_path: Path,
        timeout_seconds: float,
        *,
        client: httpx.AsyncClient | None = None,
        max_entries: int = 50_000,
    ) -> None:
        if max_entries <= 0:
            raise ValueError("short-link cache max_entries must be positive")
        self.cache_path = cache_path
        self._timeout_seconds = timeout_seconds
        self._max_entries = max_entries
        self._cache = self._read_cache()
        self._owns_client = client is None
        self._client = client if client is not None else httpx.AsyncClient(trust_env=False)

    async def close(self) -> None:
        """Close only an internally owned HTTP pool."""

        if self._owns_client:
            await self._client.aclose()

    async def resolve(self, url: str) -> str:
        """Return the absolute redirect target; reject all non-canonical input URLs."""

        if _SHORT_LINK.fullmatch(url) is None:
            raise ShortLinkResolutionError("only https://t.co/<alphanumeric> URLs can be resolved")
        cached = self._cache.get(url)
        if cached is not None:
            return cached
        try:
            response = await self._client.head(
                url, follow_redirects=False, timeout=self._timeout_seconds
            )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise ShortLinkResolutionError(f"short-link request failed: {url}") from exc
        destination: str | None = response.headers.get("location")
        if (
            response.status_code not in _REDIRECT_STATUSES
            or destination is None
            or not _is_destination(destination)
        ):
            raise ShortLinkResolutionError(f"short link did not return an HTTP(S) redirect: {url}")
        self._cache[url] = destination
        self._prune_cache()
        self._write_cache()
        return destination

    def _prune_cache(self) -> None:
        while len(self._cache) > self._max_entries:
            self._cache.pop(next(iter(self._cache)))

    def _read_cache(self) -> dict[str, str]:
        try:
            payload = json.loads(self.cache_path.read_bytes())
            if not isinstance(payload, dict) or any(
                not isinstance(key, str)
                or _SHORT_LINK.fullmatch(key) is None
                or not isinstance(value, str)
                or not _is_destination(value)
                for key, value in payload.items()
            ):
                raise ValueError("invalid short-link cache")
        except FileNotFoundError:
            return {}
        except (ValueError, OSError):
            LOGGER.warning("discarding unreadable short-link cache path=%s", self.cache_path)
            return {}
        while len(payload) > self._max_entries:
            payload.pop(next(iter(payload)))
        return dict(payload)

    def _write_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self._cache), encoding="utf-8")
        os.replace(temporary, self.cache_path)
