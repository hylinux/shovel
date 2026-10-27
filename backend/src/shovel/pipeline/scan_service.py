# ---------------------------------------------------------
# 扫描编排
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
# ---------------------------------------------------------
"""把连接器、解析器、切块器、embedder、向量库缝成一次完整的扫描。

这是 ``Connector.discover()`` 的消费者, 也是整个模块存在的理由。

## 一次扫描的骨架

```text
 preflight   资源可扫吗? 连接器支持这个模式吗?     -> 不行就直接拒绝, 不建 Job
 discover    列举条目, 逐条落成 document 行        -> is_discovery_complete = True
 per-doc     fetch -> parse -> chunk -> embed      -> 一篇失败不影响其余
 persist     写 SQLite (权威) + 写 Zvec (可重建)
 reconcile   本轮没见到的文档立碑                  -> 仅全量扫描
```

## 三个必须写清楚的设计决定

**1. discover 必须先跑完, 再处理文档。**

不是"边列举边处理"。多一次数据库往返, 换来的是 ``job.discovered``
这个进度条分母, 以及 ``is_discovery_complete`` 这个删除对账的硬门闸。
没有它, 一次在第 3000 个文件上断网的扫描, 会让剩下的 17000 个文档被
判定为"已从源端消失"而集体立碑 —— 那是在用户毫无察觉的情况下删掉
他 85% 的知识库。

**2. 单篇失败与整批失败分开处理。**

``ParseError`` 系列是单篇的: 记 ``job_event``, 标 ``pipe_state=failed``,
继续下一篇。``EmbeddingError`` / ``VectorSinkError`` 是作业级的: 后面
每一篇都会以同样方式失败, 继续跑只是把同一条错误刷一万遍, 所以直接
中止 Job。这条界线就是下面 ``except`` 子句的分布。

**3. 内容没变且索引完整、版本匹配时才跳过。**

不是 ``modified_at``。很多同步工具 (OneDrive、git checkout) 会在内容
完全没变的情况下刷新 mtime。本地使用 size + mtime_ns 的廉价指纹,
因此会保守地重处理 touch 过的文件, 但不使用秒级 mtime 作为发现过滤条件。
除内容指纹外, 还要检查流水线状态、待补向量和处理配置/模型版本。
完成标记只在本篇向量落盘之后提交。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from time import perf_counter

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from shovel.db.base import now_ts
from shovel.db.model.config import Resource, ScanProfile
from shovel.db.model.content import Chunk, Document, ProfileDocument
from shovel.db.model.execution import Job, JobEvent
from shovel.db.model.retrieval import DeletionOutbox
from shovel.domain.enums import (
    DeletionScope,
    DocClass,
    DocumentLifecycleState,
    DocumentPipeState,
    ErrorKind,
    JobState,
    LogLevel,
    ScanMode,
    ScheduleKind,
    Stage,
    VectorState,
)
from shovel.exceptions.pipeline import (
    EmbeddingError,
    ParseError,
    PipelineError,
    ResourceNotScannableError,
    ScanModeUnsupportedError,
    UnsupportedDocumentError,
    VectorSinkError,
)
from shovel.exceptions.resource import ConnectorDiscoveryError
from shovel.pipeline.chunker import (
    CHUNKER_VERSION,
    ChunkDraft,
    ChunkerConfig,
    chunk_document,
    default_config_for,
)
from shovel.pipeline.cleanup import IndexCleanupService
from shovel.pipeline.embedder import Embedder, NullEmbedder
from shovel.pipeline.parsers import ParsedDocument, ParseHint, can_parse, parse_bytes
from shovel.pipeline.parsers.base import DEFAULT_MAX_CHARS
from shovel.pipeline.parsers.registry import parser_versions
from shovel.pipeline.sink import ChunkVectorSink, NullVectorSink, VectorRecord
from shovel.runtime.diagnostics import trace
from shovel.services.connector_base import (
    Connector,
    ConnectorCapability,
    DiscoveredItem,
    DiscoveryScope,
    connector_session,
)
from shovel.services.resource_service import ResourceService

#: 心跳间隔 (秒)。``idx_job_zombie`` 靠它识别"进程被 kill 了但 Job
#: 还停在 running"的僵尸作业。
_HEARTBEAT_SECONDS = 30


@dataclass(slots=True)
class ScanOutcome:
    """一次扫描的结果摘要。

    是 ``Job`` 的一个只读投影, 而不是直接把 ORM 对象返回给 CLI ——
    session 关掉之后再访问 ORM 属性会抛 DetachedInstanceError,
    而那种错误会发生在 CLI 渲染表格的时候, 离真正的原因很远。
    """

    job_id: str
    state: JobState
    resource_name: str
    scan_mode: ScanMode

    discovered: int = 0
    skipped_unchanged: int = 0
    fetched: int = 0
    parsed: int = 0
    chunked: int = 0
    embedded: int = 0
    failed_docs: int = 0
    chunks_written: int = 0
    vectors_written: int = 0
    bytes_read: int = 0
    tombstoned: int = 0

    error_kind: ErrorKind | None = None
    error_detail: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.state in (JobState.SUCCEEDED, JobState.COMPLETED_WITH_ERRORS)


class ScanService:
    """执行一次扫描。

    和 ``ResourceService`` 一样, 每个阶段自己开 session。一次扫描可能跑
    几十分钟, 全程握着一个事务会让 WAL 无限增长, 也会让 UI 读到的永远是
    扫描开始那一刻的旧数据。按文档提交则是: 扫到哪, 用户就能查到哪。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        resources: ResourceService,
        *,
        embedder: Embedder | None = None,
        sink: ChunkVectorSink | None = None,
        stopping: asyncio.Event | None = None,
    ) -> None:
        self._sessions = session_factory
        self._resources = resources
        self._embedder = embedder or NullEmbedder()
        self._sink = sink or NullVectorSink()
        self._cleanup = IndexCleanupService(session_factory, self._sink)
        self._stopping = stopping
        self._processing_version: str | None = None

    @property
    def _embedding_enabled(self) -> bool:
        return not isinstance(self._embedder, NullEmbedder)

    def _recovery_condition(self) -> ColumnElement[bool]:
        processed_hash = func.coalesce(
            Document.indexed_hash, func.json_extract(Document.source_meta, "$.chunked_hash")
        )
        conditions = [
            Document.pipe_state.not_in(
                (DocumentPipeState.INDEXED,)
                if self._embedding_enabled
                else (DocumentPipeState.CHUNKED, DocumentPipeState.INDEXED)
            ),
            processed_hash.is_(None),
            processed_hash != Document.content_hash,
            Document.chunker_version.is_(None),
            Document.chunker_version != CHUNKER_VERSION,
        ]
        if self._processing_version is not None:
            fingerprint = func.json_extract(Document.source_meta, "$.processing_fingerprint")
            conditions.extend([fingerprint.is_(None), fingerprint != self._processing_version])
        if self._embedding_enabled:
            conditions.extend(
                [
                    Document.model_version.is_(None),
                    Document.model_version != self._embedder.model_version,
                    select(Chunk.id)
                    .where(
                        Chunk.doc_uri == Document.uri,
                        Chunk.resource_id == Document.resource_id,
                        Chunk.vector_state == VectorState.PENDING,
                    )
                    .exists(),
                ]
            )
        return or_(*conditions)

    # ----------------------------------------------------------------- #
    # 入口
    # ----------------------------------------------------------------- #
    async def scan(
        self,
        ref: str,
        *,
        mode: ScanMode | None = None,
        profile_name: str | None = None,
        target_ids: Sequence[str] = (),
        limit: int | None = None,
        force: bool = False,
        job_id: str | None = None,
    ) -> ScanOutcome:
        """扫一个资源。

        ``limit`` 只截断"本轮处理多少篇", **不**影响 discover 的完整性 ——
        因为删除对账依赖完整的观测集。用 limit 跑出来的 Job 仍然会把
        每一个被发现的条目写进 document 表, 只是其中一部分停在 PENDING。
        """

        resource, profile, connector, scan_mode = await self._preflight(
            ref,
            mode=mode,
            profile_name=profile_name,
            target_ids=target_ids,
        )

        job_id = await self._open_job(resource, profile, scan_mode, job_id=job_id)
        started = perf_counter()
        trace(
            "scan.started",
            job_id=job_id,
            resource_id=resource.id,
            profile_id=profile.id,
            mode=scan_mode.value,
            embedding_enabled=self._embedding_enabled,
        )

        outcome = ScanOutcome(
            job_id=job_id,
            state=JobState.RUNNING,
            resource_name=resource.name,
            scan_mode=scan_mode,
        )

        heartbeat = asyncio.create_task(self._heartbeat(job_id))

        try:
            if await self._cancel_requested(job_id):
                raise asyncio.CancelledError
            async with connector_session(connector):
                await self._run(
                    job_id=job_id,
                    resource=resource,
                    profile=profile,
                    connector=connector,
                    scan_mode=scan_mode,
                    target_ids=target_ids,
                    limit=limit,
                    force=force,
                    outcome=outcome,
                )

        except (PipelineError, ConnectorDiscoveryError) as exc:
            # 作业级失败: embedding 端点挂了、向量库写不进去。
            # 已经处理完的文档保持原样 —— 它们是有效成果, 下次扫描
            # 会因为 indexed_hash 已对上而直接跳过。
            outcome.state = JobState.FAILED
            outcome.error_kind = _error_kind_of(exc)
            outcome.error_detail = exc.message
            await self._log(job_id, LogLevel.ERROR, None, "JOB_ABORTED", exc.message)

        except asyncio.CancelledError:
            outcome.state = JobState.CANCELLED
            outcome.error_kind = ErrorKind.CANCELLED
            await self._log(job_id, LogLevel.WARN, None, "JOB_CANCELLED", "扫描被取消。")
            raise

        except Exception as exc:
            outcome.state = JobState.FAILED
            outcome.error_kind = ErrorKind.INTERNAL
            outcome.error_detail = str(exc)
            await self._log(job_id, LogLevel.ERROR, None, "JOB_ABORTED", str(exc))
            raise

        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

            await self._close_job(job_id, outcome)
            trace(
                "scan.completed",
                job_id=job_id,
                state=outcome.state.value,
                discovered=outcome.discovered,
                skipped=outcome.skipped_unchanged,
                parsed=outcome.parsed,
                failed_docs=outcome.failed_docs,
                chunks_written=outcome.chunks_written,
                vectors_written=outcome.vectors_written,
                tombstoned=outcome.tombstoned,
                elapsed_ms=round((perf_counter() - started) * 1000, 2),
            )

        return outcome

    async def prepare_job(
        self,
        ref: str,
        *,
        mode: ScanMode | None = None,
        profile_name: str | None = None,
        target_ids: Sequence[str] = (),
    ) -> Job:
        """Validate submission and return a queued row for the caller's transaction."""
        resource, profile, _connector, scan_mode = await self._preflight(
            ref,
            mode=mode,
            profile_name=profile_name,
            target_ids=target_ids,
        )
        return Job(
            resource_id=resource.id,
            scan_profile_id=profile.id,
            fired_by_kind=ScheduleKind.MANUAL,
            scan_mode=scan_mode,
            state=JobState.QUEUED,
        )

    # ----------------------------------------------------------------- #
    # preflight
    # ----------------------------------------------------------------- #
    async def _preflight(
        self,
        ref: str,
        *,
        mode: ScanMode | None,
        profile_name: str | None,
        target_ids: Sequence[str],
    ) -> tuple[Resource, ScanProfile, Connector, ScanMode]:
        """在建 Job **之前**把能拒绝的都拒绝掉。

        为什么不先建 Job 再检查: 一个"因为资源是 DRAFT 所以立刻失败"的
        Job 对用户没有任何价值, 只会把作业历史填满噪声。真正的失败
        (扫到一半断网) 才值得留一条记录。
        """

        resource = await self._resources.resolve(ref)

        if not resource.is_scannable:
            raise ResourceNotScannableError(resource.name, resource.state.value)

        profile = await self._load_profile(resource.id, profile_name)
        connector = await self._resources.build_connector(resource.id)

        # 用户点名了条目 = TARGETED, 不管 profile 上配的是什么。
        # 显式意图优先于配置, 否则 `shovel scan --target a.md` 会在一个
        # 增量 profile 上被水位线过滤掉, 用户会觉得命令没生效。
        scan_mode = ScanMode.TARGETED if target_ids else (mode or profile.scan_mode)

        if not connector.spec.supports_mode(scan_mode):
            raise ScanModeUnsupportedError(connector.spec.kind, scan_mode.value)

        return resource, profile, connector, scan_mode

    async def _load_profile(self, resource_id: str, name: str | None) -> ScanProfile:
        from shovel.services.resource_service import DEFAULT_PROFILE_NAME

        stmt = select(ScanProfile).where(ScanProfile.resource_id == resource_id)
        stmt = stmt.where(ScanProfile.name == (name or DEFAULT_PROFILE_NAME))

        async with self._sessions() as session:
            profile = (await session.execute(stmt)).scalar_one_or_none()

        if profile is None:
            raise PipelineError(
                f"资源没有名为 '{name or DEFAULT_PROFILE_NAME}' 的扫描配置。",
                hint="用 `shovel resource show` 查看该资源有哪些 profile。",
            )

        if not profile.is_enabled:
            raise PipelineError(f"扫描配置 '{profile.name}' 已停用。")
        return profile

    # ----------------------------------------------------------------- #
    # 主流程
    # ----------------------------------------------------------------- #
    async def _run(
        self,
        *,
        job_id: str,
        resource: Resource,
        profile: ScanProfile,
        connector: Connector,
        scan_mode: ScanMode,
        target_ids: Sequence[str],
        limit: int | None,
        force: bool,
        outcome: ScanOutcome,
    ) -> None:
        identity = {
            "parsers": parser_versions(),
            "parser_default_max_chars": DEFAULT_MAX_CHARS,
            "chunker_version": CHUNKER_VERSION,
            "defaults": {kind.value: asdict(default_config_for(kind)) for kind in DocClass},
            "pipeline": profile.pipeline,
        }
        self._processing_version = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        if self._embedding_enabled:
            await self._cleanup.drain()

        # ---------- discover ----------
        await self._set_stage(job_id, Stage.DISCOVER)

        since = (
            await self._watermark(resource.id, profile.id)
            if scan_mode is ScanMode.INCREMENTAL and not force
            else None
        )

        scope = DiscoveryScope(
            include=tuple(profile.include),
            exclude=tuple(profile.exclude),
            max_file_bytes=profile.max_file_bytes,
            time_window_days=profile.time_window_days,
            target_ids=tuple(target_ids),
            since=since,
        )

        pending = await self._discover(
            job_id=job_id,
            profile_id=profile.id,
            resource=resource,
            connector=connector,
            scope=scope,
            outcome=outcome,
            force=force,
        )
        trace(
            "scan.discovery.completed",
            job_id=job_id,
            discovered=outcome.discovered,
            pending=len(pending),
            skipped=outcome.skipped_unchanged,
        )

        # 这一行是删除对账的门闸。放在 discover 完整跑完之后, 一行都
        # 不能提前 —— 详见模块顶部的说明。
        delete_detect = connector.spec.supports(ConnectorCapability.DELETE_DETECT)
        await self._mark_discovery_complete(job_id, outcome.discovered, delete_detect=delete_detect)

        # ---------- per-document ----------
        await self._set_stage(job_id, Stage.FETCH)

        processed = 0

        for item in pending:
            if limit is not None and processed >= limit:
                break

            if await self._cancel_requested(job_id):
                raise asyncio.CancelledError

            try:
                written = await self._process(
                    job_id=job_id,
                    resource=resource,
                    profile=profile,
                    connector=connector,
                    item=item,
                    outcome=outcome,
                )
            except ParseError as exc:
                # 单篇级: 记下来, 继续。
                await self._fail_document(
                    job_id,
                    resource.id,
                    item,
                    exc,
                    outcome,
                )
                written = False

            if written:
                processed += 1
            await self._save_progress(job_id, outcome)

        # ---------- reconcile ----------
        if await self._cancel_requested(job_id):
            raise asyncio.CancelledError
        if scan_mode is ScanMode.FULL_SWEEP and delete_detect:
            await self._set_stage(job_id, Stage.RECONCILE)
            outcome.tombstoned = await self._reconcile(job_id, resource.id)
            if self._embedding_enabled:
                await self._cleanup.drain(resource_id=resource.id)
        elif scan_mode is ScanMode.FULL_SWEEP:
            warning = "连接器未声明完整删除检测能力,本轮不会按缺失条目删除索引。"
            outcome.warnings.append(warning)
            await self._log(job_id, LogLevel.WARN, None, "RECONCILE_UNSUPPORTED", warning)

        outcome.state = JobState.COMPLETED_WITH_ERRORS if outcome.failed_docs else JobState.SUCCEEDED

    # ----------------------------------------------------------------- #
    # discover
    # ----------------------------------------------------------------- #
    async def _discover(
        self,
        *,
        job_id: str,
        profile_id: str,
        resource: Resource,
        connector: Connector,
        scope: DiscoveryScope,
        outcome: ScanOutcome,
        force: bool,
    ) -> list[DiscoveredItem]:
        """列举条目, 逐条 upsert 进 document 表, 返回需要处理的那些。

        无论要不要处理, **每个被发现的条目都会更新 ``last_seen_run``**。
        这是对账的基础: "本轮没见到"必须严格等于"源端真的没有了",
        而不是"本轮跳过了它"。少记一次 last_seen_run, 就会给一篇完好
        的文档立碑。
        """

        seen: list[DiscoveredItem] = []

        async with self._sessions() as session:
            profile = await session.get(ScanProfile, profile_id)
            if profile is None:
                raise PipelineError("扫描 Profile 已不存在。")
            async for item in connector.discover(scope):
                if outcome.discovered % 100 == 0:
                    await session.commit()
                    await self._save_progress(job_id, outcome)
                    if await self._cancel_requested(job_id):
                        raise asyncio.CancelledError
                outcome.discovered += 1

                other_pipelines = await session.scalars(
                    select(ScanProfile.pipeline)
                    .join(
                        ProfileDocument,
                        ProfileDocument.profile_id == ScanProfile.id,
                    )
                    .where(
                        ProfileDocument.resource_id == resource.id,
                        ProfileDocument.doc_uri == item.uri,
                        ProfileDocument.profile_id != profile_id,
                    )
                )
                if any(pipeline != profile.pipeline for pipeline in other_pipelines):
                    raise PipelineError(
                        "重叠 Profile 使用了不同的流水线配置;一个资源内同一文档仅共享一份索引。",
                        hint="请统一这些 Profile 的 pipeline,或使用不重叠的 include/exclude。",
                    )
                hint = _hint_of(resource, item)
                document = await self._upsert_document(
                    session,
                    job_id,
                    resource,
                    item,
                )
                membership = await session.get(ProfileDocument, (profile_id, item.uri, resource.id))
                if membership is None:
                    session.add(
                        ProfileDocument(
                            profile_id=profile_id,
                            doc_uri=item.uri,
                            resource_id=resource.id,
                            last_seen_run=job_id,
                        )
                    )
                else:
                    membership.last_seen_run = job_id

                if not can_parse(hint):
                    if document.pipe_state is not DocumentPipeState.SKIPPED:
                        session.add(
                            DeletionOutbox(
                                scope=DeletionScope.DOC,
                                resource_id=resource.id,
                                doc_uri=item.uri,
                            )
                        )
                    document.lifecycle_state = DocumentLifecycleState.SKIPPED
                    document.pipe_state = DocumentPipeState.SKIPPED
                    document.skip_reason = f"不支持的类型: {hint.suffix or '(无扩展名)'}"
                    continue

                # 旧数据可能 hash 已匹配但向量仍 pending, 不能仅靠内容判定完成。
                requires_processing = (
                    await session.execute(
                        select(Document.uri).where(
                            Document.uri == item.uri,
                            Document.resource_id == resource.id,
                            self._recovery_condition(),
                        )
                    )
                ).scalar_one_or_none() is not None
                if not force and not requires_processing:
                    outcome.skipped_unchanged += 1
                    continue

                seen.append(item)

            await session.commit()

        if self._embedding_enabled:
            await self._cleanup.drain(resource_id=resource.id)

        return seen

    async def _upsert_document(
        self,
        session: AsyncSession,
        job_id: str,
        resource: Resource,
        item: DiscoveredItem,
    ) -> Document:
        uri = item.uri
        document = await session.get(Document, (uri, resource.id))

        if document is None:
            document = Document(
                uri=uri,
                resource_id=resource.id,
                authority=resource.authority,
            )
            session.add(document)

        document.doc_class = item.doc_class
        document.modality = item.modality
        document.content_hash = item.content_hash
        document.size_bytes = item.size_bytes
        document.title = item.title
        document.modified_at_src = item.modified_at
        document.last_seen_run = job_id
        document.last_seen_at = now_ts()

        # 曾经被判定为"消失了"的文档又出现了: 撤销墓碑。
        # 这很常见 —— 用户把文件移走再移回来、外接硬盘重新插上。
        if document.lifecycle_state is not DocumentLifecycleState.ACTIVE:
            document.lifecycle_state = DocumentLifecycleState.ACTIVE
            document.tombstoned_at = None
            document.skip_reason = None

        source_meta = dict(document.source_meta or {})
        source_meta.update(item.extra)
        source_meta["external_id"] = item.external_id
        document.source_meta = source_meta

        return document

    # ----------------------------------------------------------------- #
    # 单篇处理
    # ----------------------------------------------------------------- #
    async def _process(
        self,
        *,
        job_id: str,
        resource: Resource,
        profile: ScanProfile,
        connector: Connector,
        item: DiscoveredItem,
        outcome: ScanOutcome,
    ) -> bool:
        """fetch -> parse -> chunk -> embed -> persist。"""

        started = perf_counter()
        document_id = hashlib.sha256(item.uri.encode()).hexdigest()[:16]
        trace("document.fetch", job_id=job_id, document_id=document_id)
        payload = await _read(connector, item)
        outcome.fetched += 1
        outcome.bytes_read += len(payload)

        hint = _hint_of(resource, item)
        if "parser" in profile.pipeline:
            hint = ParseHint(
                uri=hint.uri,
                filename=hint.filename,
                doc_class=hint.doc_class,
                size_bytes=hint.size_bytes,
                max_chars=profile.pipeline["parser"]["max_chars"],
                content_format=hint.content_format,
            )

        # 解析是 CPU 密集的 (pypdf 解压、lxml 建树)。丢进线程池,
        # 让事件循环还能响应取消信号和心跳。
        trace("document.parse", job_id=job_id, document_id=document_id, bytes_read=len(payload))
        parsed: ParsedDocument = await asyncio.to_thread(parse_bytes, payload, hint)
        outcome.parsed += 1

        if parsed.truncated:
            outcome.warnings.append(f"{item.external_id}: 内容过长, 已截断。")
            await self._log(
                job_id,
                LogLevel.WARN,
                item.uri,
                "PARSE_TRUNCATED",
                "文档内容超过上限, 只索引了前一部分。",
            )

        content_hash = item.content_hash or f"len:{len(payload):x}"

        config = _chunker_config(profile, parsed)
        trace("document.chunk", job_id=job_id, document_id=document_id)
        drafts = chunk_document(
            parsed,
            doc_uri=item.uri,
            content_hash=content_hash,
            config=config,
            resource_id=resource.id,
        )

        if not drafts:
            raise ParseError(item.uri, "切块后没有产生任何内容。")

        outcome.chunked += 1

        # ---------- embed ----------
        embeddable = [d for d in drafts if d.granularity.should_embedded]
        vectors: list[list[float]] = []

        if embeddable and self._embedding_enabled:
            await self._set_stage(job_id, Stage.EMBED)
            trace("document.embed", job_id=job_id, document_id=document_id, chunks=len(embeddable))
            vectors = await self._embedder.embed([d.embed_text for d in embeddable])

            if len(vectors) != len(embeddable):
                raise EmbeddingError(
                    self._embedder.model_version,
                    f"需要 {len(embeddable)} 个向量, 拿到 {len(vectors)} 个。",
                )

            outcome.embedded += 1

        by_id = {draft.id: vector for draft, vector in zip(embeddable, vectors, strict=False)}

        # ---------- persist ----------
        await self._set_stage(job_id, Stage.PERSIST)
        trace("document.persist", job_id=job_id, document_id=document_id, chunks=len(drafts))
        written = await self._persist(
            resource=resource,
            item=item,
            parsed=parsed,
            drafts=drafts,
            embedded=bool(by_id),
        )

        outcome.chunks_written += written

        if self._embedding_enabled:
            await self._cleanup.drain(resource_id=resource.id)
            trace("document.vector_flush", job_id=job_id, document_id=document_id)
            records = [
                _record_of(resource, item, parsed, draft, by_id[draft.id], self._embedder.model_version)
                for draft in embeddable
            ]
            outcome.vectors_written += await self._flush(
                records,
                resource.id,
                item.uri,
                content_hash,
            )

        trace(
            "document.completed",
            job_id=job_id,
            document_id=document_id,
            chunks_written=written,
            elapsed_ms=round((perf_counter() - started) * 1000, 2),
        )
        return True

    async def _persist(
        self,
        *,
        resource: Resource,
        item: DiscoveredItem,
        parsed: ParsedDocument,
        drafts: Sequence[ChunkDraft],
        embedded: bool,
    ) -> int:
        """把 chunk 写进 SQLite, 并更新 document 的流水线状态。

        一篇一个事务。这样一次中断只会丢掉正在写的那一篇, 而不是
        整轮扫描的成果; 同时 UI 在扫描进行中就能查到已完成的部分。
        """

        async with self._sessions() as session:
            document = await session.get(Document, (item.uri, resource.id))

            if document is None:  # pragma: no cover - discover 已经建过
                raise PipelineError("扫描中的文档已被删除, 已停止写入索引。")

            # 先删旧 chunk 再写新的。不能只 upsert: 文档改短之后,
            # 旧的尾部 chunk 不会被任何新 id 覆盖, 会变成永远搜得到
            # 却已不存在于原文的幽灵内容。
            await session.execute(
                delete(Chunk).where(
                    Chunk.doc_uri == item.uri,
                    Chunk.resource_id == resource.id,
                )
            )

            for draft in drafts:
                session.add(
                    Chunk(
                        id=draft.id,
                        doc_uri=item.uri,
                        resource_id=resource.id,
                        granularity=draft.granularity,
                        chunk_index=draft.chunk_index,
                        parent_chunk_id=draft.parent_id,
                        text_=draft.text,
                        context_prefix=draft.context_prefix,
                        token_count=draft.token_estimate,
                        vector_state=(
                            VectorState.PENDING if draft.granularity.should_embedded else VectorState.SKIPPED
                        ),
                        model_version=self._embedder.model_version if embedded else None,
                        chunk_meta=draft.meta,
                    )
                )

            session.add(
                DeletionOutbox(
                    scope=DeletionScope.DOC,
                    resource_id=resource.id,
                    doc_uri=item.uri,
                )
            )
            document.pipe_state = DocumentPipeState.CHUNKED
            document.indexed_hash = None
            document.pipe_error_kind = None
            document.chunker_version = CHUNKER_VERSION
            document.model_version = self._embedder.model_version if embedded else None
            document.ingested_at = now_ts()
            document.rev += 1
            document.title = parsed.title or document.title
            document.lang = parsed.lang
            document.doc_class = parsed.doc_class or document.doc_class

            source_meta = dict(document.source_meta or {})
            source_meta.update(parsed.meta)
            source_meta["parser"] = parsed.parser
            source_meta["parser_version"] = parsed.parser_version
            source_meta["processing_fingerprint"] = self._processing_version
            source_meta["chunked_hash"] = item.content_hash

            source_meta["truncated"] = parsed.truncated
            document.source_meta = source_meta

            await session.commit()

        return len(drafts)

    async def _flush(
        self,
        records: list[VectorRecord],
        resource_id: str,
        doc_uri: str,
        content_hash: str,
    ) -> int:
        """Commit the document's completion only after its vectors are durable."""
        written = await self._sink.upsert(records)
        if written != len(records):
            raise VectorSinkError(f"需要写入 {len(records)} 个向量, 实际写入 {written} 个。")

        # SQLite 里的 vector_state 是"向量库里到底有没有它"的账本。
        # 写 Zvec 成功之后才改它 —— 反过来会让一次写入失败留下一批
        # 标着 indexed 却根本不存在的向量, 而那种不一致没有任何
        # 自动发现的途径。
        async with self._sessions() as session:
            await session.execute(
                update(Chunk)
                .where(Chunk.id.in_([r.id for r in records]))
                .values(vector_state=VectorState.INDEXED)
            )
            await session.execute(
                update(Document)
                .where(Document.uri == doc_uri, Document.resource_id == resource_id)
                .values(pipe_state=DocumentPipeState.INDEXED, indexed_hash=content_hash)
            )
            await session.commit()

        return written

    async def _fail_document(
        self,
        job_id: str,
        resource_id: str,
        item: DiscoveredItem,
        exc: ParseError,
        outcome: ScanOutcome,
    ) -> None:
        """单篇失败的落账。

        不支持的类型标 SKIPPED 而不是 FAILED, 且**不计入 failed_docs** ——
        用户目录里本来就有 .zip 和 .exe, 把它们算成失败会让每一次扫描
        都显示"完成但有错误", 久而久之这个信号就没人看了。
        """

        skipped = isinstance(exc, UnsupportedDocumentError)

        async with self._sessions() as session:
            document = await session.get(Document, (item.uri, resource_id))

            if document is not None:
                document.pipe_state = DocumentPipeState.SKIPPED if skipped else DocumentPipeState.FAILED
                document.pipe_error_kind = None if skipped else ErrorKind.PARSE
                document.pipe_attempts += 1
                document.skip_reason = exc.reason

                if skipped:
                    document.lifecycle_state = DocumentLifecycleState.SKIPPED

            await session.commit()

        if not skipped:
            outcome.failed_docs += 1

        await self._log(
            job_id,
            LogLevel.WARN if skipped else LogLevel.ERROR,
            item.uri,
            exc.code,
            exc.reason,
        )

    # ----------------------------------------------------------------- #
    # reconcile
    # ----------------------------------------------------------------- #
    async def _reconcile(self, job_id: str, resource_id: str) -> int:
        """本轮没见到的活跃文档 -> 立碑。

        "没见到"就是 ``last_seen_run != job_id``, 一条 UPDATE 解决 ——
        这正是 ``Job.id`` 兼任 sweep run id 的价值 (见 execution.py):
        换成在内存里做集合差, 二十万个 uri 要全部装进内存。

        **立碑而不是删除。** 墓碑是可撤销的: 外接硬盘拔掉一次不该
        让知识永久消失, 下次扫描见到它就会自动复活 (见 _upsert_document)。
        真正的物理清除由单独的 purge 流程按保留期做。

        但**向量必须当场撤下来**: SQLite 的 lifecycle_state 只是一个标记,
        Zvec 并不知道它。不删的话一篇已经从源端消失的文档照样能被检索命中,
        而"立碑"对用户的承诺恰恰是它不该再出现在结果里。

        代价是复活时要重新 embedding。所以立碑的同时把 pipe_state 打回
        PENDING —— 否则复活那一刻 ``needs_processing`` 会因为 hash 没变
        而返回 False, 于是文档回到 ACTIVE 却永远没有向量, 成了一篇
        "在库里但搜不到"的隐形文档。
        """

        async with self._sessions() as session:
            job = await session.get(Job, job_id)

            # 双保险。理论上调用方已经检查过, 但"把用户的知识库清空"
            # 这种操作值得在执行的最后一刻再确认一次。
            if job is None or not job.may_reconcile_deletions:
                return 0

            await session.execute(
                delete(ProfileDocument).where(
                    ProfileDocument.profile_id == job.scan_profile_id,
                    or_(ProfileDocument.last_seen_run.is_(None), ProfileDocument.last_seen_run != job_id),
                )
            )
            condition = (
                (Document.resource_id == resource_id)
                & (Document.lifecycle_state == DocumentLifecycleState.ACTIVE)
                & or_(Document.last_seen_run.is_(None), Document.last_seen_run != job_id)
                & ~select(ProfileDocument.doc_uri)
                .where(
                    ProfileDocument.resource_id == Document.resource_id,
                    ProfileDocument.doc_uri == Document.uri,
                )
                .exists()
            )

            # 先把 uri 取出来: UPDATE 之后这些行就不再满足条件了,
            # 而撤向量需要逐个 doc_uri 调用 sink。
            doomed = list((await session.execute(select(Document.uri).where(condition))).scalars().all())

            if not doomed:
                await session.commit()
                return 0

            await session.execute(
                update(Document)
                .where(condition)
                .values(
                    lifecycle_state=DocumentLifecycleState.TOMBSTONED,
                    tombstoned_at=now_ts(),
                    pipe_state=DocumentPipeState.PENDING,
                    skip_reason="本轮全量扫描未再发现该条目。",
                )
            )

            # 账本同步改掉: 向量马上就要从 Zvec 撤走了。
            await session.execute(
                update(Chunk)
                .where(
                    (Chunk.resource_id == resource_id)
                    & (Chunk.doc_uri.in_(doomed))
                    & (Chunk.vector_state == VectorState.INDEXED)
                )
                .values(vector_state=VectorState.PENDING)
            )

            for uri in doomed:
                session.add(
                    DeletionOutbox(
                        scope=DeletionScope.DOC,
                        resource_id=resource_id,
                        doc_uri=uri,
                    )
                )
            await session.commit()

        count = len(doomed)

        await self._log(
            job_id,
            LogLevel.INFO,
            None,
            "RECONCILE_TOMBSTONED",
            f"{count} 个条目在源端已不存在, 已标记为墓碑并持久化向量清理意图。",
        )

        return count

    # ----------------------------------------------------------------- #
    # Job 生命周期
    # ----------------------------------------------------------------- #
    async def _open_job(
        self,
        resource: Resource,
        profile: ScanProfile,
        scan_mode: ScanMode,
        *,
        job_id: str | None = None,
    ) -> str:
        async with self._sessions() as session:
            if job_id is not None:
                existing = await session.get(Job, job_id)
                if (
                    existing is None
                    or existing.resource_id != resource.id
                    or existing.scan_profile_id != profile.id
                    or existing.scan_mode != scan_mode
                    or existing.state != JobState.QUEUED
                ):
                    raise PipelineError("排队的扫描作业不存在或与执行参数不匹配。")
                existing.state = JobState.RUNNING
                existing.started_at = now_ts()
                existing.heartbeat_at = now_ts()
                existing.finished_at = None
                # A replay keeps its Job ID, but must rebuild this attempt's observed set.
                await session.execute(
                    update(Document)
                    .where(
                        Document.resource_id == resource.id,
                        Document.last_seen_run == job_id,
                    )
                    .values(last_seen_run=None)
                )
                await session.execute(
                    update(ProfileDocument)
                    .where(
                        ProfileDocument.profile_id == profile.id,
                        ProfileDocument.last_seen_run == job_id,
                    )
                    .values(last_seen_run=None)
                )
                existing.current_stage = Stage.DISCOVER
                existing.stats = {"embedding_enabled": self._embedding_enabled}
                existing.is_discovery_complete = False
                existing.error_kind = None
                existing.error_detail = None
                for counter in (
                    "discovered",
                    "skipped_unchanged",
                    "fetched",
                    "parsed",
                    "chunked",
                    "embedded",
                    "failed_docs",
                    "chunks_written",
                    "bytes_read",
                ):
                    setattr(existing, counter, 0)
                await session.commit()
                return existing.id
            job = Job(
                stats={"embedding_enabled": self._embedding_enabled},
                scan_profile_id=profile.id,
                resource_id=resource.id,
                fired_by_kind=ScheduleKind.MANUAL,
                scan_mode=scan_mode,
                state=JobState.RUNNING,
                started_at=now_ts(),
                heartbeat_at=now_ts(),
                current_stage=Stage.DISCOVER,
            )
            session.add(job)
            await session.commit()

            return job.id

    async def _save_progress(self, job_id: str, outcome: ScanOutcome) -> None:
        async with self._sessions() as session:
            await session.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(
                    discovered=outcome.discovered,
                    skipped_unchanged=outcome.skipped_unchanged,
                    fetched=outcome.fetched,
                    parsed=outcome.parsed,
                    chunked=outcome.chunked,
                    embedded=outcome.embedded,
                    failed_docs=outcome.failed_docs,
                    chunks_written=outcome.chunks_written,
                    bytes_read=outcome.bytes_read,
                )
            )
            await session.commit()

    async def _close_job(self, job_id: str, outcome: ScanOutcome) -> None:
        async with self._sessions() as session:
            job = await session.get(Job, job_id)

            if job is None:  # pragma: no cover
                return

            job.discovered = outcome.discovered
            job.skipped_unchanged = outcome.skipped_unchanged
            job.fetched = outcome.fetched
            job.parsed = outcome.parsed
            job.chunked = outcome.chunked
            job.embedded = outcome.embedded
            job.failed_docs = outcome.failed_docs
            job.chunks_written = outcome.chunks_written
            job.bytes_read = outcome.bytes_read
            job.error_kind = outcome.error_kind
            job.error_detail = outcome.error_detail
            job.finished_at = now_ts()
            job.current_stage = None
            job.stats = {
                **job.stats,
                "embedding_enabled": self._embedding_enabled,
                "vectors_written": outcome.vectors_written,
                "tombstoned": outcome.tombstoned,
                "warnings": outcome.warnings[:50],
            }

            # terminal_state() 自己会判断 partial, 别在这里重复那套逻辑
            job.state = (
                outcome.state
                if outcome.state in (JobState.FAILED, JobState.CANCELLED)
                else job.terminal_state()
            )

            outcome.state = job.state

            await session.commit()

    async def _mark_discovery_complete(self, job_id: str, discovered: int, *, delete_detect: bool) -> None:
        async with self._sessions() as session:
            job = await session.get(Job, job_id)

            if job is not None:
                job.is_discovery_complete = True
                job.discovered = discovered
                job.stats = {**job.stats, "delete_detect": delete_detect}
                await session.commit()

    async def _set_stage(self, job_id: str, stage: Stage) -> None:
        async with self._sessions() as session:
            job = await session.get(Job, job_id)

            if job is not None and job.current_stage is not stage:
                job.current_stage = stage
                await session.commit()
                trace("scan.stage", job_id=job_id, stage=stage.value)

    async def _cancel_requested(self, job_id: str) -> bool:
        if self._stopping is not None and self._stopping.is_set():
            return True
        async with self._sessions() as session:
            job = await session.get(Job, job_id)
            return bool(job and job.is_cancel_requested)

    async def _heartbeat(self, job_id: str) -> None:
        """定期刷新 heartbeat_at。

        僵尸检测的唯一依据: 进程被 kill 时没有任何机会写"我失败了",
        Job 会永远停在 running。心跳停了超过阈值, 清理流程就能把它
        判死, 从而让 ``idx_job_active`` 不会永久挡住后续扫描。
        """

        while True:
            await asyncio.sleep(_HEARTBEAT_SECONDS)

            async with self._sessions() as session:
                await session.execute(update(Job).where(Job.id == job_id).values(heartbeat_at=now_ts()))
                await session.commit()

    async def _log(
        self,
        job_id: str,
        level: LogLevel,
        doc_uri: str | None,
        code: str,
        message: str,
    ) -> None:
        async with self._sessions() as session:
            session.add(
                JobEvent(
                    job_id=job_id,
                    level=level,
                    doc_uri=doc_uri,
                    code=code,
                    message=message,
                )
            )
            await session.commit()
        trace("scan.event", job_id=job_id, code=code, level=level.value)

    async def _watermark(self, resource_id: str, profile_id: str | None = None) -> int | None:
        """增量扫描的水位线: 上次见过的最大源端修改时间。

        有待恢复文档时不应用水位线, 否则旧失败或待补向量的文件永远列举不到。

        取 ``max(modified_at_src)`` 而不是"上次扫描的结束时间"。后者会
        漏掉一类文档: 扫描过程中被修改的文件, 它的 mtime 早于扫描结束,
        于是下次扫描会认为它已经处理过。用源端时间做水位线则不会 ——
        连接器那边是严格大于的比较 (见 local_fs 的 ``scope.since``)。
        """

        stmt = select(func.max(Document.modified_at_src)).where(
            (Document.resource_id == resource_id)
            & (Document.lifecycle_state == DocumentLifecycleState.ACTIVE)
            & (Document.indexed_hash.is_not(None))
        )
        membership = (
            select(ProfileDocument.doc_uri)
            .where(
                ProfileDocument.resource_id == Document.resource_id,
                ProfileDocument.doc_uri == Document.uri,
                ProfileDocument.profile_id == profile_id,
            )
            .exists()
        )
        if profile_id is not None:
            stmt = stmt.where(membership)

        async with self._sessions() as session:
            recovery = (
                select(Document.uri)
                .where(
                    Document.resource_id == resource_id,
                    Document.lifecycle_state == DocumentLifecycleState.ACTIVE,
                    self._recovery_condition(),
                )
                .limit(1)
            )
            if profile_id is not None:
                recovery = recovery.where(membership)
            if (await session.execute(recovery)).scalar_one_or_none() is not None:
                return None
            return (await session.execute(stmt)).scalar_one_or_none()


