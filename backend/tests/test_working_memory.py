from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from shovel.db.base import now_ts
from shovel.db.model.memory import MemoryWorking
from shovel.db.model.runtime import ChatMessage
from shovel.memory.working import WorkingMemoryService
from shovel.runtime.errors import NotFound, RuntimeFailure
from shovel.runtime.service import Runtime
from test_hosted_runtime import api as api
from test_hosted_runtime import runtime as runtime
from test_hosted_runtime import streamed_completion


async def create_task(api: httpx.AsyncClient, session: str, task: str, **notes: Any) -> dict[str, Any]:
    response = await api.put(
        f"/api/v1/conversations/{session}/working-memory/{task}",
        json={"expected_revision": 0, "goal": f"Goal for {task}", **notes},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_working_context_restores_owned_tasks_and_cas(runtime: Runtime, api: httpx.AsyncClient) -> None:
    session = (await api.post("/api/v1/conversations", json={})).json()["id"]
    other = (await api.post("/api/v1/conversations", json={})).json()["id"]
    await create_task(api, session, "one", constraints=["Keep SQLite authoritative."])
    await create_task(api, session, "two")
    await create_task(api, other, "one", goal="Unrelated task.")
    fresh = WorkingMemoryService(runtime.sessions, runtime.memory.owner, runtime.settings.memory)
    assert (await fresh.context(session, "one"))["constraints"] == ["Keep SQLite authoritative."]
    assert (await fresh.context(session, "two"))["goal"] == "Goal for two"
    for scope in (
        replace(runtime.memory.owner, workspace_id="other"),
        replace(runtime.memory.owner, user_id="other"),
        replace(runtime.memory.owner, agent_id="other"),
    ):
        foreign = WorkingMemoryService(runtime.sessions, scope, runtime.settings.memory)
        with pytest.raises(NotFound):
            await foreign.context(session, "one")
    updates = await asyncio.gather(
        *(
            api.put(
                f"/api/v1/conversations/{session}/working-memory/one",
                json={"expected_revision": 1, "goal": goal},
            )
            for goal in ("New goal A", "New goal B")
        )
    )
    assert sorted(response.status_code for response in updates) == [200, 409]
    current = next(item for item in (await fresh.list(session)) if item["task_id"] == "one")
    assert current["revision"] == 2
    assert (
        await api.delete(f"/api/v1/conversations/{session}/working-memory/one?revision=1")
    ).status_code == 409


@pytest.mark.parametrize("stream", [False, True])
async def test_selected_working_task_is_in_real_agent_context_and_receipt(
    runtime: Runtime, api: httpx.AsyncClient, stream: bool
) -> None:
    captured = []

    def model(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        if body.get("stream"):
            return streamed_completion("Continue safely.")
        return httpx.Response(
            200,
            json={
                "created": 1,
                "choices": [{"message": {"role": "assistant", "content": "Continue safely."}}],
            },
        )

    runtime.chat.agent._client = httpx.AsyncClient(transport=httpx.MockTransport(model))
    session = (await api.post("/api/v1/conversations", json={})).json()["id"]
    await create_task(
        api,
        session,
        "selected",
        goal="Restore the long-running migration.",
        constraints=["Keep the original authority."],
        plan=["Validate before applying."],
    )
    await create_task(api, session, "unselected", goal="Do not expose another task.")
    properties = {"working_task_id": "selected", "memory_enabled": True}
    if stream:
        response = await api.post(
            "/api/v1/agent",
            json={
                "threadId": session,
                "runId": "working-run",
                "state": {},
                "messages": [{"id": "question", "role": "user", "content": "Continue this task."}],
                "tools": [],
                "context": [],
                "forwardedProps": properties,
            },
        )
        assert response.status_code == 200 and "RUN_FINISHED" in response.text
    else:
        response = await api.post(
            f"/api/v1/conversations/{session}/messages",
            json={
                "content": "Continue this task.",
                **properties,
            },
        )
        assert response.status_code == 200, response.text
    serialized = json.dumps(captured, ensure_ascii=False)
    assert "Restore the long-running migration." in serialized
    assert "Keep the original authority." in serialized
    assert "Do not expose another task." not in serialized
    reference_messages = [
        message for message in captured[0]["messages"] if "working_memory" in str(message.get("content", ""))
    ]
    assert len(reference_messages) == 1 and reference_messages[0]["role"] == "user"
    assert "\u672a\u53d7\u4fe1\u4efb" in reference_messages[0]["content"]
    fresh = WorkingMemoryService(runtime.sessions, runtime.memory.owner, runtime.settings.memory)
    selected = next(task for task in await fresh.list(session) if task["task_id"] == "selected")
    assert selected["revision"] == 2 and selected["state"] == "active"
    async with runtime.sessions() as db:
        answer = await db.get(ChatMessage, selected["last_assistant_message_id"])
        assert answer is not None and answer.state == "completed"
        assert selected["last_user_message_id"]
    # Without an explicit selection, stored task notes do not enter a later run.
    captured.clear()
    response = await api.post(
        f"/api/v1/conversations/{session}/messages",
        json={
            "content": "Answer without task notes.",
            "memory_enabled": False,
        },
    )
    assert response.status_code == 200
    assert "working_memory" not in json.dumps(captured)
    assert next(task for task in await fresh.list(session) if task["task_id"] == "selected")["revision"] == 2


async def test_working_failures_do_not_advance_task_and_invalid_selection_persists_nothing(
    runtime: Runtime, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = (await api.post("/api/v1/conversations", json={})).json()["id"]
    await create_task(api, session, "task")
    invalid = await api.post(
        f"/api/v1/conversations/{session}/messages",
        json={
            "content": "Continue.",
            "working_task_id": "unknown",
        },
    )
    assert invalid.status_code == 404
    async with runtime.sessions() as db:
        assert list(await db.scalars(select(ChatMessage).where(ChatMessage.conversation_id == session))) == []

    async def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeFailure("Fixture model failure.", status=502)

    monkeypatch.setattr(runtime.chat.agent, "respond", fail)
    response = await api.post(
        f"/api/v1/conversations/{session}/messages",
        json={
            "content": "Continue.",
            "working_task_id": "task",
        },
    )
    assert response.status_code == 502
    task = (await runtime.memory.working.list(session))[0]
    assert task["revision"] == 1 and task["last_assistant_message_id"] is None


async def test_working_evidence_expiry_deletion_and_chat_lock(
    runtime: Runtime, api: httpx.AsyncClient
) -> None:
    session = (await api.post("/api/v1/conversations", json={})).json()["id"]
    other = (await api.post("/api/v1/conversations", json={})).json()["id"]
    async with runtime.sessions() as db:
        foreign = ChatMessage(conversation_id=other, role="user", content="Private source.", sequence=1)
        db.add(foreign)
        await db.commit()
    invalid = await api.put(
        f"/api/v1/conversations/{session}/working-memory/task",
        json={
            "expected_revision": 0,
            "goal": "Goal",
            "evidence_message_ids": [foreign.id],
        },
    )
    assert invalid.status_code == 422
    await create_task(api, session, "task")
    lock = runtime.chat._locks.setdefault(session, asyncio.Lock())
    async with lock:
        assert (
            await api.put(
                f"/api/v1/conversations/{session}/working-memory/task",
                json={
                    "expected_revision": 1,
                    "goal": "Changed goal",
                },
            )
        ).status_code == 409
        assert (
            await api.delete(f"/api/v1/conversations/{session}/working-memory/task?revision=1")
        ).status_code == 409
    async with runtime.sessions() as db:
        row = await db.scalar(
            select(MemoryWorking).where(MemoryWorking.session_id == session, MemoryWorking.task_id == "task")
        )
        assert row is not None
        row.expires_at = now_ts() - 1
        await db.commit()
    assert (await runtime.memory.working.list(session))[0]["state"] == "expired"
    with pytest.raises(RuntimeFailure, match="\u8fc7\u671f"):
        await runtime.memory.working.context(session, "task")
    assert await runtime.memory.working.expire_due() == 1
    assert (
        await api.put(
            f"/api/v1/conversations/{session}/working-memory/task",
            json={
                "expected_revision": 2,
                "goal": "Revive expired goal",
            },
        )
    ).status_code == 409
    await create_task(api, other, "retained")
    assert (await api.delete(f"/api/v1/conversations/{session}")).status_code == 200
    async with runtime.sessions() as db:
        assert list(await db.scalars(select(MemoryWorking).where(MemoryWorking.session_id == session))) == []
    assert (await runtime.memory.working.list(other))[0]["task_id"] == "retained"


async def test_working_context_budget_preserves_critical_notes(
    runtime: Runtime, api: httpx.AsyncClient
) -> None:
    session = (await api.post("/api/v1/conversations", json={})).json()["id"]
    runtime.settings.memory.working_context_max_chars = 1500
    await create_task(api, session, "bounded", plan=["x" * 500] * 4)
    context = await runtime.memory.working.context(session, "bounded")
    assert context["goal"] == "Goal for bounded"
    assert context["omitted_fields"] == ["plan"]
    assert len(json.dumps(context, ensure_ascii=False)) <= 1500
    await create_task(api, session, "too-large", constraints=["x" * 500] * 4)
    with pytest.raises(RuntimeFailure, match="\u9884\u7b97"):
        await runtime.memory.working.context(session, "too-large")
    assert (
        await api.put(
            f"/api/v1/conversations/{session}/working-memory/sensitive",
            json={
                "expected_revision": 0,
                "goal": "Store my password.",
            },
        )
    ).status_code == 422
