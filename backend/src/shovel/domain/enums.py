from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ResourceState(StrEnum):
    """
    用户创建的资源状态

    状态的取值分别是：
    draft
    verifying
    active
    degraded
    paused
    revoked
    """

    DRAFT = "draft"
    VERIFYING = "verifying"
    ACTIVE = "active"
    DEGRADED = "degraded"
    PAUSED = "paused"
    REVOKED = "revoked"


class Sensitivity(StrEnum):
    """
    安全等级标签
    """

    NORMAL = "normal"
    PERSONAL = "personal"
    SECRET = "secret"  # noqa: S105

    @property
    def rank(self) -> int:
        return {"normal": 0, "personal": 1, "secret": 2}[self.value]



class ScanMode(StrEnum):
    """
    扫描模式定义
    """

    FULL_SWEEP = "full_sweep"     # 全面扫描，也是唯一允许驱动(Driver)
    INCREMENTAL = "incremental"   # 增量刷新
    TARGETED = "targeted"         # 表示由用户传入实际需要扫描的资源, 而且扫描只在这几个传入的资源上


class ScheduleKind(StrEnum):
    """
    Scedule 的分类
    """

    CRON = "cron"     # 定时跑
    INTERVAL = "interval"   #间隔多长时间跑
    WATCH  = "watch"    # 监控根据变化跑
    WEBHOOK = "webhook"
    MANUAL = "manual"    # 手动跑


class MisfirePolicy(StrEnum):
    """
    错过触发的处理规则
    """

    COALESCE = "coalesce"   # 多次合并成一次处理，也就是因为各种原因错过触发了之后，不管之前多少次都合并成一次进行触发操作
    SKIP = "skip"      # 不管错过了多少次，都跳过，等待下一次触发
    RUN_ALL = "run_all"  # 把所有错过的触发全部跑一遍




class JobState(StrEnum):
    """
    定义job的状态
    """

    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"

    @property
    def is_terminal(self) -> bool:
        return self in {
            JobState.SUCCEEDED, JobState.COMPLETED_WITH_ERRORS, JobState.FAILED,
            JobState.CANCELLED, JobState.SKIPPED,
        }



class Stage(StrEnum):
    """
    扫描整个pipeline的阶段说明
    """

    DISCOVER = "discover"
    FETCH = "fetch"
    PARSE = "parse"
    CHUNK = "chunk"
    EMBED = "embed"
    PERSIST = "persist"
    RECONCILE = "reconcile"



class ErrorKind(StrEnum):
    """
    错误类型
    """

    AUTH = "auth"
    NETWORK = "network"
    PARSE = "parse"
    QUOTA = "quota"
    INTERNAL = "internal"
    CANCELLED = "cancelled"
    PERMISSION = "permission"



class DocumentLifecycleState(StrEnum):
    """
    保存在Document 表里每条文档的生命周期状态
    """

    ACTIVE = "active"
    TOMBSTONED = "tombstoned"
    PURGED = "purged"
    INACCESSIBLE = "inaccessible"
    SKIPPED = "skipped"



class DocumentPipeState(StrEnum):
    """
    Document 处理pipeline的状态
    """
    PENDING = "pending"
    FETCHED = "fetched"
    PARSED = "parsed"
    CHUNKED = "chunked"
    ENRICHED = "enriched"
    INDEXED = "indexed"
    FAILED = "failed"
    SKIPPED = "skipped"



class Modality(StrEnum):
    """
    内容的形态

    这里的内容有很多种，例如Document, Chunk等等
    """

    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    TABLE = "table"



class DocClass(StrEnum):
    """
    文档的类型
    """

    PLAIN_TEXT = "plain_text"
    MARKDOWN = "markdown"
    CODE = "code"
    PDF_DOC = "pdf_doc"
    OFFICE_DOC = "office_doc"
    HTML = "html"
    EMAIL = "email"
    CHAT = "chat"
    TABLE = "table"
    IMAGE = "image"
    TRANSCRIPT = "transcript"
    CALENDAR = "calendar"
    UNKNOWN = "unknown"





@dataclass(frozen=True)
class LayerSpec:
    """
    用于控制内容的处理方式
    """
    should_embed: bool
    returnable: bool
    requires_stage: str | None


