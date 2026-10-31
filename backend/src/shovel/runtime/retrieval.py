from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from dataclasses import dataclass
from time import perf_counter
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, column, or_, select, table, text, tuple_

from shovel.db.model.config import Resource
from shovel.db.model.content import Chunk, Document
from shovel.db.model.retrieval import RetrievalLog
from shovel.domain.enums import (
    DocClass,
    DocumentLifecycleState,
    DocumentPipeState,
    Granularity,
    ResourceState,
    RetrievalRoute,
    VectorState,
)
from shovel.pipeline.embedder import Embedder, build_embedder
from shovel.pipeline.embedding_cache import EmbeddingCache
from shovel.runtime.diagnostics import trace, trace_duration
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.schemas import SearchInput

if TYPE_CHECKING:
    from sqlalchemy.sql import Select

    from shovel.runtime.service import Runtime

_READABLE = (ResourceState.ACTIVE, ResourceState.DEGRADED, ResourceState.PAUSED, ResourceState.VERIFYING)
_FTS = table("chunk_search", column("chunk_id"), column("rank"))


@dataclass
class _EmbeddingRead:
    task: asyncio.Task[list[float]]
    readers: int = 0


class RetrievalService:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self._embedder: Embedder | None = None
        settings = runtime.settings.performance
        self._embeddings = EmbeddingCache(settings.embedding_cache_entries, settings.embedding_cache_seconds)
        self._embedding_reads: dict[str, _EmbeddingRead] = {}
        self._embedding_slots = asyncio.Semaphore(64)

    async def embed_query(self, query: str) -> tuple[list[float], str]:
        if self._embedder is None:
            self._embedder = build_embedder(
                self.runtime.settings.embedding, expected_dimension=self.runtime.settings.zvec.dim
            )
        embedder = self._embedder
        version = embedder.model_version
        key = self._embeddings.key(
            [version, self.runtime.settings.embedding.base_url, embedder.dimension, query], {}
        )
        vector = self._embeddings.get(key)
        if vector is not None:
            trace("retrieval.embedding.cached")
            return vector, version

        async def embed() -> list[float]:
            trace("retrieval.embedding.started", provider=self.runtime.settings.embedding.provider)
            with trace_duration("retrieval.embedding.completed"):
                result = (await embedder.embed([query]))[0]
                self._embeddings.put(key, result)
                return result

        read = self._embedding_reads.get(key)
        if read is None:
            await self._embedding_slots.acquire()
            vector = self._embeddings.get(key)
            if vector is not None:
                self._embedding_slots.release()
                return vector, version
            read = self._embedding_reads.get(key)
            if read is None:
                read = _EmbeddingRead(asyncio.create_task(embed()))
                self._embedding_reads[key] = read
            else:
                self._embedding_slots.release()
        else:
            trace("retrieval.embedding.shared")
        read.readers += 1
        try:
            with trace_duration("retrieval.embedding.wait.completed"):
                return list(await asyncio.shield(read.task)), version
        finally:
            read.readers -= 1
            if not read.readers:
                self._embedding_reads.pop(key)
                if not read.task.done():
                    read.task.cancel()
                try:
                    await asyncio.gather(read.task, return_exceptions=True)
                finally:
                    self._embedding_slots.release()

    async def close(self) -> None:
        tasks = [read.task for read in self._embedding_reads.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._embedder is not None:
            await self._embedder.aclose()
            self._embedder = None

    async def _scope(self, requested: list[str]) -> list[str]:
        async with self.runtime.sessions() as session:
            resources = list(await session.scalars(select(Resource).where(Resource.state.in_(_READABLE))))
        allowed = {resource.id for resource in resources}
        if requested and not set(requested).issubset(allowed):
            raise RuntimeFailure("所选资源不存在、尚未验证或已吊销,请刷新资源列表。", status=409)
        return sorted(set(requested) if requested else allowed)

    @staticmethod
    def _chunks(scope: list[str]) -> Select[Chunk]:
        return (
            select(Chunk)
            .join(Document, and_(Chunk.doc_uri == Document.uri, Chunk.resource_id == Document.resource_id))
            .join(Resource, Resource.id == Chunk.resource_id)
            .where(
                Chunk.resource_id.in_(scope),
                Chunk.granularity == Granularity.CHUNK,
                Document.lifecycle_state == DocumentLifecycleState.ACTIVE,
                Resource.state.in_(_READABLE),
                Document.pipe_state.in_((DocumentPipeState.CHUNKED, DocumentPipeState.INDEXED)),
            )
        )

    async def _keyword(self, request: SearchInput, scope: list[str], limit: int) -> list[str]:
        terms = list(dict.fromkeys(re.findall(r"\w+", request.query.casefold())))[:32]
        if not terms:
            raise RuntimeFailure("搜索内容至少包含一个文字或数字。")
        long = [term for term in terms if len(term) >= 3]
        short = [term for term in terms if len(term) < 3]
        identifiers: list[str] = []
        async with self.runtime.sessions() as session:
            if long:
                expression = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in long)
                statement = (
                    self._chunks(scope)
                    .join(_FTS, _FTS.c.chunk_id == Chunk.id)
                    .where(text("chunk_search MATCH :query"))
                    .order_by(_FTS.c.rank, Chunk.id)
                    .limit(limit)
                )
                rows = await session.scalars(statement, {"query": expression})
                identifiers.extend(chunk.id for chunk in rows)
            if short:
                statement = (
                    self._chunks(scope)
                    .where(or_(*(Chunk.text_.icontains(term, autoescape=True) for term in short)))
                    .order_by(Chunk.id)
                    .limit(limit)
                )
                rows = await session.scalars(statement)
                identifiers.extend(chunk.id for chunk in rows if chunk.id not in identifiers)
        return identifiers

    async def search(self, request: SearchInput) -> dict[str, Any]:
        started = perf_counter()
        with trace_duration("retrieval.scope.completed"):
            scope = await self._scope(request.resource_ids)
        mode = request.mode
        if mode == "auto":
            mode = "hybrid" if self.runtime.settings.embedding.is_configured else "keyword"
        if mode in {"vector", "hybrid"} and not self.runtime.settings.embedding.is_configured:
            raise RuntimeFailure("向量检索需要配置 [embedding];可改用 keyword。", status=503)
        trace("retrieval.started", mode=mode, resource_count=len(scope), limit=request.limit)
        candidate_count = min(300, request.limit * 10)
        rankings: list[list[str]] = []
        if scope and mode in {"keyword", "hybrid"}:
            with trace_duration("retrieval.keyword.query.completed"):
                rankings.append(await self._keyword(request, scope, candidate_count))
            trace("retrieval.keyword.completed", candidates=len(rankings[-1]))
        model_version: str | None = None
        if scope and mode in {"vector", "hybrid"}:
            vector, model_version = await self.embed_query(request.query)
            with trace_duration("retrieval.index.ready"):
                sink = await self.runtime.sink()
            with trace_duration("retrieval.vector.search.completed"):
                matches = await sink.search(
                    vector, resource_ids=scope, model_version=model_version, limit=candidate_count
                )
            identifiers = [identifier for identifier, _score in matches]
            # Vectors are an acceleration structure, never an authority or permission source.
            async with self.runtime.sessions() as session:
                with trace_duration("retrieval.vector.revalidated"):
                    valid = set(
                        await session.scalars(
                            self._chunks(scope)
                            .with_only_columns(Chunk.id)
                            .where(
                                Chunk.id.in_(identifiers),
                                Chunk.vector_state == VectorState.INDEXED,
                                Chunk.model_version == model_version,
                                Document.pipe_state == DocumentPipeState.INDEXED,
                                Document.indexed_hash == Document.content_hash,
                            )
                        )
                    )
            rankings.append([identifier for identifier in identifiers if identifier in valid])
            trace("retrieval.vector.completed", candidates=len(rankings[-1]))
        with trace_duration("retrieval.ranks.completed"):
            scores: defaultdict[str, float] = defaultdict(float)
            for ranking in rankings:
                for rank, identifier in enumerate(ranking, 1):
                    scores[identifier] += 1 / (60 + rank)
            ordered = sorted(scores, key=lambda identifier: (-scores[identifier], identifier))
        hits: list[dict[str, Any]] = []
        seen: set[str] = set()
        if ordered:
            hydration_started = perf_counter()
            async with self.runtime.sessions() as session:
                # Re-check scope and lifecycle after embedding/network I/O.
                rows = await session.scalars(self._chunks(scope).where(Chunk.id.in_(ordered)))
                chunks = {chunk.id: chunk for chunk in rows}
                parent_ids = {chunk.parent_chunk_id for chunk in chunks.values() if chunk.parent_chunk_id}
                parents = (
                    {
                        chunk.id: chunk
                        for chunk in await session.scalars(select(Chunk).where(Chunk.id.in_(parent_ids)))
                    }
                    if parent_ids
                    else {}
                )
                document_keys = {(chunk.doc_uri, chunk.resource_id) for chunk in chunks.values()}
                documents = (
                    {
                        (document.uri, document.resource_id): document
                        for document in await session.scalars(
                            select(Document).where(
                                tuple_(Document.uri, Document.resource_id).in_(document_keys)
                            )
                        )
                    }
                    if document_keys
                    else {}
                )
                resources = (
                    {
                        resource.id: resource
                        for resource in await session.scalars(
                            select(Resource).where(
                                Resource.id.in_({chunk.resource_id for chunk in chunks.values()})
                            )
                        )
                    }
                    if chunks
                    else {}
                )
                for identifier in ordered:
                    chunk = chunks.get(identifier)
                    if chunk is None:
                        continue
                    key = chunk.parent_chunk_id or chunk.id
                    if key in seen:
                        continue
                    seen.add(key)
                    parent = parents.get(key) if chunk.parent_chunk_id else None
                    if parent is not None and (
                        parent.resource_id != chunk.resource_id
                        or parent.doc_uri != chunk.doc_uri
                        or parent.rev != chunk.rev
                    ):
                        parent = None
                    document = documents.get((chunk.doc_uri, chunk.resource_id))
                    resource = resources.get(chunk.resource_id)
                    if document is None or resource is None:
                        continue
                    hits.append(
                        {
                            "chunk_id": chunk.id,
                            "resource_id": chunk.resource_id,
                            "resource_name": resource.name,
                            "doc_uri": chunk.doc_uri,
                            "title": document.title or chunk.doc_uri.rsplit("/", 1)[-1],
                            "text": (parent.text_ if parent else chunk.text_)[:4000],
                            "score": scores[identifier],
                            "locator": chunk.chunk_meta,
                            **(
                                {
                                    "email": {
                                        name: document.source_meta.get(name)
                                        for name in ("sender", "to", "sent_at", "received_at", "folder_path")
                                    }
                                }
                                if document.doc_class is DocClass.EMAIL
                                else {}
                            ),
                        }
                    )
                    if len(hits) >= request.limit:
                        break
            trace(
                "retrieval.hydration.completed",
                elapsed_ms=round((perf_counter() - hydration_started) * 1000, 2),
                candidates=len(chunks),
                hits=len(hits),
            )
        async with self.runtime.sessions() as session:
            log = RetrievalLog(
                query_text=request.query,
                agent_id="shovel",
                route=RetrievalRoute.HYBRID
                if mode == "hybrid"
                else (RetrievalRoute.SEMANTIC if mode == "vector" else RetrievalRoute.STRUCTURED),
                filters={"resource_ids": scope, "mode": mode, "model_version": model_version},
                returned=[{"chunk_id": hit["chunk_id"], "score": hit["score"]} for hit in hits],
                top_score=hits[0]["score"] if hits else None,
            )
            session.add(log)
            with trace_duration("retrieval.log.persisted"):
                await session.commit()
        trace(
            "retrieval.completed",
            retrieval_id=log.id,
            mode=mode,
            hits=len(hits),
            elapsed_ms=round((perf_counter() - started) * 1000, 2),
        )
        return {"mode": mode, "items": hits, "retrieval_id": log.id}
