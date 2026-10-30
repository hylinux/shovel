"""全部 ORM 模型。

导入这个包 = 把所有表注册进 ``Base.metadata``。
``create_all`` 只会建它"见过"的表, 而它只见过被导入过的模块,
所以建表入口必须先导入这里, 而不是指望调用方碰巧 import 过某个模型。
"""

from __future__ import annotations

from .automation import ProactiveService
from .capability import (
    CapabilityDefinition,
    CapabilityDependency,
    CapabilityVersion,
    CapabilityVersionAvailability,
)
from .config import Resource, ScanProfile, Schedule
from .content import BlobRef, Chunk, Document, DocumentDerived
from .control import ExecutionWakeup
from .execution import ExecutionAttempt, ExecutionStep, Job, JobEvent
from .interaction import InputRequest, Interaction, Notification, NotificationDelivery
from .knowledge import (
    Entity,
    EntityEdge,
    FactDecision,
    FactEvent,
    FactMessage,
    FactTask,
    Mention,
)
from .memory import (
    MemoryBelief,
    MemoryEpisode,
    MemoryLink,
    MemoryProcedure,
    MemoryRevision,
    MemoryWorking,
    StandingQuery,
)
from .retrieval import (
    AgentScope,
    AppSetting,
    DeletionOutbox,
    EvalCase,
    RetrievalFeedback,
    RetrievalLog,
    SchemaMigration,
)
from .runtime import ChatMessage, Conversation, MemoryEpoch, Operation
from .security import ApprovalRequest

__all__ = [
    "AgentScope",
    "AppSetting",
    "ApprovalRequest",
    "BlobRef",
    "CapabilityDefinition",
    "CapabilityDependency",
    "CapabilityVersion",
    "CapabilityVersionAvailability",
    "ChatMessage",
    "Chunk",
    "Conversation",
    "DeletionOutbox",
    "Document",
    "DocumentDerived",
    "Entity",
    "EntityEdge",
    "EvalCase",
    "ExecutionAttempt",
    "ExecutionStep",
    "ExecutionWakeup",
    "FactDecision",
    "FactEvent",
    "FactMessage",
    "FactTask",
    "InputRequest",
    "Interaction",
    "Job",
    "JobEvent",
    "MemoryBelief",
    "MemoryEpisode",
    "MemoryEpoch",
    "MemoryLink",
    "MemoryProcedure",
    "MemoryRevision",
    "MemoryWorking",
    "Mention",
    "Notification",
    "NotificationDelivery",
    "Operation",
    "ProactiveService",
    "Resource",
    "RetrievalFeedback",
    "RetrievalLog",
    "ScanProfile",
    "Schedule",
    "SchemaMigration",
    "StandingQuery",
]
