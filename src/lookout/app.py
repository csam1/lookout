"""FastAPI application factory."""

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession

from lookout.admin import admin_router
from lookout.auth import authenticate, check_quota
from lookout.cache import NullCache, RedisCache, ResultCache
from lookout.config import Settings
from lookout.db import ApiKey, Database, UsageEvent, as_utc, month_start, usage_this_month
from lookout.errors import ApiError
from lookout.fetch import HttpFetcher, PageFetcher
from lookout.models import ErrorBody, ErrorResponse, SearchRequest, SearchResponse, UsageSummary
from lookout.providers.base import SearchProvider
from lookout.providers.fake import FakeProvider
from lookout.providers.searxng import SearxngProvider
from lookout.service import SearchService, credits_for

access_log = logging.getLogger("lookout.access")

STATIC_DIR = Path(__file__).parent / "static"
# The UI pages render untrusted web text and handle API keys, so lock them down: no inline or
# third-party scripts, API calls only to this origin, fonts only from Google Fonts.
UI_CSP = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "connect-src 'self'; "
    "img-src 'self' data:; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def build_provider(settings: Settings) -> SearchProvider:
    if settings.search_provider == "fake":
        return FakeProvider()
    if settings.search_provider == "searxng":
        return SearxngProvider(settings.searxng_base_url)
    raise RuntimeError(f"Unknown search provider: {settings.search_provider!r}")


def build_fetcher(settings: Settings, store: ResultCache | None = None) -> PageFetcher:
    return HttpFetcher(
        timeout=settings.fetch_timeout_seconds,
        max_bytes=settings.fetch_max_bytes,
        concurrency=settings.fetch_concurrency,
        user_agent=settings.user_agent,
        respect_robots=settings.respect_robots_txt,
        store=store,
    )


def build_cache(settings: Settings) -> ResultCache:
    return RedisCache(settings.redis_url) if settings.redis_url else NullCache()


def create_app(
    settings: Settings | None = None,
    *,
    provider: SearchProvider | None = None,
    db: Database | None = None,
    cache: ResultCache | None = None,
    fetcher: PageFetcher | None = None,
) -> FastAPI:
    settings = settings or Settings()
    provider = provider or build_provider(settings)
    db = db or Database(settings.database_url)
    cache = cache or build_cache(settings)
    fetcher = fetcher or build_fetcher(settings, store=cache)
    service = SearchService(
        provider,
        fetcher,
        cache,
        deep_deadline_seconds=settings.deep_deadline_seconds,
        page_cache_ttl_seconds=settings.page_cache_ttl_seconds,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if settings.auto_create_schema:
            await db.create_schema()
        yield
        await service.aclose()  # stop background page reads before closing what they use
        await fetcher.aclose()
        await cache.close()
        if hasattr(provider, "aclose"):
            await provider.aclose()
        await db.close()

    app = FastAPI(title="Lookout API", version="0.2.0", lifespan=lifespan)
    app.state.db = db

    @app.middleware("http")
    async def assign_request_id(request: Request, call_next):
        request.state.request_id = f"req_{uuid.uuid4().hex[:16]}"
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    def error_response(request: Request, status: int, code: str, message: str) -> JSONResponse:
        body = ErrorResponse(error=ErrorBody(code=code, message=message, request_id=request.state.request_id))
        return JSONResponse(status_code=status, content=body.model_dump())

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        return error_response(request, exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        msg = first.get("msg", "invalid request")
        return error_response(request, 422, "invalid_request", f"{loc}: {msg}" if loc else msg)

    async def get_session() -> AsyncIterator[AsyncSession]:
        async with db.sessions() as session:
            yield session

    async def current_key(request: Request, session: AsyncSession = Depends(get_session)) -> ApiKey:
        # Runs before body validation, so unauthenticated callers get 401, not validation details.
        return await authenticate(request, session)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def ui_page(filename: str) -> FileResponse:
        return FileResponse(
            STATIC_DIR / filename,
            headers={
                "Content-Security-Policy": UI_CSP,
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-cache",
            },
        )

    @app.get("/", include_in_schema=False)
    async def home() -> FileResponse:
        return ui_page("home.html")

    @app.get("/playground", include_in_schema=False)
    async def playground() -> FileResponse:
        return ui_page("playground.html")

    @app.get("/admin", include_in_schema=False)
    async def admin_page() -> FileResponse:
        return ui_page("admin.html")

    app.include_router(admin_router(get_session, current_key, settings.default_monthly_credit_limit))

    @app.get("/v1/usage", response_model=UsageSummary, responses={401: {"model": ErrorResponse}})
    async def usage(
        key: ApiKey = Depends(current_key),
        session: AsyncSession = Depends(get_session),
    ) -> UsageSummary:
        calls, credits = await usage_this_month(session, key.id)
        return UsageSummary(
            key_name=key.name,
            key_prefix=key.key_prefix,
            period_start=as_utc(month_start()),
            calls=calls,
            credits_used=credits,
            credit_limit=key.monthly_credit_limit,
        )

    @app.post(
        "/v1/search",
        response_model=SearchResponse,
        responses={
            401: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            429: {"model": ErrorResponse},
            502: {"model": ErrorResponse},
        },
    )
    async def search(
        body: SearchRequest,
        request: Request,
        key: ApiKey = Depends(current_key),
        session: AsyncSession = Depends(get_session),
    ) -> SearchResponse:
        started = time.perf_counter()
        request_id = request.state.request_id
        status = 200
        response: SearchResponse | None = None
        try:
            await check_quota(session, key, credits_for(body))
            response = await service.search(body, request_id=request_id)
            # Only successful searches are billed.
            session.add(
                UsageEvent(
                    api_key_id=key.id,
                    request_id=request_id,
                    endpoint="search",
                    mode=body.mode,
                    credits=response.usage.credits,
                    cache_hit=response.cached,
                    result_count=len(response.results),
                    latency_ms=response.latency_ms,
                )
            )
            await session.commit()
            return response
        except ApiError as exc:
            status = exc.status_code
            raise
        except Exception:
            status = 500
            raise
        finally:
            # Query text is deliberately not logged (may contain sensitive data).
            access_log.info(
                json.dumps(
                    {
                        "request_id": request_id,
                        "key": key.key_prefix,
                        "endpoint": "search",
                        "mode": body.mode,
                        "status": status,
                        "cached": response.cached if response else False,
                        "results": len(response.results) if response else 0,
                        "fetch_ok": sum(1 for r in response.results if r.fetch_status == "ok") if response else 0,
                        "latency_ms": int((time.perf_counter() - started) * 1000),
                    }
                )
            )

    return app