class Granularity(StrEnum):
    """
    搜索和读取的不同粒度
    """

    CHUNK = "chunk"    # 用于向量搜索
    PARENT = "parent"   # 用于读取，供大模型和人类阅读，也是expand_chunk 的目标 不需要向量化
    SECTION = "section"
    DOC_SUMMARY = "doc_summary"
    HYPOTHETICAL_QUESTION = "hypothetical_question"
    PROPOSITION = "proposition"

    @property
    def spec(self) -> LayerSpec:
        return _LAYERS[self]

    @property
    def should_embedded(self) -> bool:
        return self.spec.should_embed

    @property
    def returnable(self) -> bool:
        return self.spec.returnable

    @property
    def requires_stage(self) -> str | None:
        return self.spec.requires_stage




_LAYERS: dict[Granularity, LayerSpec] = {
    Granularity.CHUNK: LayerSpec(
        should_embed=True, returnable=True, requires_stage=None
    ),

    Granularity.PARENT: LayerSpec(
        should_embed=False, returnable=True, requires_stage=None
    ),

    Granularity.SECTION : LayerSpec(
        should_embed=True, returnable=True, requires_stage="summarize",
    ),

    Granularity.DOC_SUMMARY: LayerSpec(
        should_embed=True, returnable=True, requires_stage="summarize",
    ),

    Granularity.HYPOTHETICAL_QUESTION: LayerSpec(
        should_embed=True, returnable=False, requires_stage="summarize",
    ),

    Granularity.PROPOSITION: LayerSpec(
        should_embed=True, returnable=False, requires_stage="parse"
    ),

}


def _assert_layers_complete() -> None:
    missing = set(Granularity) - set(_LAYERS)

    if missing:
        raise RuntimeError(
            "Granularity memebers missing a LayerSpec:"
            f"{sorted(m.value for m in missing)}"
        )



# 先在导入的时候就检查一下
_assert_layers_complete()



class VectorState(StrEnum):
    PENDING = "pending"
    INDEXED = "indexed"
    ORPHAN = "orphan"
    SKIPPED = "skipped"


class DerivedKind(StrEnum):
    """
    表示原始文档生成内容类型
    """
    SUMMARY = "summary"
    KEYWORDS = "keywords"
    OUTLINE = "outline"
    QUESTIONS = "questions"
    ENTITIES = "entities"


class BlockKind(StrEnum):
    """
    Block的类型
    """
    RAW = "raw"
    EXTRACTED_TEXT = "extracted_text"
    TRANSCRIPT = "transcript"
    THUMBNAIL = "thumbnail"




class EntityKind(StrEnum):
    PERSON = "person"
    PROJECT = "project"
    ORG = "org"
    TOPIC = "topic"
    LOCATION = "location"


class MentionRole(StrEnum):
    AUTHOR = "author"
    RECIPIENT = "recipient"
    MENTIONED = "mentioned"
    OWNER = "owner"
    SPEAKER = "speaker"



class RelationKind(StrEnum):
    WORKS_ON = "works_on"
    REPORTS_TO = "reports_to"
    MEMBER_OF = "member_of"
    CORRESPONDED_WITH = "corresponded_with"
    ATTENDED = "attended"
    AUTHORED = "authored"
    ABOUT = "about"
    DEPENDS_ON = "depends_on"
    SUPERSEDES = "supersedes"
    RELATED_TO = "related_to"


class EdgeSource(StrEnum):
    STRUCTURAL = "structural"
    COOCCURRENCE = "cooccurrence"
    LLM = "llm"
    USER = "user"


class TaskState(StrEnum):
    OPEN = "open"
    DONE = "done"
    UNKNOWN = "unknown"


