"""Admin password verification and opaque server-side sessions."""

import asyncio
from collections import deque
from datetime import timedelta
from hashlib import sha256
from secrets import compare_digest, token_urlsafe
from time import monotonic
from urllib.parse import parse_qs

from argon2 import PasswordHasher, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type
from fastapi import HTTPException, Request
from sqlalchemy import delete, select

from telegram_ai_newsroom.domain.clock import utc_timestamp
from telegram_ai_newsroom.web.store import EditorStore, sessions

SESSION_COOKIE = "newsroom_session"
LOGIN_COOKIE = "newsroom_login"
SESSION_SECONDS = 8 * 3600
HASHER = PasswordHasher()


def validate_admin_hash(value: str) -> None:
    args = extract_parameters(value)
    if (
        args.type != Type.ID
        or not 19_456 <= args.memory_cost <= 262_144
        or not 2 <= args.time_cost <= 8
    ):
        raise ValueError("Configure an Argon2id hash with supported memory and time costs")
    if not 1 <= args.parallelism <= 8:
        raise ValueError("Argon2 parallelism must be between 1 and 8")


def matches(left: str, right: str) -> bool:
    return bool(left and right) and compare_digest(left.encode(), right.encode())


def token_hash(token: str) -> str:
    return sha256(token.encode()).hexdigest()


async def form_data(request: Request) -> dict[str, str]:
    mime = request.headers.get("content-type", "").split(";", 1)[0].strip()
    if mime != "application/x-www-form-urlencoded":
        raise HTTPException(415, "Expected an HTML form")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 24_576:
            raise HTTPException(413, "Form is too large")
    try:
        fields = parse_qs(data.decode(), keep_blank_values=True, max_num_fields=20, errors="strict")
    except ValueError as exc:
        raise HTTPException(400, "Invalid form") from exc
    if any(len(values) != 1 for values in fields.values()):
        raise HTTPException(400, "Duplicate form fields")
    return {name: values[0] for name, values in fields.items()}


class AdminAuth:
    def __init__(self, store: EditorStore, password_hash: str) -> None:
        validate_admin_hash(password_hash)
        self.store = store
        self.password_hash = password_hash
        self.attempts: deque[float] = deque()
        self.hash_slots = asyncio.Semaphore(2)

    async def verify(self, password: str) -> bool:
        if len(password.encode()) > 1024:
            return False
        now = monotonic()
        while self.attempts and now - self.attempts[0] >= 60:
            self.attempts.popleft()
        if len(self.attempts) >= 8:
            raise HTTPException(429, "Too many sign-in attempts. Try again in a minute.")
        self.attempts.append(now)
        async with self.hash_slots:
            try:
                return await asyncio.to_thread(HASHER.verify, self.password_hash, password)
            except (VerificationError, InvalidHashError):
                return False

    async def start(self, previous: str) -> str:
        token, csrf = token_urlsafe(32), token_urlsafe(32)
        now = self.store.clock.now()
        async with self.store.db.begin() as db:
            await db.execute(delete(sessions).where(sessions.c.expires_at <= utc_timestamp(now)))
            if previous:
                await db.execute(
                    delete(sessions).where(sessions.c.token_hash == token_hash(previous))
                )
            await db.execute(
                sessions.insert().values(
                    token_hash=token_hash(token),
                    csrf=csrf,
                    expires_at=utc_timestamp(now + timedelta(seconds=SESSION_SECONDS)),
                )
            )
        return token

    async def csrf(self, token: str) -> str | None:
        if len(token) != 43:
            return None
        async with self.store.db() as db:
            value = await db.scalar(
                select(sessions.c.csrf).where(
                    sessions.c.token_hash == token_hash(token),
                    sessions.c.expires_at > utc_timestamp(self.store.clock.now()),
                )
            )
            return str(value) if value else None

    async def end(self, token: str) -> None:
        async with self.store.db.begin() as db:
            await db.execute(delete(sessions).where(sessions.c.token_hash == token_hash(token)))
