from __future__ import annotations

import io
import json
import os
import re
from collections.abc import AsyncIterator
from pathlib import Path
from random import Random
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image, ImageDraw
from scripts.memory_baseline import baseline_runtime, isolated_settings

from shovel.config.model_settings import DefaultAgentModelSettings
from shovel.config.settings import AppSettings
from shovel.runtime.api import configure_api
from shovel.runtime.service import Runtime

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("SHOVEL_RUN_LIVE_TESTS") != "1",
        reason="Explicit live endpoint opt-in required",
    ),
]


@pytest.fixture
async def live_attachment_session(
    tmp_path: Path,
) -> AsyncIterator[tuple[Runtime, httpx.AsyncClient, dict[str, Any]]]:
    path = os.environ.get("SHOVEL_LIVE_CONFIG")
    assert path, "Set SHOVEL_LIVE_CONFIG to an explicitly selected settings.toml."
    configured = AppSettings.load(Path(path))
    assert configured.model.is_configured, "Attachment live tests require a configured chat model."
    settings = isolated_settings(configured, tmp_path)
    settings.model = configured.model.model_copy(
        update={
            "max_input_chars": min(
                configured.model.max_input_chars, DefaultAgentModelSettings().max_input_chars
            )
        }
    )
    report: dict[str, Any] = {
        "provider": settings.model.model_provider_type.value,
        "model": settings.model.model_name,
        "configured_max_input_chars": configured.model.max_input_chars,
        "tested_max_input_chars": settings.model.max_input_chars,
        "max_input_images": settings.model.max_input_images,
        "max_input_image_bytes": settings.model.max_input_image_bytes,
        "requests": [],
        "cases": [],
    }

    async def observe_request(request: httpx.Request) -> None:
        body = json.loads(request.content)
        images = [
            part["image_url"]["url"]
            for message in body.get("messages", [])
            if isinstance(message.get("content"), list)
            for part in message["content"]
            if part.get("type") == "image_url"
        ]
        report["requests"].append(
            {
                "images": len(images),
                "request_bytes": len(request.content),
                "stream": body.get("stream", False),
            }
        )

    async with baseline_runtime(settings) as runtime:
        runtime.chat.agent.client()
        client = runtime.chat.agent._client
        assert client is not None
        client.event_hooks["request"].append(observe_request)
        app = FastAPI()
        configure_api(app, lambda: runtime, settings)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:9999"
        ) as api:
            yield runtime, api, report


@pytest.mark.parametrize("protocol", ["rest", "agui"])
async def test_live_high_detail_images_multiple_images_history_and_budget_rejection(
    live_attachment_session: tuple[Runtime, httpx.AsyncClient, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
) -> None:
    runtime, api, report = live_attachment_session
    try:
        created = await api.post("/api/v1/conversations")
        assert created.status_code == 201
        identifier = created.json()["id"]
        attachments = []
        for index, color in enumerate(("red", "blue")):
            image = Image.frombytes("RGB", (640, 360), Random(0).randbytes(640 * 360 * 3))
            ImageDraw.Draw(image).rectangle((160, 60, 480, 300), fill=color)
            data = io.BytesIO()
            image.save(data, format="PNG")
            uploaded = await api.put(
                f"/api/v1/conversations/{identifier}/attachments/{uuid4()}",
                params={"filename": f"synthetic-{index}.png"},
                content=data.getvalue(),
                headers={"Content-Type": "application/octet-stream"},
            )
            assert uploaded.status_code == 200, uploaded.text
            attachments.append(uploaded.json())
        assert all(item["size"] * 4 // 3 > runtime.settings.model.max_input_chars for item in attachments)

        async def send(prompt: str, ids: list[str]) -> httpx.Response:
            payload = {
                "content": prompt,
                "attachment_ids": ids,
                "memory_enabled": False,
                "search_mode": "keyword",
            }
            if protocol == "rest":
                return await api.post(f"/api/v1/conversations/{identifier}/messages", json=payload)
            return await api.post(
                "/api/v1/agent",
                json={
                    "threadId": identifier,
                    "runId": str(uuid4()),
                    "messages": [{"id": str(uuid4()), "role": "user", "content": prompt}],
                    "state": {},
                    "tools": [],
                    "context": [],
                    "forwardedProps": payload,
                },
            )

        red, blue = (item["id"] for item in attachments)
        stages = [
            (
                "single",
                "Name the color of the large solid rectangle in the center of the attached image. "
                "Reply with one lowercase English color name. Do not call tools.",
                [red],
                1,
                r"\bred\b",
            ),
            (
                "multiple",
                "For only the two images attached to this message, name the colors of their central "
                "solid rectangles in attachment order. Reply with lowercase English color names "
                "separated by a comma. Do not call tools.",
                [red, blue],
                3,
                r"\bred\b.*\bblue\b",
            ),
            (
                "history",
                "Using the two images attached to my immediately preceding message, name their central "
                "solid rectangle colors in order. Reply with lowercase English color names separated "
                "by a comma. Do not call tools.",
                [],
                3,
                r"\bred\b.*\bblue\b",
            ),
        ]
        for stage, prompt, ids, expected_images, expected_answer in stages:
            before = len(report["requests"])
            response = await send(prompt, ids)
            case: dict[str, Any] = {"stage": stage, "status": response.status_code}
            report["cases"].append(case)
            assert response.status_code == 200, response.text
            if protocol == "agui":
                assert "RUN_FINISHED" in response.text and "RUN_ERROR" not in response.text, response.text
            messages = (await api.get(f"/api/v1/conversations/{identifier}/messages")).json()["items"]
            case.update(
                answer=messages[-1]["content"],
                state=messages[-1]["state"],
                images=[request["images"] for request in report["requests"][before:]],
            )
            assert case["state"] == "completed", case
            assert expected_images in case["images"], case
            assert re.search(expected_answer, case["answer"].lower(), re.DOTALL), case

        for limit, value in (("max_input_images", 1), ("max_input_image_bytes", 1024)):
            before = len(report["requests"])
            with monkeypatch.context() as patch:
                patch.setattr(runtime.settings.model, limit, value)
                response = await send("Describe these images without calling tools.", [red, blue])
            report["cases"].append(
                {
                    "stage": limit,
                    "status": response.status_code,
                    "new_model_requests": len(report["requests"]) - before,
                }
            )
            assert response.status_code == (413 if protocol == "rest" else 200), response.text
            if protocol == "agui":
                assert "RUN_ERROR" in response.text and "RUN_FINISHED" not in response.text
            assert "媒体预算" in response.text
            assert len(report["requests"]) == before
    finally:
        directory = Path(os.environ.get("SHOVEL_ATTACHMENT_LIVE_REPORT_DIR", str(tmp_path)))
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"attachment-live-{protocol}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
