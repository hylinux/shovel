from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from starlette.types import Receive, Scope, Send

from shovel.runtime.diagnostics import DevelopmentRequestLogging, development_logging, trace


@pytest.mark.parametrize(
    ("method", "path", "route", "status", "duration", "error", "logged"),
    [
        ("GET", "/api/v1/status", "/api/v1/status", 200, 0.01, None, False),
        ("GET", "/api/v1/resources", "/api/v1/resources", 200, 0.01, None, False),
        ("GET", "/api/v1/jobs", "/api/v1/jobs", 200, 0.01, None, False),
        ("GET", "/api/v1/operations", "/api/v1/operations", 200, 0.01, None, False),
        ("GET", "/api/v1/operations/memory-id", "/api/v1/operations/{identifier}", 200, 0.01, None, False),
        ("GET", "/api/v1/operations/memory-id", "/api/v1/operations/{identifier}", 500, 0.01, None, True),
        (
            "POST",
            "/api/v1/operations/memory-id/retry",
            "/api/v1/operations/{identifier}/retry",
            200,
            0.01,
            None,
            True,
        ),
        (
            "GET",
            "/api/v1/resources/private-id/profiles",
            "/api/v1/resources/{ref}/profiles",
            200,
            0.01,
            None,
            False,
        ),
        (
            "GET",
            "/api/v1/jobs/private-id/events",
            "/api/v1/jobs/{identifier}/events",
            200,
            0.01,
            None,
            False,
        ),
        ("GET", "/api/v1/status", "/api/v1/status", 503, 0.01, None, True),
        ("GET", "/api/v1/status", "/api/v1/status", 200, 1.0, None, True),
        ("GET", "/api/v1/status", "/api/v1/status", 200, 0.01, RuntimeError, True),
        ("GET", "/api/v1/status", "/api/v1/status", 200, 0.01, asyncio.CancelledError, True),
        ("GET", "/api/v1/memories", "/api/v1/memories", 200, 0.01, None, True),
        ("POST", "/api/v1/resources", "/api/v1/resources", 202, 0.01, None, True),
        ("POST", "/api/v1/agent", "/api/v1/agent", 200, 0.01, None, True),
    ],
)
async def test_poll_logs_preserve_errors_slow_requests_and_operation_traces(
    method: str,
    path: str,
    route: str,
    status: int,
    duration: float,
    error: type[BaseException] | None,
    logged: bool,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter((0.0, duration))
    monkeypatch.setattr("shovel.runtime.diagnostics.perf_counter", lambda: next(clock))

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        scope["route"] = SimpleNamespace(path=route)
        for event in ("scan.completed", "model.completed", "memory.completed"):
            trace(event)
        await send({"type": "http.response.start", "status": status, "headers": []})
        if error is not None:
            raise error("private-error-detail")
        await send({"type": "http.response.body", "body": b"private-response-body"})

    with development_logging():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=DevelopmentRequestLogging(app)),
            base_url="http://localhost",
        ) as client:
            if error is not None:
                with pytest.raises(error):
                    await client.request(method, f"{path}?private-query=value")
            else:
                response = await client.request(method, f"{path}?private-query=value")
                assert response.status_code == status
                assert response.headers["x-shovel-request-id"]
    output = capsys.readouterr().out
    assert ("http.completed" in output) == logged
    assert ("http.failed" in output) == (error is not None)
    if not logged:
        assert "http.started" not in output
    else:
        assert route in output
        assert f"status={status}" in output
    for event in ("scan.completed", "model.completed", "memory.completed"):
        assert event in output
    assert "private-" not in output
