"""Bounded HTTP requests to explicitly configured public hosts."""

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult


class FetchError(ValueError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def checked_host(url: str) -> str:
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
            or not parsed.hostname.isascii()
        ):
            raise ValueError
        host = parsed.hostname.lower()
    except ValueError as exc:
        raise FetchError("invalid_url") from exc
    # aiohttp bypasses DNS for address literals. Require DNS names.
    if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]*", host):
        raise FetchError("invalid_host")
    return host


class PublicResolver(AbstractResolver):
    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_UNSPEC
    ) -> list[ResolveResult]:
        records = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=family, type=socket.SOCK_STREAM
        )
        results: list[ResolveResult] = []
        for record_family, _, proto, _, address in records:
            ip = ipaddress.ip_address(address[0])
            if not ip.is_global or ip.is_multicast:
                raise OSError("DNS answer is not a public unicast address")
            results.append(
                ResolveResult(
                    hostname=host,
                    host=str(ip),
                    port=port,
                    family=record_family,
                    proto=proto,
                    flags=socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
                )
            )
        return results

    async def close(self) -> None:
        pass


@dataclass(frozen=True)
class Response:
    status: int
    url: str
    headers: dict[str, str]
    body: bytes


class FeedHttp:
    agent = "TelegramAINewsroom"

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session

    @staticmethod
    def session_context() -> aiohttp.ClientSession:
        return aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(resolver=PublicResolver(), ttl_dns_cache=60),
            timeout=aiohttp.ClientTimeout(total=15, connect=5),
            trust_env=False,
            auto_decompress=False,
            headers={"User-Agent": FeedHttp.agent, "Accept-Encoding": "identity"},
        )

    async def fetch(
        self,
        url: str,
        hosts: frozenset[str],
        *,
        mime: set[str],
        limit: int,
        headers: dict[str, str] | None = None,
        method: str = "GET",
        allow_missing: bool = False,
        allow_redirects: bool = True,
    ) -> Response:
        try:
            async with asyncio.timeout(20):
                for _ in range(5):
                    if checked_host(url) not in hosts:
                        raise FetchError("unapproved_host")
                    async with self.session.request(
                        method, url, headers=headers, allow_redirects=False
                    ) as response:
                        status = response.status
                        if status in {301, 302, 303, 307, 308}:
                            if not allow_redirects:
                                raise FetchError("redirect_requires_onboarding")
                            location = response.headers.get("Location")
                            if not location:
                                raise FetchError("invalid_redirect")
                            url = urljoin(url, location)
                            headers = None
                            continue
                        metadata = {key.lower(): value for key, value in response.headers.items()}
                        if status == 304 or (allow_missing and status in {404, 410}):
                            return Response(status, url, metadata, b"")
                        if status != 200:
                            raise FetchError(
                                f"http_{status}", retryable=status in {408, 429} or status >= 500
                            )
                        if response.content_type not in mime:
                            raise FetchError("unexpected_content_type")
                        if response.headers.get("Content-Encoding", "identity") != "identity":
                            raise FetchError("unsupported_content_encoding")
                        size = response.content_length
                        if (size is None and method == "HEAD") or (
                            size is not None and size > limit
                        ):
                            raise FetchError("invalid_content_length")
                        body = bytearray()
                        if method != "HEAD":
                            async for chunk in response.content.iter_chunked(32_768):
                                body.extend(chunk)
                                if len(body) > limit:
                                    raise FetchError("response_too_large")
                        return Response(status, url, metadata, bytes(body))
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise FetchError("network_error", retryable=True) from exc
        raise FetchError("too_many_redirects")
