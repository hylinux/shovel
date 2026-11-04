"""Contain managed enterprise flows at legacy sinks that cannot retain protection."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, fields, is_dataclass
from enum import StrEnum

import structlog
from pydantic import BaseModel

from shovel.protection.domain import (
    ArtifactRef,
    DataAction,
    DataDestination,
    ProtectedContent,
    ProtectedContentMetadata,
    ProtectedContentRef,
    ProtectionContext,
    ProtectionDeniedError,
)


class EgressChannel(StrEnum):
    PROCESS = "process"
    DISPLAY = "display"
    MODEL = "model"
    TOOL = "tool"
    ARTIFACT = "artifact"
    CLIPBOARD = "clipboard"
    MEMORY = "memory"
    KNOWLEDGE = "knowledge"
    EMBEDDING = "embedding"
    INDEX = "index"
    PERSISTENCE = "persistence"
    CACHE = "cache"

    @property
    def binding(self) -> tuple[DataAction, DataDestination]:
        return {
            self.PROCESS: (DataAction.PROCESS, DataDestination.WORKFLOW_STATE),
            self.DISPLAY: (DataAction.DISPLAY, DataDestination.USER_SCREEN),
            self.MODEL: (DataAction.SEND_TO_MODEL, DataDestination.EXTERNAL_MODEL),
            self.TOOL: (DataAction.SEND_TO_PROVIDER, DataDestination.NETWORK_ENDPOINT),
            self.ARTIFACT: (DataAction.PERSIST, DataDestination.LOCAL_ARTIFACT_STORE),
            self.CLIPBOARD: (DataAction.COPY_TO_CLIPBOARD, DataDestination.WINDOWS_CLIPBOARD),
            self.MEMORY: (DataAction.WRITE_TO_MEMORY, DataDestination.MEMORY_STORE),
            self.KNOWLEDGE: (DataAction.WRITE_TO_KNOWLEDGE, DataDestination.KNOWLEDGE_STORE),
            self.EMBEDDING: (DataAction.EMBED, DataDestination.EXTERNAL_MODEL),
            self.INDEX: (DataAction.INDEX, DataDestination.LOCAL_ZVEC),
            self.PERSISTENCE: (DataAction.PERSIST, DataDestination.LOCAL_SQLITE),
            self.CACHE: (DataAction.CACHE, DataDestination.WORKFLOW_STATE),
        }[self]


@dataclass
class EnterpriseFlow:
    # Shared by child tasks: a tool's ingress must also contain its parent model/stream.
    protected: bool = False
    displayed_receipt: bool = False


_flow: ContextVar[EnterpriseFlow | None] = ContextVar("shovel_enterprise_egress", default=None)
_failure_receipt: ContextVar[bool] = ContextVar("shovel_enterprise_failure_receipt", default=False)


@contextmanager
def egress_scope(flow: EnterpriseFlow | None = None) -> Iterator[EnterpriseFlow]:
    existing = _flow.get()
    if existing is not None:
        yield existing
        return
    current = flow if flow is not None else EnterpriseFlow()
    token = _flow.set(current)
    try:
        yield current
    finally:
        _flow.reset(token)


@contextmanager
def execution_egress_scope() -> Iterator[None]:
    # Executions can return only validated JSON or fixed safe receipts, never a raw protected buffer.
    # Do not let a protected caller use a child Execution to reset its own containment.
    existing = _flow.get()
    if existing is None or existing.protected:
        with egress_scope():
            yield
    else:
        token = _flow.set(EnterpriseFlow())
        try:
            yield
        finally:
            _flow.reset(token)


def mark_enterprise() -> None:
    current = _flow.get()
    if current is not None:
        current.protected = True


def enterprise_active() -> bool:
    current = _flow.get()
    return current is not None and current.protected


@contextmanager
def chat_failure_receipt() -> Iterator[None]:
    token = _failure_receipt.set(True)
    try:
        yield
    finally:
        _failure_receipt.reset(token)


def require_personal_table_write(table: str | None, *, update: bool) -> None:
    if table == "chat_message" and update and _failure_receipt.get():
        return
    if table is None or table in {
        "chat_message",
        "conversation",
        "session_summary",
        "memory_working",
        "memory_episode",
        "memory_belief",
        "memory_procedure",
        "memory_revision",
        "document",
        "chunk",
        "document_derived",
        "blob_ref",
    }:
        require_ordinary(EgressChannel.PERSISTENCE)


def _protected(value: object, seen: set[int]) -> bool:
    if isinstance(
        value,
        ProtectedContent | ProtectedContentMetadata | ProtectedContentRef | ProtectionContext | ArtifactRef,
    ):
        return True
    if id(value) in seen:
        return False
    seen.add(id(value))
    if isinstance(value, BaseModel):
        return any(_protected(getattr(value, name), seen) for name in type(value).model_fields)
    if isinstance(value, Mapping):
        # A serialized protected reference is not ordinary JSON or permission to refetch it.
        if {"protection_context_ref", "content_digest"}.issubset(value) or {
            "protection_context",
            "provenance",
            "reference",
        }.issubset(value):
            return True
        return any(_protected(item, seen) for item in value.values())
    if isinstance(value, list | tuple | set | frozenset):
        return any(_protected(item, seen) for item in value)
    if is_dataclass(value) and not isinstance(value, type):
        return any(_protected(getattr(value, field.name), seen) for field in fields(value))
    return False


def require_ordinary(
    channel: EgressChannel, value: object = None, *, flow: EnterpriseFlow | None = None
) -> None:
    """No permit is inferred from READ, local hosting, a callback or a raw string."""
    current = flow if flow is not None else _flow.get()
    protected = _protected(value, set())
    if protected:
        mark_enterprise()
        if current is not None:
            current.protected = True
    if protected or (current is not None and current.protected):
        action, destination = channel.binding
        structlog.get_logger("shovel.protection").warning(
            "protection.egress_denied", action=action.value, destination=destination.value
        )
        raise ProtectionDeniedError("Enterprise content requires a reviewed protected egress adapter.")


def record_displayed_receipt() -> None:
    current = _flow.get()
    if current is not None:
        current.displayed_receipt = True


def require_safe_completion(value: object) -> None:
    current = _flow.get()
    if (
        current is not None
        and current.protected
        and (value is None or value == {} or (current.displayed_receipt and value == {"status": "displayed"}))
    ):
        return
    require_ordinary(EgressChannel.PERSISTENCE, value)