# --------------------------------------------------------------------- #
# 模块级辅助
# --------------------------------------------------------------------- #
async def _read(connector: Connector, item: DiscoveredItem) -> bytes:
    """把 ``open()`` 的字节流读成一个 bytes。

    只有声明了 RANDOM_READ 的连接器才走到这里; 编排层依赖这个能力,
    所以 preflight 之外不再重复检查 —— 连接器自己会抛
    ConnectorCapabilityError。
    """

    if not connector.spec.supports(ConnectorCapability.RANDOM_READ):
        raise ParseError(
            item.uri,
            f"连接器 '{connector.spec.kind}' 不支持按条目读取内容。",
        )

    buffer = bytearray()

    async for block in connector.open(item):
        buffer.extend(block)

    return bytes(buffer)


def _hint_of(resource: Resource, item: DiscoveredItem) -> ParseHint:
    # 文件名优先用 title (连接器给的显示名), 回落到 external_id 的末段。
    # external_id 是 POSIX 相对路径, 所以 rsplit 就够了, 不必上 Path ——
    # 在 Windows 上 Path 会把 "a/b.md" 也按 "\" 再拆一次, 结果一样, 但
    # 对一个本来就是 POSIX 的字符串走平台相关逻辑是没必要的风险。
    filename = item.title or item.external_id.rsplit("/", 1)[-1]

    return ParseHint(
        uri=item.uri,
        filename=filename,
        doc_class=item.doc_class,
        size_bytes=item.size_bytes,
        content_format=(
            "outlook-mail-v1"
            if resource.connector_kind == "outlook_local"
            and item.extra.get("content_format") == "outlook-mail-v1"
            else None
        ),
    )