class DecisionState(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REVERTED = "reverted"



## 召回

class RetrievalRoute(StrEnum):

    SEMANTIC = "semantic"
    STRUCTURED = "structured"
    GRAPH = "graph"
    TEMPORAL = "temporal"
    MEMORY = "memory"
    CATALOG = "catalog"
    SUBSCRIPTION = "subscription"
    LIVE = "live"
    COMPUTE = "compute"
    ASK_USER = "ask_user"
    HYBRID = "hybrid"


class FeedbackSignal(StrEnum):
    CITED = "cited"
    OPENED = "opened"
    THUMBS_UP = "thumbs_up"
    THUMBS_DOWN = "thumbs_down"
    IGNORED = "ignored"


class AgentScopeMode(StrEnum):
    READ = "read"
    SUMMARY_ONLY = "summary_only"
    DENIED = "denied"



class DeletionScope(StrEnum):
    DOC = "doc"
    DOC_OLD_REV = "doc_old_rev"
    RESOURCE = "resource"
    MODEL_VERSION = "model_version"
    CHUNK_IDS = "chunk_ids"
    MEMORY = "memory"



class RecencyBucket(StrEnum):
    D1 = "1d"
    D7 = "7d"
    D30 = "30d"
    D90 = "90d"
    OLDER = "older"


    @classmethod
    def of(
        cls,
        ts: int | None,
        *,
        now: int,
    ) -> RecencyBucket:
        if ts is None:
            return cls.OLDER
        age_days = (now - ts) / 86400.0

        if age_days <= 1:
            return cls.D1
        if age_days <= 7:
            return cls.D7
        if age_days <= 30:
            return cls.D30
        if age_days <= 90:
            return cls.D90

        return cls.OLDER



# 关于记忆的定义

class MemoryKind(StrEnum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


class MemoryValidityState(StrEnum):

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    RETRACTED = "retracted"
    ARCHIVED = "archived"


class MemorySource(StrEnum):

    STATED = "stated"
    CONFIRMED = "confirmed"
    INFERRED = "inferred"
    CORRECTED = "corrected"
    IMPORTED = "imported"



class MemoryPredicate(StrEnum):
    """Closed predicate set for semantic memory.

    A closed set is what makes contradiction detection possible: two beliefs
    conflict when they share (subject, predicate) and the predicate is
    single-valued. A free-text predicate makes that undecidable.

    Naming rule: attribute-style predicates read ``has_*`` (subject-verb-object
    order), never ``*_is`` -- ``user has_timezone Asia/Shanghai`` reads as a
    sentence, ``user timezone_is ...`` does not.

    ``works_on`` and ``reports_to`` deliberately do NOT appear here: they are
    relations between two entities and belong in :class:`RelationKind` on the
    ``entity_edge`` table. Defining them twice would split the answer to
    "who works on Shovel?" across two stores that cannot be joined.
    """

    PREFERS = "prefers"                # single-valued per object_domain
    DISLIKES = "dislikes"
    USES = "uses"
    WORKS_AT = "works_at"              # single-valued
    HAS_ROLE = "has_role"              # single-valued
    HAS_TIMEZONE = "has_timezone"      # single-valued
    HAS_LANGUAGE = "has_language"      # single-valued
    HAS_GOAL = "has_goal"
    HAS_CONSTRAINT = "has_constraint"
    DECIDES = "decides"
    KNOWS_ABOUT = "knows_about"

    @property
    def single_valued(self) -> bool:
        """Single-valued predicates auto-supersede; multi-valued ones accumulate.

        "has_timezone" can only have one answer -- a new one replaces the old.
        "uses" can have many -- Python AND SQLite AND Qdrant all coexist.
        """
        return self in {
            MemoryPredicate.HAS_ROLE, MemoryPredicate.HAS_TIMEZONE,
            MemoryPredicate.HAS_LANGUAGE, MemoryPredicate.WORKS_AT,
        }


class MemoryLinkKind(StrEnum):
    """What a memory is attached to. This is the seam between memory and the
    knowledge base: a memory about "Shovel" must point at the SAME ent_shovel
    the documents point at, or the two stores can never be answered together."""

    ENTITY = "entity"
    DOCUMENT = "document"
    CHUNK = "chunk"
    DECISION = "decision"
    RESOURCE = "resource"


class WorkingSlot(StrEnum):
    """Working-memory slot types, so compaction can prioritise by kind."""

    GOAL = "goal"                # never compact: the task's objective
    PLAN = "plan"                # rarely compact
    EVIDENCE = "evidence"        # compact to ids + summary
    OBSERVATION = "observation"  # compact aggressively
    SCRATCH = "scratch"          # drop first
    ERROR = "error"              # keep: repeated failures must stay visible
