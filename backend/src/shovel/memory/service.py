"""Framework-independent extraction, consolidation and bounded retrieval."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Sequence
from dataclasses import replace

import structlog

from shovel.memory.domain import (
    PROFILE_TOPICS,
    MemoryRecord,
    MemoryScope,
    MemoryState,
    MemoryType,
    Provenance,
    admissible,
    eligible_source,
    explicit_correction,
    normalize,
)
from shovel.memory.ports import MemoryExtractor, MemoryRepository, MemoryVectorIndex
from shovel.runtime.errors import NotFound


def rank(record: MemoryRecord, relevance: float, now: int) -> float:
    age = max(0, now - record.recorded_at) / 86400
    return (
        0.7 * relevance
        + 0.1 * record.importance
        + 0.1 * record.confidence
        + 0.05 / (1 + age / 30)
        + 0.05 * min(1, math.log1p(record.access_count) / 5)
    )


def view(record: MemoryRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "memory": record.text,
        "kind": record.kind.value,
        "state": record.state.value,
        "scope": record.scope.kind.value,
        "created_at": record.recorded_at,
        "revision": record.revision,
        "superseded_by": record.superseded_by,
        "expires_at": record.expires_at,
        "provenance": record.provenance,
        "occurred_at": record.occurred_at,
    }


class MemoryService:
    def __init__(
        self,
        repository: MemoryRepository,
        index: MemoryVectorIndex | None,
        extractor: MemoryExtractor,
        scope: MemoryScope,
        *,
        min_similarity: float = 0.55,
        min_confidence: float = 0.8,
        max_chars: int = 2000,
        max_items: int = 10,
    ) -> None:
        self.repository, self.index, self.extractor, self.scope = repository, index, extractor, scope
        self.min_similarity, self.min_confidence = min_similarity, min_confidence
        self.max_chars, self.max_items = max_chars, max_items

    async def extract(
        self,
        source: str,
        provenance: Provenance,
        *,
        operation_id: str | None = None,
        generation: int | None = None,
        related: Sequence[MemoryRecord] = (),
    ) -> list[MemoryRecord]:
        candidates = (
            (
                await self.extractor.extract(source, related)
                if related
                else await self.extractor.extract(source)
            )
            if eligible_source(source)
            else []
        )
        accepted = []
        ignored_corrections = 0
        for candidate in candidates:
            if candidate.replaces and not explicit_correction(candidate.evidence):
                ignored_corrections += 1
                candidate = replace(candidate, replaces=False)
            if not admissible(candidate, source, self.min_confidence):
                continue
            if candidate.subject == "user" and candidate.predicate in PROFILE_TOPICS:
                candidate = replace(
                    candidate, kind=MemoryType.SEMANTIC, topic=PROFILE_TOPICS[candidate.predicate]
                )
            accepted.append(candidate)
        if ignored_corrections:
            structlog.get_logger().warning("memory.extraction.correction_ignored", count=ignored_corrections)
        references = {r.id: r for r in related}
        ignored_profile_merges = 0
        for index, candidate in enumerate(accepted):
            if candidate.merge_with:
                target = references.get(candidate.merge_with)
                if (
                    target is None
                    or candidate.replaces
                    or candidate.kind != MemoryType.SEMANTIC
                    or target.kind != candidate.kind
                    or target.predicate != candidate.predicate
                    or normalize(target.subject) != normalize(candidate.subject)
                ):
                    raise ValueError("Extractor proposed an unsupported semantic merge.")
                if candidate.subject == "user" and candidate.predicate in PROFILE_TOPICS:
                    accepted[index] = replace(candidate, merge_with=None)
                    ignored_profile_merges += 1
        if ignored_profile_merges:
            structlog.get_logger().warning(
                "memory.extraction.profile_merge_ignored", count=ignored_profile_merges
            )
        if any(
            c.predicate
            not in {
                "prefers",
                "dislikes",
                "uses",
                "works_at",
                "has_name",
                "has_role",
                "has_timezone",
                "has_language",
                "has_goal",
                "has_constraint",
                "decides",
                "knows_about",
            }
            for c in accepted
        ):
            raise ValueError("Extractor returned an unsupported predicate.")
        return await self.repository.consolidate(
            self.scope,
            accepted,
            provenance,
            operation_id=operation_id,
            generation=generation,
        )

    async def related(self, source: str) -> list[MemoryRecord]:
        if self.index is None or not eligible_source(source):
            return []
        hits = await self.index.search(source, self.scope, 4)
        records = []
        for hit in hits:
            if hit.similarity < 0.85:
                continue
            try:
                record = await self.repository.get(self.scope, hit.id)
            except NotFound:
                continue
            if (
                record.state == MemoryState.ACTIVE
                and record.kind == hit.kind
                and record.revision == hit.revision
                and record.model_version == self.index.model_version
                and record.scope.kind == self.scope.kind
                and (record.expires_at is None or record.expires_at > time.time())
            ):
                records.append(record)
        return records

    async def retrieve(
        self, query: str, conversation_id: str = "", *, use_index: bool = True
    ) -> list[dict[str, object]]:
        scope = replace(self.scope, session_id=conversation_id)
        now = int(time.time())
        profile = await self.repository.profile(scope, limit=self.max_items)
        found = {record.id: (record, rank(record, 1, now)) for record in profile}
        profile_ids = set(found)
        hits = (
            await self.index.search(query, scope, self.max_items * 4)
            if self.index is not None and use_index
            else []
        )
        for hit in hits:
            if hit.similarity < self.min_similarity:
                continue
            try:
                record = await self.repository.get(scope, hit.id)
            except NotFound:
                continue
            if (
                (record.subject == "user" and record.predicate in PROFILE_TOPICS)
                or self.index is None
                or record.kind != hit.kind
                or record.revision != hit.revision
                or record.model_version != self.index.model_version
                or record.state != MemoryState.ACTIVE
                or (record.expires_at is not None and record.expires_at <= now)
            ):
                continue
            score = rank(record, hit.similarity, now)
            if hit.id not in found or score > found[hit.id][1]:
                found[hit.id] = (record, score)
        selected: list[dict[str, object]] = []
        identifiers = []
        for record, _score in sorted(
            found.values(), key=lambda item: (item[0].id not in profile_ids, -item[1], item[0].id)
        ):
            entry = view(record)
            if record.kind == MemoryType.SEMANTIC:
                entry.update(subject=record.subject, predicate=record.predicate, object=record.object)
            entry["provenance"] = [
                {
                    key: value
                    for key, value in source.items()
                    if key in ("source_kind", "conversation_id", "message_id")
                }
                for source in record.provenance[-1:]
            ]
            if len(json.dumps([*selected, entry], ensure_ascii=False)) > self.max_chars:
                continue
            selected.append(entry)
            identifiers.append(record.id)
            if len(selected) == self.max_items:
                break
        if identifiers:
            await self.repository.touch(scope, identifiers)
        return selected
