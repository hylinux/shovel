from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from shovel.db.model.runtime import ChatMessage
from shovel.runtime.agent_instructions import BUILTIN_INSTRUCTIONS, AgentInstructionsService
from shovel.runtime.service import Runtime
from test_hosted_runtime import api as api
from test_hosted_runtime import runtime as runtime


def model_client(captured: list[dict[str, Any]]) -> httpx.AsyncClient:
    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "created": 1,
                "choices": [{"message": {"role": "assistant", "content": "Ready."}}],
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(respond))


async def test_saved_instructions_reach_maf_and_reload_without_losing_sessions(
    runtime: Runtime,
    api: httpx.AsyncClient,
) -> None:
    initial = await api.get("/api/v1/agent/instructions")
    assert initial.status_code == 200 and initial.headers["cache-control"] == "no-store"
    config = initial.json()
    assert config["content"] == "" and config["exists"] is False
    assert not runtime.instructions.path.exists()
    conversation = (await api.post("/api/v1/conversations", json={"title": "Keep session"})).json()["id"]
    memory = runtime.memory
    original = runtime.chat.agent
    first = await api.put(
        "/api/v1/agent/instructions",
        json={
            "content": "Explain answers with a short Python example.",
            "revision": config["revision"],
        },
    )
    assert first.status_code == 200 and first.json()["applied"] is True
    assert not first.json()["restart_required"]
    assert runtime.chat.agent is not original and runtime.memory is memory
    captured: list[dict[str, Any]] = []
    client = model_client(captured)
    runtime.chat.agent._client = client
    sent = await api.post(
        f"/api/v1/conversations/{conversation}/messages",
        json={
            "content": "hello",
            "memory_enabled": False,
        },
    )
    assert sent.status_code == 200, sent.text
    prompt = "\n".join(str(message["content"]) for message in captured[-1]["messages"])
    assert BUILTIN_INSTRUCTIONS in prompt and first.json()["content"] in prompt
    assert (await runtime.chat.conversation(conversation)).title == "Keep session"
    updated = await api.put(
        "/api/v1/agent/instructions",
        json={
            "content": "Use a numbered checklist instead.",
            "revision": first.json()["revision"],
        },
    )
    assert updated.status_code == 200 and client.is_closed
    runtime.chat.agent._client = model_client(captured)
    sent = await api.post(
        f"/api/v1/conversations/{conversation}/messages",
        json={
            "content": "hello",
            "memory_enabled": False,
        },
    )
    assert sent.status_code == 200, sent.text
    prompt = json.dumps(captured[-1], ensure_ascii=False)
    assert updated.json()["content"] in prompt and first.json()["content"] not in prompt
    assert len(await runtime.chat.messages(conversation)) == 4
    service = AgentInstructionsService(
        runtime.settings.agent.workspace, runtime.settings.model.max_input_chars
    )
    assert service.read()["content"] == updated.json()["content"]
    restored = await api.put(
        "/api/v1/agent/instructions",
        json={
            "content": "",
            "revision": updated.json()["revision"],
        },
    )
    assert restored.status_code == 200 and restored.json()["applied"]
    assert runtime.chat.agent.instructions == BUILTIN_INSTRUCTIONS