def _chunker_config(profile: ScanProfile, parsed: ParsedDocument) -> ChunkerConfig:
    """profile 上配了就用配的, 没配就按文档类型挑默认。

    顺序不能反: 用户在 profile 里写下的数字是明确意图, 而按类型挑的
    只是一个还不错的猜测。
    """

    section = (profile.pipeline or {}).get("chunker")

    if section:
        return ChunkerConfig.from_pipeline(profile.pipeline)

    return default_config_for(parsed.doc_class)


def _record_of(
    resource: Resource,
    item: DiscoveredItem,
    parsed: ParsedDocument,
    draft: ChunkDraft,
    vector: list[float],
    model_version: str,
) -> VectorRecord:
    return VectorRecord(
        id=draft.id,
        vector=vector,
        # 送进 Zvec 的 FTS 文本用 embed_text (带来源前缀): 全文检索
        # 同样受益于"这段话出自哪一章"这个信息。
        text=draft.embed_text,
        resource_id=resource.id,
        doc_uri=item.uri,
        granularity=draft.granularity.value,
        chunk_index=draft.chunk_index,
        doc_class=(parsed.doc_class or item.doc_class).value,
        modality=item.modality.value,
        lang=parsed.lang,
        model_version=model_version,
        authority=resource.authority,
    )


def _error_kind_of(exc: PipelineError | ConnectorDiscoveryError) -> ErrorKind:
    if isinstance(exc, (EmbeddingError, ConnectorDiscoveryError)):
        return ErrorKind.NETWORK

    if isinstance(exc, VectorSinkError):
        return ErrorKind.INTERNAL

    return ErrorKind.INTERNAL


__all__ = ["ScanOutcome", "ScanService"]
