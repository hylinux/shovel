"""Owned, revision-checked task notes, independent of long-term extraction."""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shovel.config.memory_config import MemorySettings
from shovel.db.base import now_ts
from shovel.db.model.memory import MemoryWorking
from shovel.db.model.runtime import ChatMessage, Conversation, MemoryEpoch
from shovel.domain.enums import MemoryValidityState, WorkingSlot
from shovel.memory.domain import _SECRET, MemoryScope
from shovel.protection.egress import EgressChannel, require_ordinary
from shovel.runtime.diagnostics import trace
from shovel.runtime.errors import NotFound, RuntimeFailure

Note = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
MessageId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class WorkingMemoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_revision: int = Field(ge=0)
    goal: str = Field(min_length=1, max_length=1000)
    constraints: list[Note] = Field(default_factory=list, max_length=20)
    decisions: list[Note] = Field(default_factory=list, max_length=20)
    plan: list[Note] = Field(default_factory=list, max_length=20)
    blockers: list[Note] = Field(default_factory=list, max_length=20)
    next_step: str = Field(default="", max_length=1000)
    evidence_message_ids: list[MessageId] = Field(default_factory=list, max_length=20)
    state: Literal["active", "completed", "archived"] = "active"
    expires_at: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def safe_notes(self) -> WorkingMemoryInput:
        serialized = self.model_dump_json()
        if len(serialized) > 12000:
            raise ValueError("Working Memory 超过 12000 字符预算。")
        if _SECRET.search(serialized):
            raise ValueError("不能将凭据保存到 Working Memory。")
        return self


