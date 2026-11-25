"""FastAPI application factory for the editorial workspace."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_urlsafe
from time import perf_counter
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound, OperationalError, SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import RequestResponseEndpoint

from telegram_ai_newsroom.cli.editorial import EditorialConfig, execute, provider
from telegram_ai_newsroom.domain.workflow import DraftState, InvalidTransition
from telegram_ai_newsroom.editorial import (
    Audit,
    EditorialPipeline,
    OutputSchemaError,
    PostText,
    Review,
)
from telegram_ai_newsroom.editorial_store import EditorialRepository
from telegram_ai_newsroom.feed_http import FeedHttp, FetchError, checked_host
from telegram_ai_newsroom.ingestion import Collector, Source
from telegram_ai_newsroom.ingestion_store import IngestionStore, collect_source
from telegram_ai_newsroom.llm import ModelError, Operation
from telegram_ai_newsroom.sqlite import open_database, publications
from telegram_ai_newsroom.telemetry import event
from telegram_ai_newsroom.web.auth import (
    LOGIN_COOKIE,
    SESSION_COOKIE,
    SESSION_SECONDS,
    AdminAuth,
    form_data,
    matches,
)
from telegram_ai_newsroom.web.i18n import translator
from telegram_ai_newsroom.web.store import EditorStore

HERE = Path(__file__).parent
logger = logging.getLogger("uvicorn.error.newsroom")
LANGUAGE_COOKIE = "newsroom_language"
LANGUAGES = {"en": "English", "ru": "Русский"}
FILTERS = {
    "all": "All candidates",
    "review": "Needs review",
    "eligible": "Ready to score",
    "drafted": "Drafted",
    "rejected": "Passed over",
}


def local_due_at(value: str, zone: str, now: datetime) -> datetime:
    """Resolve a wall-clock minute only if it maps to exactly one instant."""
    try:
        local = datetime.strptime(value, "%Y-%m-%dT%H:%M")
        tz = ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Enter a local date and time and a valid IANA time zone.") from exc
    instants = {
        aware.astimezone(UTC)
        for fold in (0, 1)
        if (aware := local.replace(tzinfo=tz, fold=fold))
        .astimezone(UTC)
        .astimezone(tz)
        .replace(tzinfo=None)
        == local
    }
    if len(instants) != 1:
        raise ValueError("This local time is ambiguous or does not exist. Choose another time.")
    due = instants.pop()
    if due <= now:
        raise ValueError("Choose a time in the future.")
    return due


def safe_url(value: str) -> str | None:
    try:
        checked_host(value)
    except FetchError:
        return None
    return value


def create_app(
    db_path: Path,
    admin_hash: str,
    *,
    secure_cookie: bool = True,
    timezone: str = "UTC",
    config: EditorialConfig | None = None,
    demo: bool = False,
) -> FastAPI:
    ZoneInfo(timezone)
    engine = open_database(db_path)
    store = EditorStore(engine)
    auth = AdminAuth(store, admin_hash)
    ingestion = IngestionStore(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.dispose()

    app = FastAPI(
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.mount("/assets", StaticFiles(directory=HERE / "static"), name="assets")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(safe_url=safe_url, filters=FILTERS)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        if db_path.is_file():
            try:
                async with engine.connect() as conn:
                    await conn.execute(select(publications.c.message_id).limit(0))
            except SQLAlchemyError:
                pass
            else:
                return JSONResponse({"status": "ok", "database": "ok"})
        return JSONResponse({"status": "unavailable", "database": "unavailable"}, 503)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = uuid4().hex
        started = perf_counter()
        status = 500
        try:
            response: Response = await call_next(request)
            status = response.status_code
        finally:
            route = getattr(request.scope.get("route"), "path", None)
            if status >= 400 or route not in {"/health/live", "/health/ready"}:
                event(
                    logger,
                    logging.INFO,
                    "http_request",
                    request_id=request_id,
                    method=request.method,
                    route=route,
                    status=status,
                    latency_ms=round((perf_counter() - started) * 1_000, 1),
                )
        response.headers["X-Request-ID"] = request_id
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; img-src 'self'; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if not request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def render(request: Request, template: str, status: int = 200, **data: Any) -> Response:
        selected = request.query_params.get("lang") if request.method == "GET" else None
        saved = request.cookies.get(LANGUAGE_COOKIE, "en")
        language = selected if selected in LANGUAGES else saved
        if language not in LANGUAGES:
            language = "en"

        def language_url(code: str) -> str:
            if request.method == "GET":
                return "?" + request.url.include_query_params(lang=code).query
            query = {"lang": code}
            if template == "candidate.html":
                draft = data["draft"]
                if draft:
                    query["revision"] = str(draft["id"])
                path = f"/candidates/{data['entry']['candidate_id']}"
            else:
                path = {"login.html": "/login", "sources.html": "/sources"}.get(template, "/")
            return f"{path}?{urlencode(query)}"

        response = templates.TemplateResponse(
            request,
            template,
            {
                "demo": demo,
                "language": language,
                "languages": LANGUAGES,
                "language_url": language_url,
                "t": translator(language),
                **data,
            },
            status,
        )
        response.headers["Content-Language"] = language
        if selected in LANGUAGES:
            cookie(response, LANGUAGE_COOKIE, language, 365 * 86400)
        return response

    def cookie(response: Response, name: str, value: str, age: int) -> None:
        response.set_cookie(
            name,
            value,
            max_age=age,
            secure=secure_cookie,
            httponly=True,
            samesite="strict",
            path="/",
        )

    def set_redirect(url: str, request: Request) -> Response:
        if request.headers.get("HX-Request") == "true":
            return Response(status_code=200, headers={"HX-Redirect": url})
        return RedirectResponse(url, status_code=303)

    async def session(request: Request) -> str:
        csrf = await auth.csrf(request.cookies.get(SESSION_COOKIE, ""))
        if csrf is None:
            raise HTTPException(401)
        return csrf

    async def authorized_form(request: Request) -> tuple[str, dict[str, str]]:
        csrf = await session(request)
        fields = await form_data(request)
        if not matches(fields.get("csrf", ""), csrf):
            raise HTTPException(403, "The form has expired. Reload this page and try again.")
        return csrf, fields

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == 401:
            return set_redirect("/login", request)
        return render(request, "error.html", exc.status_code, message=exc.detail)

    @app.exception_handler(OperationalError)
    async def database_error(request: Request, exc: OperationalError) -> Response:
        return render(
            request,
            "error.html",
            503,
            message="The database is unavailable. Check the migrations and try again.",
        )

    def show_login(request: Request, error: str = "", status: int = 200) -> Response:
        nonce = token_urlsafe(32)
        response = render(request, "login.html", status, nonce=nonce, error=error)
        cookie(response, LOGIN_COOKIE, nonce, 600)
        return response

    @app.get("/login")
    async def login_page(request: Request) -> Response:
        if await auth.csrf(request.cookies.get(SESSION_COOKIE, "")):
            return set_redirect("/", request)
        return show_login(request)

    @app.post("/login")
    async def login(request: Request) -> Response:
        data = await form_data(request)
        if not matches(data.get("csrf", ""), request.cookies.get(LOGIN_COOKIE, "")):
            return show_login(request, "The sign-in form expired. Please try again.", 403)
        if not await auth.verify(data.get("password", "")):
            return show_login(request, "The password was not accepted.", 401)
        token = await auth.start(request.cookies.get(SESSION_COOKIE, ""))
        response = set_redirect("/", request)
        cookie(response, SESSION_COOKIE, token, SESSION_SECONDS)
        response.delete_cookie(LOGIN_COOKIE, path="/", secure=secure_cookie)
        return response

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        await authorized_form(request)
        await auth.end(request.cookies.get(SESSION_COOKIE, ""))
        response = set_redirect("/login", request)
        response.delete_cookie(SESSION_COOKIE, path="/", secure=secure_cookie)
        return response

    @app.get("/")
    async def queue(request: Request, state: str = "all", page: int = 1) -> Response:
        csrf = await session(request)
        if state not in FILTERS or not 1 <= page <= 10000:
            raise HTTPException(400, "Invalid queue filter")
        rows, counts = await store.queue(state, page)
        partial = request.headers.get("HX-Request") == "true" and not request.headers.get(
            "HX-History-Restore-Request"
        )
        return render(
            request,
            "queue_items.html" if partial else "queue.html",
            csrf=csrf,
            rows=rows[:25],
            more=len(rows) > 25,
            counts=counts,
            state=state,
            page=page,
            section="queue",
        )

    async def detail(
        request: Request,
        identity: UUID,
        revision: UUID | None,
        csrf: str,
        *,
        error: str = "",
        values: dict[str, str] | None = None,
        status: int = 200,
    ) -> Response:
        try:
            item = await store.candidate(identity, revision)
        except (NoResultFound, LookupError) as exc:
            raise HTTPException(404, "Candidate or draft revision not found") from exc
        return render(
            request,
            "candidate.html",
            status,
            csrf=csrf,
            section="queue",
            **item,
            timezone=timezone,
            has_models=config is not None,
            error=error,
            values=values or {},
        )

    @app.get("/candidates/{identity}")
    async def candidate(request: Request, identity: UUID, revision: UUID | None = None) -> Response:
        return await detail(request, identity, revision, await session(request))

    @app.post("/candidates/{identity}/process")
    async def process(request: Request, identity: UUID) -> Response:
        csrf, _ = await authorized_form(request)
        if config is None:
            raise HTTPException(409, "A model profile is required to process candidates.")
        try:
            await execute(db_path, identity, config)
        except OutputSchemaError as exc:
            fields = ", ".join(f"{issue.field}: {issue.kind}" for issue in exc.issues)
            return await detail(
                request, identity, None, csrf, error=f"{exc.operation}: {fields}", status=422
            )
        except (ModelError, InvalidTransition) as exc:
            return await detail(request, identity, None, csrf, error=str(exc), status=409)
        return set_redirect(f"/candidates/{identity}", request)

    @app.post("/candidates/{identity}/revisions/{revision}/{action}")
    async def revise(request: Request, identity: UUID, revision: UUID, action: str) -> Response:
        csrf, data = await authorized_form(request)
        destination = f"/candidates/{identity}?revision={revision}"
        try:
            if action in {"approve", "reject"}:
                await store.decide(
                    identity,
                    revision,
                    DraftState.APPROVED if action == "approve" else DraftState.REJECTED,
                )
            elif action == "edit":
                post = PostText(headline=data.get("headline", ""), body=data.get("body", ""))
                if "\n" in post.headline or "\r" in post.headline:
                    raise ValueError("Headline must fit on one line.")
                new_id = await store.revise(identity, revision, post, "edited")
                destination = f"/candidates/{identity}?revision={new_id}"
            elif action == "schedule":
                due = local_due_at(data.get("due", ""), data.get("timezone", ""), store.clock.now())
                await store.schedule(identity, revision, due)
            elif action == "regenerate":
                if config is None:
                    raise ValueError("A model profile is required to regenerate a draft.")
                item = await store.candidate(identity, revision)
                entry = item["entry"]
                title, body = entry["title"], entry["text"]
                repo, run_id = EditorialRepository(engine), uuid4()
                profiles = config.profiles()

                async def audit(call: Audit) -> None:
                    await repo.audit(identity, run_id, call)

                async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                    stages: tuple[Operation, ...] = ("draft", "review")
                    providers = {stage: provider(profiles[stage], client) for stage in stages}
                    pipeline = EditorialPipeline(profiles, providers, audit, config.threshold)
                    post = await pipeline.generate_draft(title, body)
                    issue = pipeline.draft_issue(body, post)
                    review = (
                        Review(supported=False, issues=[issue])
                        if issue
                        else (await pipeline.review_draft(title, body, post.text))
                    )
                new_id = await store.revise(identity, revision, post, "regenerated", review, run_id)
                destination = f"/candidates/{identity}?revision={new_id}"
            else:
                raise HTTPException(404, "Unknown draft action")
        except (LookupError, NoResultFound) as exc:
            raise HTTPException(404, "Candidate or draft revision not found") from exc
        except ValidationError:
            return await detail(
                request,
                identity,
                revision,
                csrf,
                values=data,
                status=422,
                error="Enter a headline of at most 120 characters and a body of at most 700.",
            )
        except OutputSchemaError as exc:
            fields = ", ".join(f"{issue.field}: {issue.kind}" for issue in exc.issues)
            return await detail(
                request, identity, revision, csrf, error=f"{exc.operation}: {fields}", status=422
            )
        except ValueError as exc:
            return await detail(
                request, identity, revision, csrf, error=str(exc), status=409, values=data
            )
        return set_redirect(destination, request)

    @app.get("/sources")
    async def sources_page(request: Request) -> Response:
        csrf = await session(request)
        return render(
            request, "sources.html", csrf=csrf, section="sources", rows=await store.sources()
        )

    @app.post("/sources/{action}")
    async def update_sources(request: Request, action: str) -> Response:
        csrf, data = await authorized_form(request)
        counts = None
        try:
            if demo:
                raise ValueError("The demo uses a fixed local dataset.")
            if action == "approve":
                url = data.get("url", "").strip()
                hosts = {
                    host.strip().lower()
                    for host in data.get("hosts", "").split(",")
                    if host.strip()
                }
                hosts.add(checked_host(url))
                if len(hosts) > 12 or len(url) > 2048:
                    raise ValueError("Use a feed URL and at most 11 additional hostnames.")
                allowed = frozenset(checked_host(f"https://{host}") for host in hosts)
                if allowed != hosts:
                    raise ValueError("Enter hostnames without paths, ports or credentials.")
                source = Source(url, allowed)
                async with FeedHttp.session_context() as client:
                    await Collector(FeedHttp(client)).verify(source)
                await ingestion.approve(source)
            elif action in {"enable", "disable"}:
                await store.toggle_source(data.get("url", ""), action == "enable")
            elif action == "collect":
                async with FeedHttp.session_context() as client:
                    collector = Collector(FeedHttp(client))
                    counts = [
                        (source.url, await collect_source(collector, ingestion, source))
                        for source in await ingestion.enabled()
                    ]
            else:
                raise HTTPException(404, "Unknown source action")
        except LookupError as exc:
            raise HTTPException(404, "Source not found") from exc
        except ValueError as exc:
            return render(
                request,
                "sources.html",
                422,
                csrf=csrf,
                section="sources",
                rows=await store.sources(),
                error=str(exc),
                values=data,
            )
        if counts is not None:
            return render(
                request,
                "sources.html",
                csrf=csrf,
                section="sources",
                rows=await store.sources(),
                counts=counts,
            )
        return set_redirect("/sources", request)

    return app
