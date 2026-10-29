from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter
from uuid import uuid4

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from structlog.typing import FilteringBoundLogger

_logger: ContextVar[FilteringBoundLogger | None] = ContextVar("shovel_development_logger", default=None)
_POLL_PATH = re.compile(
    r"/api/v1/(?:status|resources|jobs|operations|resources/[^/]+/profiles|jobs/[^/]+/events)/?"
)
_SLOW_REQUEST_SECONDS = 1.0


@contextmanager
def development_logging() -> Iterator[None]:
    """Keep verbose console output local to the dev command and its child tasks."""
    logger = structlog.wrap_logger(
        structlog.PrintLogger(file=sys.stdout),
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
    ).bind(component="backend", env="dev")
    token = _logger.set(logger)
    try:
        yield
    finally:
        _logger.reset(token)


@contextmanager
def trace_context(**fields: object) -> Iterator[None]:
    logger = _logger.get()
    token = _logger.set(logger.bind(**fields) if logger is not None else None)
    try:
        yield
    finally:
        _logger.reset(token)


def trace(event: str, **fields: object) -> None:
    logger = _logger.get()
    if logger is not None:
        logger.debug(event, **fields)


class DevelopmentRequestLogging:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = perf_counter()
        status = 500
        request_id = uuid4().hex
        routine_poll = scope["method"] == "GET" and _POLL_PATH.fullmatch(scope["path"]) is not None
        failed = False

        async def send_response(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-shovel-request-id", request_id.encode("ascii")),
                ]
            await send(message)

        with trace_context(request_id=request_id):
            if not routine_poll:
                trace("http.started", method=scope["method"])
            try:
                await self.app(scope, receive, send_response)
            except BaseException as exc:
                failed = True
                trace("http.failed", error_type=type(exc).__name__)
                raise
            finally:
                elapsed = perf_counter() - started
                if failed or not routine_poll or not 200 <= status < 300 or elapsed >= _SLOW_REQUEST_SECONDS:
                    # Route templates exclude user-controlled path/query values and credentials.
                    trace(
                        "http.completed",
                        method=scope["method"],
                        route=getattr(scope.get("route"), "path", "<unmatched>"),
                        status=status,
                        elapsed_ms=round(elapsed * 1000, 2),
                    )
