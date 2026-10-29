"""Memory values and policies. No persistence, vector-store or Agent Framework types."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum


class MemoryType(StrEnum):
    SEMANTIC = "semantic"
    EPISODIC = "episodic"
    PROCEDURAL = "procedural"


class MemoryState(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    DELETED = "deleted"
    REJECTED = "rejected"


class ScopeKind(StrEnum):
    USER = "user"
    WORKSPACE = "workspace"
    AGENT = "agent"
    SESSION = "session"


@dataclass(frozen=True)
class MemoryScope:
    workspace_id: str
    user_id: str = "local-user"
    agent_id: str = "shovel"
    session_id: str = ""
    kind: ScopeKind = ScopeKind.AGENT

    def __post_init__(self) -> None:
        if not self.workspace_id or not self.user_id or not self.agent_id:
            raise ValueError("Memory ownership must be explicit.")
        if self.kind == ScopeKind.SESSION and not self.session_id:
            raise ValueError("Session memory requires a session ID.")

    def owns(self, other: MemoryScope) -> bool:
        if (self.workspace_id, self.user_id) != (other.workspace_id, other.user_id):
            return False
        if other.kind in (ScopeKind.USER, ScopeKind.WORKSPACE):
            return True
        if self.agent_id != other.agent_id:
            return False
        return other.kind == ScopeKind.AGENT or bool(self.session_id and self.session_id == other.session_id)


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


@dataclass(frozen=True)
class MemoryCandidate:
    text: str
    kind: MemoryType = MemoryType.SEMANTIC
    subject: str = "user"
    predicate: str = "prefers"
    object: str = ""
    topic: str = ""
    confidence: float = 0.8
    importance: float = 0.5
    evidence: str = ""
    replaces: bool = False
    expires_at: int | None = None
    occurred_at: int | None = None
    merge_with: str | None = None

    @property
    def fingerprint(self) -> str:
        key = (
            f"{self.kind}|{normalize(self.subject)}|{self.predicate}|"
            f"{normalize(self.topic)}|{normalize(self.object or self.text)}"
        )
        return hashlib.sha256(key.encode()).hexdigest()


@dataclass(frozen=True)
class Provenance:
    source_kind: str
    conversation_id: str = ""
    message_id: str = ""
    evidence: str = ""
    extractor_version: str = "native-v1"


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    kind: MemoryType
    text: str
    scope: MemoryScope
    state: MemoryState
    fingerprint: str
    revision: int
    recorded_at: int
    confidence: float
    importance: float
    access_count: int = 0
    last_accessed_at: int | None = None
    expires_at: int | None = None
    subject: str = ""
    predicate: str = ""
    object: str = ""
    topic: str = ""
    superseded_by: str | None = None
    model_version: str | None = None
    provenance: list[dict[str, str]] = field(default_factory=list)
    occurred_at: int | None = None


@dataclass(frozen=True)
class VectorHit:
    id: str
    kind: MemoryType
    revision: int
    similarity: float


@dataclass(frozen=True)
class IndexIntent:
    id: str
    memory_id: str
    kind: MemoryType
    revision: int
    action: str


class ExtractionCancelled(Exception):
    """Source, ownership or generation changed before an atomic write."""


_SECRET = re.compile(
    r"(?i)(?:api[_ -]?key|password|passwd|secret|access[_ -]?token|bearer|密码|密钥|令牌)"
    r"|(?:sk-[a-zA-Z0-9_-]{12,})|-----BEGIN [A-Z ]*PRIVATE KEY"
)
_EXTERNAL = re.compile(
    r"```|^\s*>|(?:网页|文档|工具|网站|邮件|文章).{0,8}(?:说|写|显示|内容)"
    r"|(?:webpage|document|tool output|website|email)\s+(?:says|contains|states)"
    r"|[\"“「『]",
    re.IGNORECASE | re.MULTILINE,
)
_CORRECTION = re.compile(
    r"改为|改用|不再|替代|更正|决定|选择|instead|replace|no longer|switch|decid|choose|chosen",
    re.IGNORECASE,
)


def admissible(candidate: MemoryCandidate, source: str, min_confidence: float) -> bool:
    """Fail closed for credentials, ambiguous quotations and unsupported evidence."""
    return (
        8 <= len(candidate.text.strip()) <= 1500
        and candidate.kind != MemoryType.PROCEDURAL
        and min_confidence <= candidate.confidence <= 1
        and 0 <= candidate.importance <= 1
        and bool(candidate.evidence.strip())
        and candidate.evidence in source
        and not _SECRET.search(
            source + candidate.text + candidate.object + candidate.topic + candidate.subject
        )
        and not _EXTERNAL.search(source)
        and (not candidate.replaces or bool(_CORRECTION.search(candidate.evidence)))
    )


def eligible_source(source: str) -> bool:
    return not (_SECRET.search(source) or _EXTERNAL.search(source))


def same_fact(record: MemoryRecord, candidate: MemoryCandidate) -> bool:
    return record.fingerprint == candidate.fingerprint or normalize(record.text) == normalize(candidate.text)


def conflicts(record: MemoryRecord, candidate: MemoryCandidate) -> bool:
    return (
        record.kind == candidate.kind == MemoryType.SEMANTIC
        and bool(candidate.topic)
        and normalize(record.subject) == normalize(candidate.subject)
        and record.predicate == candidate.predicate
        and normalize(record.topic) == normalize(candidate.topic)
        and normalize(record.object) != normalize(candidate.object)
        and candidate.replaces
    )