class WorkingMemoryService:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], scope: MemoryScope, settings: MemorySettings
    ) -> None:
        self.sessions, self.scope, self.settings = sessions, scope, settings

    def _query(self, session_id: str, task_id: str | None = None) -> Select[MemoryWorking]:
        query = select(MemoryWorking).where(
            MemoryWorking.workspace_id == self.scope.workspace_id,
            MemoryWorking.user_id == self.scope.user_id,
            MemoryWorking.agent_id == self.scope.agent_id,
            MemoryWorking.session_id == session_id,
            MemoryWorking.task_id != "",
        )
        return query.where(MemoryWorking.task_id == task_id) if task_id is not None else query

    @staticmethod
    def _snapshot(row: MemoryWorking) -> dict[str, Any]:
        content = json.loads(row.content)
        return {
            **content,
            "task_id": row.task_id,
            "revision": row.revision,
            "updated_at": row.updated_at,
            "expires_at": row.expires_at,
            "state": (
                "expired" if row.expires_at is not None and row.expires_at <= now_ts() else content["state"]
            ),
            "last_user_message_id": row.meta.get("last_user_message_id"),
            "last_assistant_message_id": row.meta.get("last_assistant_message_id"),
        }

    async def list(self, session_id: str) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            rows = await session.scalars(
                self._query(session_id)
                .where(MemoryWorking.validity_state != MemoryValidityState.DELETED)
                .order_by(MemoryWorking.updated_at.desc(), MemoryWorking.id)
                .limit(100)
            )
            return [self._snapshot(row) for row in rows]

    async def save(self, session_id: str, task_id: str, body: WorkingMemoryInput) -> dict[str, Any]:
        require_ordinary(EgressChannel.MEMORY, body)
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", task_id) is None:
            raise RuntimeFailure("Working Memory task_id 必须是 1-200 位字母、数字或 ._-。", status=422)
        expires_at = body.expires_at
        if expires_at is None:
            expires_at = now_ts() + self.settings.working_ttl_seconds
        if expires_at <= now_ts():
            raise RuntimeFailure("Working Memory 有效期必须在未来。", status=422)
        async with self.sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            if await session.get(Conversation, session_id) is None:
                raise NotFound("Session", session_id)
            row = await session.scalar(self._query(session_id, task_id))
            if (row.revision if row is not None else 0) != body.expected_revision:
                raise RuntimeFailure("Working Memory 已变更,请重新加载后编辑。", status=409)
            if row is not None and (
                row.validity_state != MemoryValidityState.ACTIVE
                or (row.expires_at is not None and row.expires_at <= now_ts())
            ):
                raise RuntimeFailure("Working Memory 已删除或过期,请新建任务。", status=409)
            evidence = list(dict.fromkeys(body.evidence_message_ids))
            messages = list(
                await session.scalars(
                    select(ChatMessage.id).where(
                        ChatMessage.id.in_(evidence),
                        ChatMessage.conversation_id == session_id,
                        ChatMessage.state == "completed",
                    )
                )
            )
            if set(messages) != set(evidence):
                raise RuntimeFailure("证据必须引用当前 Session 中已持久化完成的消息。", status=422)
            if row is None:
                count = await session.scalar(
                    self._query(session_id)
                    .with_only_columns(func.count())
                    .where(
                        MemoryWorking.validity_state != MemoryValidityState.DELETED,
                    )
                )
                if count is not None and count >= 100:
                    raise RuntimeFailure("此 Session 已有 100 个工作任务,请先删除不需要的任务。", status=409)
                row = MemoryWorking(
                    workspace_id=self.scope.workspace_id,
                    user_id=self.scope.user_id,
                    agent_id=self.scope.agent_id,
                    session_id=session_id,
                    task_id=task_id,
                    slot=WorkingSlot.PLAN,
                    content="{}",
                    revision=1,
                )
                session.add(row)
            else:
                row.revision += 1
            row.content = json.dumps(
                body.model_dump(exclude={"expected_revision", "expires_at"}), ensure_ascii=False
            )
            row.refs, row.expires_at, row.updated_at = evidence, expires_at, now_ts()
            await session.flush()
            result = self._snapshot(row)
            await session.commit()
        trace("memory.working.saved", task_id=task_id, revision=result["revision"])
        return result

    async def delete(self, session_id: str, task_id: str, revision: int) -> None:
        async with self.sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            row = await session.scalar(self._query(session_id, task_id))
            if row is None:
                raise NotFound("Working Memory", task_id)
            if row.revision != revision:
                raise RuntimeFailure("Working Memory 已变更,请重新加载后删除。", status=409)
            row.validity_state, row.content = MemoryValidityState.DELETED, "{}"
            row.refs, row.meta = [], {}
            row.revision += 1
            row.updated_at = now_ts()
            await session.commit()

    async def context(self, session_id: str, task_id: str) -> dict[str, Any]:
        async with self.sessions() as session:
            row = await session.scalar(self._query(session_id, task_id))
            if row is None:
                raise NotFound("Working Memory", task_id)
            if (
                row.validity_state != MemoryValidityState.ACTIVE
                or (row.expires_at is not None and row.expires_at <= now_ts())
                or json.loads(row.content)["state"] != "active"
            ):
                raise RuntimeFailure("所选工作任务已完成、归档、删除或过期,请重新选择。", status=409)
            snapshot = self._snapshot(row)
        context = {
            "task_id": task_id,
            "revision": snapshot["revision"],
            "goal": snapshot["goal"],
            **{key: snapshot[key] for key in ("constraints", "decisions", "blockers", "next_step")},
        }
        budget = self.settings.working_context_max_chars - 128
        if len(json.dumps(context, ensure_ascii=False)) > budget:
            raise RuntimeFailure("工作目标/约束超过上下文预算,请缩短任务备注或调整 Memory 配置。", status=503)
        omitted = []
        for key in ("plan", "evidence_message_ids"):
            candidate = {**context, key: snapshot[key]}
            if len(json.dumps(candidate, ensure_ascii=False)) <= budget:
                context = candidate
            else:
                omitted.append(key)
        context["omitted_fields"] = omitted
        return context

    async def record_turn(
        self,
        session: AsyncSession,
        session_id: str,
        task_id: str,
        user: ChatMessage,
        answer: ChatMessage,
        generation: int,
    ) -> None:
        epoch = await session.scalar(
            select(MemoryEpoch.generation).where(
                MemoryEpoch.workspace_id == self.scope.workspace_id,
                MemoryEpoch.user_id == self.scope.user_id,
            )
        )
        if epoch != generation:
            trace("memory.working.turn_fenced", task_id=task_id)
            return
        row = await session.scalar(self._query(session_id, task_id))
        if row is None or row.validity_state != MemoryValidityState.ACTIVE:
            raise RuntimeFailure("Working Memory 在回答持久化前已失效。", status=409)
        row.meta = {**row.meta, "last_user_message_id": user.id, "last_assistant_message_id": answer.id}
        row.turn, row.updated_at = answer.sequence, now_ts()
        row.revision += 1

    async def forget(self, session_id: str) -> None:
        async with self.sessions() as session:
            rows = await session.scalars(self._query(session_id))
            for row in rows:
                await session.delete(row)
            await session.commit()

    async def expire_due(self) -> int:
        async with self.sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            rows = list(
                await session.scalars(
                    select(MemoryWorking)
                    .where(
                        MemoryWorking.workspace_id == self.scope.workspace_id,
                        MemoryWorking.user_id == self.scope.user_id,
                        MemoryWorking.agent_id == self.scope.agent_id,
                        MemoryWorking.validity_state == MemoryValidityState.ACTIVE,
                        MemoryWorking.expires_at.is_not(None),
                        MemoryWorking.expires_at <= now_ts(),
                    )
                    .limit(32)
                )
            )
            for row in rows:
                row.validity_state = MemoryValidityState.EXPIRED
                row.revision += 1
                row.updated_at = now_ts()
            await session.commit()
            return len(rows)
