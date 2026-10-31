"""
Production-ready FastAPI entry point.

Install:
    pip install "fastapi" "uvicorn[standard]" httpx pydantic-settings

Run (dev):
    ENVIRONMENT=development python main.py

Run (prod, single container):
    python main.py                     # uses WEB_CONCURRENCY workers
    # or: uvicorn main:app --workers 4 --host 0.0.0.0 --port 8000

How concurrency is handled
--------------------------
* Async endpoints run on the event loop: thousands of in-flight I/O-bound
  requests per worker (use async DB drivers / httpx, never blocking calls).
* Blocking / sync code goes through the thread pool (def endpoints or
  run_in_threadpool); pool size is configurable.
* CPU-bound work should be offloaded to a process pool / task queue, or
  scaled via multiple worker processes (one event loop per process).
* A concurrency limiter sheds load with 503 + Retry-After instead of letting
  the process collapse under overload.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import anyio.to_thread
import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api import forward_email_router
from app.core.config import settings



# --------------------------------------------------------------------------- #
# Logging (with per-request correlation id)
# --------------------------------------------------------------------------- #
request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        return True


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] [req=%(request_id)s] %(message)s"
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())
    # We emit our own access log; silence uvicorn's duplicate one.
    logging.getLogger("uvicorn.access").disabled = True


configure_logging()
logger = logging.getLogger(settings.app_name)


# --------------------------------------------------------------------------- #
# Pure-ASGI middleware (lower overhead than BaseHTTPMiddleware and safe
# with streaming responses / contextvars)
# --------------------------------------------------------------------------- #
class RequestContextMiddleware:
    """Adds X-Request-ID, X-Process-Time headers and an access log line."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope["headers"])
        rid = headers.get(b"x-request-id", b"").decode() or uuid.uuid4().hex
        token = request_id_ctx.set(rid)
        scope.setdefault("state", {})["request_id"] = rid
        start = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                elapsed_ms = (time.perf_counter() - start) * 1000
                hdrs = list(message.get("headers", []))
                hdrs.append((b"x-request-id", rid.encode()))
                hdrs.append((b"x-process-time-ms", f"{elapsed_ms:.2f}".encode()))
                message["headers"] = hdrs
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info(
                "%s %s -> %s (%.1f ms)",
                scope["method"],
                scope["path"],
                status_code,
                elapsed_ms,
            )
            request_id_ctx.reset(token)


class ConcurrencyLimitMiddleware:
    """Load shedding: reject with 503 when too many requests are in flight."""

    EXEMPT_PATHS = {"/health/live", "/health/ready"}

    def __init__(self, app: ASGIApp, max_concurrent: int) -> None:
        self.app = app
        self._sem = asyncio.Semaphore(max_concurrent)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self.EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        if self._sem.locked():
            logger.warning("Concurrency limit reached; shedding request")
            response = JSONResponse(
                {"detail": "Server is busy, please retry shortly."},
                status_code=503,
                headers={"Retry-After": "1"},
            )
            await response(scope, receive, send)
            return

        async with self._sem:
            await self.app(scope, receive, send)


# --------------------------------------------------------------------------- #
# Lifespan: create/destroy shared resources once per worker
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # Size the thread pool used by sync endpoints / run_in_threadpool.
    anyio.to_thread.current_default_thread_limiter().total_tokens = (
        settings.threadpool_size
    )

    # One shared, pooled HTTP client per worker (reuses TCP/TLS connections).
    app.state.http = httpx.AsyncClient(
        timeout=settings.upstream_timeout_seconds,
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
    )

    # TODO: init DB engine / Redis pool / message broker here, e.g.
    #   app.state.db = await create_async_engine(...)
    #   app.state.redis = redis.asyncio.from_url(...)

    app.state.ready = True
    logger.info(
        "Started pid=%s env=%s workers=%s",
        os.getpid(),
        settings.environment,
        settings.web_concurrency,
    )
    try:
        yield
    finally:
        app.state.ready = False  # readiness probe fails -> LB stops routing
        logger.info("Shutting down pid=%s", os.getpid())
        await app.state.http.aclose()
        # TODO: dispose DB engine / close Redis here.


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #
def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        lifespan=lifespan,
        # Hide interactive docs in production.
        docs_url="/docs" if settings.is_dev else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.is_dev else None,
    )

    # Middleware order: last added = outermost.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.add_middleware(
        ConcurrencyLimitMiddleware, max_concurrent=settings.max_concurrent_requests
    )
    app.add_middleware(RequestContextMiddleware)  # outermost: logs everything

    register_exception_handlers(app)
    register_routes(app)
    return app


# --------------------------------------------------------------------------- #
# Error handling: consistent JSON, never leak stack traces
# --------------------------------------------------------------------------- #
def _error(status: int, message: str, **extra) -> JSONResponse:
    body = {"error": {"message": message, "request_id": request_id_ctx.get(), **extra}}
    return JSONResponse(body, status_code=status)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_exc_handler(_: Request, exc: StarletteHTTPException):
        return _error(exc.status_code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError):
        return _error(422, "Validation failed", details=exc.errors())

    @app.exception_handler(Exception)
    async def unhandled_handler(_: Request, exc: Exception):
        logger.exception("Unhandled error")
        return _error(500, "Internal server error")


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def register_routes(app: FastAPI) -> None:
    @app.get("/health/live", include_in_schema=False)
    async def liveness():
        """Process is up. Used by the orchestrator to decide on restarts."""
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    async def readiness(request: Request):
        """Ready to take traffic (deps initialised, not shutting down)."""
        if not getattr(request.app.state, "ready", False):
            raise HTTPException(status_code=503, detail="Not ready")
        # TODO: ping DB / Redis here with a short timeout.
        return {"status": "ready"}

    # POST /api/v1/forward-email — inbound email webhook (Postfix pipe).
    app.include_router(forward_email_router, prefix="/api/v1")

    # --- Example 1: I/O-bound, fully async (best for high concurrency) ----- #
    @app.get("/api/v1/aggregate")
    async def aggregate(request: Request):
        """Fan out to several upstream calls concurrently."""
        client: httpx.AsyncClient = request.app.state.http
        urls = ["https://httpbin.org/delay/1"] * 3

        async def fetch(url: str) -> int:
            r = await client.get(url)
            return r.status_code

        try:
            statuses = await asyncio.gather(*(fetch(u) for u in urls))
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="Upstream timeout")
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="Upstream error")
        return {"upstream_statuses": statuses}

    # --- Example 2: blocking library -> thread pool, keeps the loop free --- #
    def _blocking_work(n: int) -> int:
        time.sleep(0.5)  # stands in for a sync SDK / legacy driver call
        return n * 2

    @app.get("/api/v1/blocking/{n}")
    async def blocking(n: int):
        result = await run_in_threadpool(_blocking_work, n)
        return {"result": result}


app = create_app()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> None:
    uvicorn.run(
        "app.main:app",  # import string is required for workers/reload
        host=settings.host,
        port=settings.port,
        workers=1 if settings.is_dev else settings.web_concurrency,
        reload=settings.is_dev,
        log_config=None,  # use our logging setup
        access_log=False,  # we log in RequestContextMiddleware
        proxy_headers=True,  # honour X-Forwarded-* from a trusted proxy/LB
        forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1"),
        backlog=2048,
        timeout_keep_alive=settings.keep_alive_timeout,
        timeout_graceful_shutdown=settings.graceful_shutdown_timeout,
    )


if __name__ == "__main__":
    main()