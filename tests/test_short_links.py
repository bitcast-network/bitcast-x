"""Short-link resolution never follows a destination or uses live HTTP."""

import json
from pathlib import Path

import httpx
import pytest

from bitcast_x import short_links
from bitcast_x.short_links import ShortLinkResolutionError, ShortLinkResolver


@pytest.mark.parametrize("status", [301, 302, 307, 308])
@pytest.mark.parametrize("destination", ["https://example.com/path", "http://example.com/path"])
async def test_resolves_one_head_without_following_redirects(
    tmp_path: Path, status: int, destination: str
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "HEAD"
        assert str(request.url) == "https://t.co/Ab123"
        assert request.extensions["timeout"] == dict.fromkeys(
            ("connect", "read", "write", "pool"), 2.5
        )
        return httpx.Response(status, headers={"Location": destination})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        resolver = ShortLinkResolver(tmp_path / "cache.json", 2.5, client=client)
        assert await resolver.resolve("https://t.co/Ab123") == destination
        assert await resolver.resolve("https://t.co/Ab123") == destination
        await resolver.close()
        assert not client.is_closed
    assert len(requests) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://t.co/abc",
        "https://T.CO/abc",
        "https://t.co/",
        "https://t.co/abc/",
        "https://t.co/abc?query=1",
        "https://t.co/abc#fragment",
        "https://t.co/abc_def",
        "https://t.co/abc-def",
        "https://t.co/é",
        "https://t.co/abc\n",
        " https://t.co/abc",
        "https://t.co:443/abc",
        "https://user@t.co/abc",
        "https://t.co.evil.test/abc",
        "https://evil.test/t.co/abc",
        "https://127.0.0.1/abc",
        "https://t.co/%61bc",
        "//t.co/abc",
    ],
)
async def test_unsafe_inputs_never_make_a_request(tmp_path: Path, url: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        pytest.fail("unsafe URL reached HTTP transport")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolver = ShortLinkResolver(tmp_path / "cache.json", 1, client=client)
        with pytest.raises(ShortLinkResolutionError):
            await resolver.resolve(url)
    assert not (tmp_path / "cache.json").exists()


@pytest.mark.parametrize(
    ("status", "location"),
    [
        (200, "https://example.com"),
        (303, "https://example.com"),
        (404, None),
        (429, None),
        (500, None),
        (302, None),
        (302, ""),
        (302, "/relative"),
        (302, "//example.com/path"),
        (302, "https:/example.com/path"),
        (302, "ftp://example.com/path"),
        (302, "javascript:alert(1)"),
        (302, "http://[invalid"),
    ],
)
async def test_invalid_redirects_are_errors_not_cached_or_retried(
    tmp_path: Path, status: int, location: str | None
) -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(status, headers={} if location is None else {"Location": location})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolver = ShortLinkResolver(tmp_path / "cache.json", 1, client=client)
        for _ in range(2):
            with pytest.raises(ShortLinkResolutionError):
                await resolver.resolve("https://t.co/abc")
    assert requests == 2
    assert not (tmp_path / "cache.json").exists()


@pytest.mark.parametrize(
    "error_type", [httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout]
)
async def test_transport_failures_are_dedicated_errors(
    tmp_path: Path, error_type: type[httpx.RequestError]
) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        raise error_type("unavailable", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolver = ShortLinkResolver(tmp_path / "cache.json", 1, client=client)
        with pytest.raises(ShortLinkResolutionError) as raised:
            await resolver.resolve("https://t.co/abc")
    assert isinstance(raised.value.__cause__, error_type)
    assert requests == 1
    assert not (tmp_path / "cache.json").exists()


async def test_cache_persists_atomically_across_instances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "nested" / "cache.json"
    replacements: list[tuple[Path, Path]] = []
    replace = short_links.os.replace

    def record_replace(source: Path, destination: Path) -> None:
        replacements.append((source, destination))
        assert json.loads(source.read_text()) == {"https://t.co/abc": "https://example.com"}
        replace(source, destination)

    monkeypatch.setattr(short_links.os, "replace", record_replace)
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(302, headers={"Location": "https://example.com"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = ShortLinkResolver(path, 1, client=client)
        assert await first.resolve("https://t.co/abc") == "https://example.com"
        second = ShortLinkResolver(path, 1, client=client)
        assert await second.resolve("https://t.co/abc") == "https://example.com"
    assert requests == 1
    assert replacements == [(path.with_suffix(".json.tmp"), path)]
    assert not path.with_suffix(".json.tmp").exists()


@pytest.mark.parametrize(
    "content",
    [
        b"broken JSON",
        b"\xff",
        b"[]",
        b"null",
        b'{"https://t.co/abc": 123}',
        b'{"https://evil.test/abc": "https://example.com"}',
        b'{"https://t.co/abc": "/relative"}',
    ],
)
async def test_corrupt_cache_warns_and_starts_empty(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, content: bytes
) -> None:
    path = tmp_path / "cache.json"
    path.write_bytes(content)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://example.com"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolver = ShortLinkResolver(path, 1, client=client)
        assert "discarding unreadable short-link cache" in caplog.text
        assert await resolver.resolve("https://t.co/abc") == "https://example.com"
    assert json.loads(path.read_text()) == {"https://t.co/abc": "https://example.com"}


async def test_cache_cap_applies_to_loaded_and_new_entries(tmp_path: Path) -> None:
    path = tmp_path / "cache.json"
    path.write_text(
        json.dumps({f"https://t.co/{index}": f"https://example.com/{index}" for index in range(4)})
    )
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": f"https://example.com{request.url.path}"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolver = ShortLinkResolver(path, 1, client=client, max_entries=2)
        assert await resolver.resolve("https://t.co/2") == "https://example.com/2"
        assert await resolver.resolve("https://t.co/3") == "https://example.com/3"
        assert requests == []
        await resolver.resolve("https://t.co/4")
        assert list(json.loads(path.read_text())) == ["https://t.co/3", "https://t.co/4"]
        await resolver.resolve("https://t.co/2")
        assert list(json.loads(path.read_text())) == ["https://t.co/4", "https://t.co/2"]
    assert requests == ["https://t.co/4", "https://t.co/2"]


def test_cache_cap_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_entries"):
        ShortLinkResolver(tmp_path / "cache.json", 1, max_entries=0)