async def test_instructions_reject_stale_invalid_and_failed_writes_without_restarting(
    runtime: Runtime,
    api: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = (await api.get("/api/v1/agent/instructions")).json()
    agent = runtime.chat.agent
    runtime.instructions.path.parent.mkdir(parents=True, exist_ok=True)
    runtime.instructions.path.write_text("External edit", encoding="utf-8")
    stale = await api.put(
        "/api/v1/agent/instructions",
        json={
            "content": "Unsaved draft",
            "revision": initial["revision"],
        },
    )
    assert stale.status_code == 409
    latest = (await api.get("/api/v1/agent/instructions")).json()
    oversized = await api.put(
        "/api/v1/agent/instructions",
        json={
            "content": "x" * (latest["max_length"] + 1),
            "revision": latest["revision"],
        },
    )
    assert oversized.status_code == 422
    assert runtime.instructions.path.read_text(encoding="utf-8") == "External edit"
    assert runtime.chat.agent is agent

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise PermissionError("private-file-error")

    monkeypatch.setattr("shovel.runtime.workspace_config.os.replace", fail_replace)
    failed = await api.put(
        "/api/v1/agent/instructions",
        json={
            "content": "Unsaved draft",
            "revision": latest["revision"],
        },
    )
    assert failed.status_code == 503 and "private-file-error" not in failed.text
    assert runtime.instructions.path.read_text(encoding="utf-8") == "External edit"
    assert runtime.chat.agent is agent
    assert not list(runtime.instructions.path.parent.glob(".agent-instructions-*.tmp"))
    assert (
        await api.put(
            "/api/v1/agent/instructions",
            json={
                "content": "no",
                "revision": "invalid",
            },
        )
    ).status_code == 422
    assert (
        await api.put(
            "/api/v1/agent/instructions",
            json={
                "content": "no",
                "revision": latest["revision"],
            },
            headers={"Origin": "https://untrusted.invalid"},
        )
    ).status_code == 403


async def test_workspace_instructions_load_lazily_and_oversized_file_can_be_repaired(
    runtime: Runtime,
    api: httpx.AsyncClient,
) -> None:
    path = runtime.instructions.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Use concise explanations.", encoding="utf-8")
    captured: list[dict[str, Any]] = []
    runtime.chat.agent._client = model_client(captured)
    conversation = (await api.post("/api/v1/conversations", json={})).json()["id"]
    sent = await api.post(
        f"/api/v1/conversations/{conversation}/messages",
        json={"content": "hello", "memory_enabled": False},
    )
    assert sent.status_code == 200, sent.text
    assert "Use concise explanations." in json.dumps(captured[-1])
    assert (await api.get("/api/v1/agent/instructions")).json()["applied"]
    agent = runtime.chat.agent
    path.write_text("x" * (runtime.instructions.max_length + 1), encoding="utf-8")
    invalid = (await api.get("/api/v1/agent/instructions")).json()
    assert invalid["validation_error"] and invalid["restart_required"]
    assert (await api.post("/api/v1/agent/restart")).status_code == 422
    assert runtime.chat.agent is agent
    repaired = await api.put(
        "/api/v1/agent/instructions",
        json={"content": "Use short examples.", "revision": invalid["revision"]},
    )
    assert repaired.status_code == 200 and repaired.json()["applied"]
    assert repaired.json()["validation_error"] is None


async def test_busy_agent_rejects_instruction_save_without_modifying_file(
    runtime: Runtime,
    api: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = (await api.get("/api/v1/agent/instructions")).json()
    conversation = (await api.post("/api/v1/conversations", json={})).json()["id"]
    started, release = asyncio.Event(), asyncio.Event()

    async def blocked_send(identifier: str, *_args: Any, **_kwargs: Any) -> ChatMessage:
        started.set()
        await release.wait()
        return ChatMessage(conversation_id=identifier, role="assistant", content="ok", sequence=2)

    monkeypatch.setattr(runtime.chat, "_send", blocked_send)
    running = asyncio.create_task(
        api.post(
            f"/api/v1/conversations/{conversation}/messages",
            json={
                "content": "hello",
                "memory_enabled": False,
            },
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    try:
        rejected = await api.put(
            "/api/v1/agent/instructions",
            json={
                "content": "Keep this draft",
                "revision": config["revision"],
            },
        )
        assert rejected.status_code == 409 and not runtime.instructions.path.exists()
    finally:
        release.set()
        await running
    saved = await api.put(
        "/api/v1/agent/instructions",
        json={
            "content": "Keep this draft",
            "revision": config["revision"],
        },
    )
    assert saved.status_code == 200 and saved.json()["applied"]


async def test_saved_instructions_survive_restart_failure_and_can_be_applied(
    runtime: Runtime,
    api: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = (await api.get("/api/v1/agent/instructions")).json()
    original_close = runtime.chat.agent.close

    async def fail_close() -> None:
        raise RuntimeError("private-close-error")

    monkeypatch.setattr(runtime.chat.agent, "close", fail_close)
    failed = await api.put(
        "/api/v1/agent/instructions",
        json={
            "content": "Answer as a patient tutor.",
            "revision": initial["revision"],
        },
    )
    assert failed.status_code == 503
    assert "private-close-error" not in failed.text
    latest = (await api.get("/api/v1/agent/instructions")).json()
    assert latest["content"] == "Answer as a patient tutor."
    assert latest["restart_required"] and not latest["applied"]
    monkeypatch.setattr(runtime.chat.agent, "close", original_close)
    assert (await api.post("/api/v1/agent/restart")).status_code == 200
    assert (await api.get("/api/v1/agent/instructions")).json()["applied"]


async def test_cancelled_save_completes_restart_before_allowing_new_runs(
    runtime: Runtime,
    api: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = (await api.get("/api/v1/agent/instructions")).json()
    original_close = runtime.chat.agent.close
    started, release = asyncio.Event(), asyncio.Event()

    async def blocked_close() -> None:
        started.set()
        await release.wait()
        await original_close()

    monkeypatch.setattr(runtime.chat.agent, "close", blocked_close)
    request = asyncio.create_task(
        api.put(
            "/api/v1/agent/instructions",
            json={
                "content": "Answer with concise examples.",
                "revision": initial["revision"],
            },
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    try:
        request.cancel()
        assert (await api.post("/api/v1/agent/restart")).status_code == 409
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
    latest = (await api.get("/api/v1/agent/instructions")).json()
    assert latest["applied"] and latest["content"] == "Answer with concise examples."


async def test_custom_instructions_are_included_in_history_context_budget(
    runtime: Runtime,
    api: httpx.AsyncClient,
) -> None:
    runtime.settings.model.max_input_chars = 2000
    runtime.instructions = AgentInstructionsService(runtime.settings.agent.workspace, 2000)
    initial = (await api.get("/api/v1/agent/instructions")).json()
    phrase = "Use clear steps. "
    content = phrase * (initial["max_length"] // len(phrase))
    assert (
        await api.put(
            "/api/v1/agent/instructions",
            json={
                "content": content,
                "revision": initial["revision"],
            },
        )
    ).status_code == 200
    conversation = (await api.post("/api/v1/conversations", json={})).json()["id"]
    async with runtime.sessions() as session:
        session.add_all(
            [
                ChatMessage(
                    conversation_id=conversation,
                    sequence=index + 1,
                    role="user" if index % 2 == 0 else "assistant",
                    content=f"Earlier turn {index}: " + "background " * 9,
                )
                for index in range(20)
            ]
        )
        await session.commit()
    captured: list[dict[str, Any]] = []
    runtime.chat.agent._client = model_client(captured)
    response = await api.post(
        f"/api/v1/conversations/{conversation}/messages",
        json={
            "content": "hello",
            "memory_enabled": False,
        },
    )
    assert response.status_code == 200, response.text
    prompt = "\n".join(str(message["content"]) for message in captured[-1]["messages"])
    assert content in prompt and len(prompt) <= 2000
