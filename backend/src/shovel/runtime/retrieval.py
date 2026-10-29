from __future__ import annotations

import re
from collections import defaultdict
from time import perf_counter
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, column, or_, select, table, text

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
from shovel.runtime.diagnostics import trace
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.schemas import SearchInput

if TYPE_CHECKING:
    from sqlalchemy.sql import Select

    from shovel.runtime.service import Runtime

_READABLE = (ResourceState.ACTIVE, ResourceState.DEGRADED, ResourceState.PAUSED, ResourceState.VERIFYING)
_FTS = table("chunk_search", column("chunk_id"), column("rank"))


class RetrievalService:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self._embedder: Embedder | None = None
        settings = runtime.settings.performance
        self._embeddings = EmbeddingCache(settings.embedding_cache_entries, settings.embedding_cache_seconds)

    async def embed_query(self, query: str) -> tuple[list[float], str]:
        if self._embedder is None:
            self._embedder = build_embedder(
                self.runtime.settings.embedding, expected_dimension=self.runtime.settings.zvec.dim
            )
        embedder = self._embedder
        version = embedder.model_version
        key = self._embeddings.key([version, query], {})
        vector = self._embeddings.get(key)
        if vector is not None:
            trace("retrieval.embedding.cached")
            return vector, version
        started = perf_counter()
        trace("retrieval.embedding.started", provider=self.runtime.settings.embedding.provider)
        vector = (await embedder.embed([query]))[0]
        self._embeddings.put(key, vector)
        trace("retrieval.embedding.completed", elapsed_ms=round((perf_counter() - started) * 1000, 2))
        return vector, version

    async def close(self) -> None:
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
            rankings.append(await self._keyword(request, scope, candidate_count))
            trace("retrieval.keyword.completed", candidates=len(rankings[-1]))
        model_version: str | None = None
        if scope and mode in {"vector", "hybrid"}:
            vector, model_version = await self.embed_query(request.query)
            matches = await (await self.runtime.sink()).search(
                vector,
                resource_ids=scope,
                model_version=model_version,
                limit=candidate_count,
            )
            identifiers = [identifier for identifier, _score in matches]
            # Vectors are an acceleration structure, never an authority or permission source.
            async with self.runtime.sessions() as session:
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
        scores: defaultdict[str, float] = defaultdict(float)
        for ranking in rankings:
            for rank, identifier in enumerate(ranking, 1):
                scores[identifier] += 1 / (60 + rank)
        ordered = sorted(scores, key=lambda identifier: (-scores[identifier], identifier))
        hits: list[dict[str, Any]] = []
        seen: set[str] = set()
        if ordered:
            async with self.runtime.sessions() as session:
                # Re-check scope and lifecycle after embedding/network I/O.
                rows = await session.scalars(self._chunks(scope).where(Chunk.id.in_(ordered)))
                chunks = {chunk.id: chunk for chunk in rows}
                for identifier in ordered:
                    chunk = chunks.get(identifier)
                    if chunk is None:
                        continue
                    key = chunk.parent_chunk_id or chunk.id
                    if key in seen:
                        continue
                    seen.add(key)
                    parent = await session.get(Chunk, key) if chunk.parent_chunk_id else None
                    if parent is not None and (
                        parent.resource_id != chunk.resource_id
                        or parent.doc_uri != chunk.doc_uri
                        or parent.rev != chunk.rev
                    ):
                        parent = None
                    document = await session.get(Document, (chunk.doc_uri, chunk.resource_id))
                    resource = await session.get(Resource, chunk.resource_id)
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
            await session.commit()
        trace(
            "retrieval.completed",
            retrieval_id=log.id,
            mode=mode,
            hits=len(hits),
            elapsed_ms=round((perf_counter() - started) * 1000, 2),
        )
        return {"mode": mode, "items": hits, "retrieval_id": log.id}
