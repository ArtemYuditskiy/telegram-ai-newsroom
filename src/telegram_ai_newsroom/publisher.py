"""Build Telegram messages and classify the outcome of each Bot API call."""

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import timedelta
from io import BytesIO
from typing import Protocol

import httpx
from PIL import Image
from telegram import Bot, InputFile, LinkPreviewOptions, Message, MessageEntity
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError

from telegram_ai_newsroom.delivery_store import Delivery
from telegram_ai_newsroom.domain.workflow import PublicationState
from telegram_ai_newsroom.feed_http import FeedHttp, FetchError, checked_host
from telegram_ai_newsroom.ingestion import MEDIA_MIME, Collector, Source

CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096
# Only explicit rejection permits switching transport without risking a second post.
URL_REJECTIONS = {
    "failed to get http url content",
    "wrong type of the web page content",
    "wrong file identifier/http url specified",
    "webpage_media_empty",
}
PHOTO_REJECTIONS = {"photo_invalid_dimensions", "image_process_failed", "photo_ext_invalid"}
IMAGE_FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}


class DeliveryError(Exception):
    def __init__(self, state: PublicationState, code: str, retry_after: float = 0) -> None:
        super().__init__(code)
        self.state = state
        self.code = code
        self.retry_after = retry_after


class Publisher(Protocol):
    async def send(self, job: Delivery) -> int: ...


@dataclass(frozen=True, slots=True)
class Payload:
    text: str
    entities: tuple[MessageEntity, ...]
    length: int


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def build_payload(job: Delivery) -> Payload:
    text = job.text.strip()
    if not text:
        raise DeliveryError(PublicationState.FAILED, "empty_text")
    headline, separator, _ = text.partition("\n\n")
    entities = []
    if separator:
        entities.append(MessageEntity(MessageEntity.BOLD, 0, utf16_length(headline)))
    if job.source_url:
        try:
            checked_host(job.source_url)
        except FetchError:
            pass
        else:
            label = "Source article"
            text += "\n\n"
            entities.append(
                MessageEntity(
                    MessageEntity.TEXT_LINK, utf16_length(text), len(label), url=job.source_url
                )
            )
            text += label
    length = utf16_length(text)
    if length > TEXT_LIMIT:
        raise DeliveryError(PublicationState.FAILED, "message_too_long")
    return Payload(text, tuple(entities), length)


def valid_photo(data: bytes, mime: str) -> bool:
    try:
        with Image.open(BytesIO(data), formats=(IMAGE_FORMATS[mime],)) as image:
            width, height = image.size
            if (
                image.format != IMAGE_FORMATS[mime]
                or min(width, height) <= 0
                or width + height > 10_000
                or max(width, height) > 20 * min(width, height)
            ):
                return False
            image.verify()
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        return False
    return True


async def download_photo(url: str, hosts: frozenset[str]) -> InputFile | None:
    try:
        async with FeedHttp.session_context() as session:
            http = FeedHttp(session)
            await Collector(http).check_robots(url, Source(url, hosts))
            media = await http.fetch(
                url, hosts, mime=MEDIA_MIME, limit=5_000_000, allow_redirects=False
            )
        mime = media.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if (
            media.status == 200
            and mime in IMAGE_FORMATS
            and await asyncio.to_thread(valid_photo, media.body, mime)
        ):
            return InputFile(media.body, filename=f"photo.{mime.split('/')[1]}")
    except FetchError:
        pass
    return None


class FakePublisher:
    async def send(self, job: Delivery) -> int:
        build_payload(job)
        # Stable across process restarts, and isolated from actual Telegram receipts.
        return 1 + (job.id.int % (2**31 - 1))


class TelegramPublisher:
    def __init__(self, bot: Bot, chat_id: int) -> None:
        self.bot = bot
        self.chat_id = chat_id

    async def send(self, job: Delivery) -> int:
        payload = build_payload(job)
        try:
            image_allowed = bool(job.image_url and checked_host(job.image_url) in job.image_hosts)
        except FetchError:
            image_allowed = False
        if job.image_url and image_allowed and payload.length <= CAPTION_LIMIT:
            try:
                return await self._photo(job.image_url, payload)
            except BadRequest as exc:
                reason = str(exc).lower()
                if reason not in URL_REJECTIONS | PHOTO_REJECTIONS:
                    raise DeliveryError(PublicationState.FAILED, "telegram_rejected") from exc
                if reason in URL_REJECTIONS:
                    photo = await download_photo(job.image_url, job.image_hosts)
                    if photo is not None:
                        try:
                            return await self._photo(photo, payload)
                        except BadRequest as upload_error:
                            if str(upload_error).lower() not in PHOTO_REJECTIONS:
                                raise DeliveryError(
                                    PublicationState.FAILED, "telegram_rejected"
                                ) from upload_error
        try:
            return await self._send(
                self.bot.send_message(
                    self.chat_id,
                    payload.text,
                    entities=payload.entities,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            )
        except BadRequest as exc:
            raise DeliveryError(PublicationState.FAILED, "telegram_rejected") from exc

    async def _photo(self, photo: str | InputFile, payload: Payload) -> int:
        return await self._send(
            self.bot.send_photo(
                self.chat_id, photo, caption=payload.text, caption_entities=payload.entities
            )
        )

    async def _send(self, request: Awaitable[Message]) -> int:
        try:
            response = await request
        except RetryAfter as exc:
            delay = exc.retry_after
            seconds = delay.total_seconds() if isinstance(delay, timedelta) else float(delay)
            raise DeliveryError(PublicationState.RETRY_WAIT, "rate_limited", seconds) from exc
        except BadRequest:
            raise
        except NetworkError as exc:
            if isinstance(
                exc.__cause__, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
            ):
                raise DeliveryError(PublicationState.RETRY_WAIT, "connect_failed") from exc
            raise DeliveryError(PublicationState.UNCERTAIN, "telegram_response_unknown") from exc
        except TelegramError as exc:
            raise DeliveryError(PublicationState.FAILED, "telegram_rejected") from exc
        if response.message_id <= 0:
            raise DeliveryError(PublicationState.UNCERTAIN, "telegram_receipt_missing")
        return response.message_id
