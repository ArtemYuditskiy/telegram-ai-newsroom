"""Collect approved feeds and produce normalized editorial candidates."""

import hashlib
import re
from calendar import timegm
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import feedparser  # type: ignore[import-untyped]
from trafilatura import extract

from telegram_ai_newsroom.feed_http import FeedHttp, FetchError, Response, checked_host

FEED_MIME = {"application/rss+xml", "application/atom+xml", "application/xml", "text/xml"}
HTML_MIME = {"text/html", "application/xhtml+xml"}
MEDIA_MIME = {"image/jpeg", "image/png", "image/webp"}


class Http(Protocol):
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
    ) -> Response: ...


@dataclass(frozen=True)
class Source:
    url: str
    hosts: frozenset[str]

    def __post_init__(self) -> None:
        if checked_host(self.url) not in self.hosts:
            raise FetchError("unapproved_feed_host")


@dataclass(frozen=True)
class Entry:
    key: str
    url: str
    title: str
    published_at: str | None
    image_url: str | None


@dataclass(frozen=True)
class Article:
    url: str
    text: str
    image_url: str | None

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(" ".join(self.text.casefold().split()).encode()).hexdigest()


def canonical_url(url: str) -> str:
    host = checked_host(url)
    parsed = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}
    ]
    return urlunsplit(("https", host, parsed.path or "/", urlencode(query), ""))


def normalized_title(title: str) -> str:
    return " ".join(re.findall(r"\w+", title.casefold()))


def similar_title(left: str, right: str) -> bool:
    return (
        min(len(left), len(right)) >= 25
        and SequenceMatcher(None, left, right, autojunk=False).ratio() >= 0.94
    )


def parse_feed(response: Response) -> list[Entry]:
    feed = feedparser.parse(response.body, response_headers={"content-location": response.url})
    if not feed.version or not feed.entries:
        raise FetchError("invalid_or_empty_feed")
    entries = []
    for row in feed.entries[:100]:
        link = str(row.get("link", ""))
        title = " ".join(str(row.get("title", "")).split())[:500]
        key = str(row.get("id") or link)
        if not key:
            key = hashlib.sha256(repr(dict(row)).encode()).hexdigest()
        raw_time = row.get("published_parsed") or row.get("updated_parsed")
        try:
            published = (
                datetime.fromtimestamp(timegm(raw_time), UTC).isoformat() if raw_time else None
            )
        except (ValueError, OverflowError, OSError):
            published = None
        media = [
            *row.get("media_content", []),
            *row.get("media_thumbnail", []),
            *row.get("enclosures", []),
        ]
        image = next(
            (
                str(item.get("url") or item.get("href"))
                for item in media
                if (item.get("url") or item.get("href"))
                and (not item.get("type") or str(item["type"]).startswith("image/"))
            ),
            None,
        )
        entries.append(
            Entry(
                key,
                urljoin(response.url, link) if link else "",
                title,
                published,
                urljoin(response.url, image) if image else None,
            )
        )
    return entries


class Collector:
    def __init__(self, http: Http) -> None:
        self.http = http
        self.robots: dict[str, RobotFileParser] = {}

    async def verify(self, source: Source) -> Article:
        entries = parse_feed(await self.feed(source))
        reasons = []
        for entry in entries[:5]:
            try:
                return await self.article(source, entry)
            except FetchError as exc:
                reasons.append(exc.code)
        raise FetchError(f"sample_failed: {', '.join(reasons)}")

    async def check_robots(self, url: str, source: Source) -> None:
        host = checked_host(url)
        if host not in source.hosts:
            raise FetchError("unapproved_host")
        if host not in self.robots:
            response = await self.http.fetch(
                f"https://{host}/robots.txt",
                frozenset({host}),
                mime={"text/plain"},
                limit=512_000,
                allow_missing=True,
            )
            rules = RobotFileParser()
            if response.status in {404, 410}:
                rules.parse([])
            elif response.status == 200:
                rules.parse(response.body.decode("utf-8", errors="replace").splitlines())
            else:
                raise FetchError("robots_unavailable")
            self.robots[host] = rules
        if not self.robots[host].can_fetch(FeedHttp.agent, url):
            raise FetchError("robots_disallowed")

    async def feed(self, source: Source, headers: dict[str, str] | None = None) -> Response:
        await self.check_robots(source.url, source)
        return await self.http.fetch(
            source.url,
            source.hosts,
            mime=FEED_MIME,
            limit=2_000_000,
            headers=headers,
            allow_redirects=False,
        )

    async def article(self, source: Source, entry: Entry) -> Article:
        if not entry.title or not entry.url:
            raise FetchError("missing_title_or_url")
        if entry.published_at is None:
            raise FetchError("missing_publication_date")
        url = canonical_url(entry.url)
        await self.check_robots(url, source)
        response = await self.http.fetch(
            url,
            source.hosts,
            mime=HTML_MIME,
            limit=3_000_000,
            allow_redirects=False,
        )
        if response.status != 200:
            raise FetchError("article_unavailable")
        text = extract(response.body, url=response.url, favor_precision=True)
        if text is None or len(text.split()) < 80:
            raise FetchError("insufficient_article_text")
        image = None
        if entry.image_url:
            try:
                await self.check_robots(entry.image_url, source)
                media = await self.http.fetch(
                    entry.image_url,
                    source.hosts,
                    mime=MEDIA_MIME,
                    limit=5_000_000,
                    method="HEAD",
                    allow_redirects=False,
                )
                if media.status == 200:
                    image = entry.image_url
            except FetchError:
                pass
        return Article(canonical_url(response.url), text, image)
