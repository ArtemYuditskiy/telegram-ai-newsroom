"""Exercise the transport policy without external network calls."""

import asyncio
import socket
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from multidict import CIMultiDict

from telegram_ai_newsroom.feed_http import FeedHttp, FetchError, PublicResolver

URL = "https://news.example.com/feed"
HOSTS = frozenset({"news.example.com"})
MIME = {"application/xml"}


def reply(
    status: int = 200,
    *,
    headers: dict[str, str] | None = None,
    chunks: tuple[bytes, ...] = (b"<rss/>",),
    mime: str = "application/xml",
    length: int | None = None,
) -> MagicMock:
    async def stream(size: int) -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk

    response = MagicMock()
    response.status = status
    response.headers = CIMultiDict(headers or {})
    response.content_type = mime
    response.content_length = length
    response.content.iter_chunked = stream
    context = MagicMock()
    context.__aenter__.return_value = response
    return context


@pytest.mark.parametrize(
    ("response", "reason", "retry"),
    [
        (reply(503), "http_503", True),
        (reply(403), "http_403", False),
        (reply(302), "invalid_redirect", False),
        (reply(302, headers={"Location": "https://other.example.com/"}), "unapproved_host", False),
        (reply(mime="text/html"), "unexpected_content_type", False),
        (reply(headers={"Content-Encoding": "gzip"}), "unsupported_content_encoding", False),
        (reply(length=20), "invalid_content_length", False),
        (reply(chunks=(b"12345678901",)), "response_too_large", False),
    ],
)
def test_response_policy(response: MagicMock, reason: str, retry: bool) -> None:
    session = MagicMock(spec=aiohttp.ClientSession)
    session.request.return_value = response
    with pytest.raises(FetchError, match=reason) as exc:
        asyncio.run(FeedHttp(session).fetch(URL, HOSTS, mime=MIME, limit=10))
    assert exc.value.retryable is retry


def test_redirect_head_and_conditional_statuses() -> None:
    session = MagicMock(spec=aiohttp.ClientSession)
    redirect = reply(302, headers={"Location": "/new"})
    session.request.side_effect = [
        redirect,
        reply(),
        reply(304),
        reply(404),
        reply(length=6),
        redirect,
        reply(),
    ]

    async def exercise() -> None:
        http = FeedHttp(session)
        first = await http.fetch(URL, HOSTS, mime=MIME, limit=10, headers={"If-None-Match": "v1"})
        assert first.url.endswith("/new") and first.body == b"<rss/>"
        assert session.request.call_args_list[1].kwargs["headers"] is None
        assert (await http.fetch(URL, HOSTS, mime=MIME, limit=10)).status == 304
        assert (await http.fetch(URL, HOSTS, mime=MIME, limit=10, allow_missing=True)).status == 404
        assert (await http.fetch(URL, HOSTS, mime=MIME, limit=10, method="HEAD")).body == b""
        with pytest.raises(FetchError, match="redirect_requires_onboarding"):
            await http.fetch(URL, HOSTS, mime=MIME, limit=10, allow_redirects=False)
        with pytest.raises(FetchError, match="invalid_content_length"):
            await http.fetch(URL, HOSTS, mime=MIME, limit=10, method="HEAD")

    asyncio.run(exercise())


def test_redirect_limit_and_network_error() -> None:
    session = MagicMock(spec=aiohttp.ClientSession)
    session.request.return_value = reply(302, headers={"Location": "/new"})
    with pytest.raises(FetchError, match="too_many_redirects"):
        asyncio.run(FeedHttp(session).fetch(URL, HOSTS, mime=MIME, limit=10))
    session.request.side_effect = aiohttp.ClientConnectionError()
    with pytest.raises(FetchError, match="network_error") as exc:
        asyncio.run(FeedHttp(session).fetch(URL, HOSTS, mime=MIME, limit=10))
    assert exc.value.retryable


def test_resolver_returns_only_checked_public_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    async def exercise() -> None:
        resolver = PublicResolver()
        resolve = AsyncMock(
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]
        )
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
        assert (await resolver.resolve("news.example.com", 443))[0]["host"] == "1.1.1.1"
        resolve.return_value.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)))
        with pytest.raises(OSError, match="not a public"):
            await resolver.resolve("news.example.com", 443)
        await resolver.close()

    asyncio.run(exercise())
