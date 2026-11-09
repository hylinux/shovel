"""Framework-independent extraction, consolidation and bounded retrieval."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Mapping, Sequence
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
from shovel.memory.relevance import Query, Support, assess
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
        "episode_time_source": record.episode_time_source,
        "episode_time_precision": record.episode_time_precision,
        "source": record.source,
        "confidence": record.confidence,
        "importance": record.importance,
        "subject": record.subject,
        "predicate": record.predicate,
        "object": record.object,
        "topic": record.topic,
        "session_id": record.scope.session_id,
    }


def context_entry(entry: Mapping[str, object]) -> dict[str, object]:
    """Keep model context compact; id/revision resolve the full stored evidence."""
    result = {
        key: entry[key]
        for key in (
            "id",
            "memory",
            "kind",
            "scope",
            "source",
            "confidence",
            "revision",
            "occurred_at",
            "episode_time_source",
            "episode_time_precision",
            "expires_at",
            "answer_support",
        )
        if entry.get(key) is not None
    }
    if entry.get("kind") == MemoryType.SEMANTIC:
        result.update({key: entry[key] for key in ("subject", "predicate", "object") if key in entry})
    return result


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
        review_required: bool = False,
    ) -> None:
        self.repository, self.index, self.extractor, self.scope = repository, index, extractor, scope
        self.min_similarity, self.min_confidence = min_similarity, min_confidence
        self.max_chars, self.max_items = max_chars, max_items
        self.review_required = review_required

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
                if (candidate.subject == "user" and candidate.predicate in PROFILE_TOPICS) or (
                    normalize(target.object) != normalize(candidate.object)
                    or normalize(target.topic) != normalize(candidate.topic)
                ):
                    accepted[index] = replace(candidate, merge_with=None)
                    ignored_profile_merges += 1
        if ignored_profile_merges:
            structlog.get_logger().warning(
                "memory.extraction.non_equivalent_merge_ignored", count=ignored_profile_merges
            )
        if any(
            c.predicate
            not in {
                "prefers",
                "dislikes",
                "uses",
                "works_at",
                "has_name",
                "has_gender",
                "has_bio",
                "has_avatar",
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
            review_required=self.review_required,
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
        self,
        query: str,
        conversation_id: str = "",
        *,
        use_index: bool = True,
        record_access: bool = True,
        diagnostics: dict[str, int] | None = None,
    ) -> list[dict[str, object]]:
        scope = replace(self.scope, session_id=conversation_id)
        now = int(time.time())
        profile = await self.repository.profile(scope, limit=len(PROFILE_TOPICS))
        found = {record.id: (record, rank(record, 0, now)) for record in profile}
        profile_ids = set(found)
        channels = {record.id: ["profile"] for record in profile}
        relevance: dict[str, float] = {}
        for position, record in enumerate(
            await self.repository.keyword(scope, query, limit=self.max_items * 4), 1
        ):
            if (
                record.subject == "user"
                and record.predicate in PROFILE_TOPICS
                and record.id not in profile_ids
            ):
                continue
            found[record.id] = (record, 0)
            relevance[record.id] = 1 / (60 + position)
            channels.setdefault(record.id, []).append("keyword")
        hits = (
            await self.index.search(query, scope, self.max_items * 4)
            if self.index is not None and use_index
            else []
        )
        below_threshold = invalid_hits = 0
        for position, hit in enumerate(hits, 1):
            if hit.similarity < self.min_similarity:
                below_threshold += 1
                continue
            try:
                record = await self.repository.get(scope, hit.id)
            except NotFound:
                invalid_hits += 1
                continue
            if (
                (
                    record.subject == "user"
                    and record.predicate in PROFILE_TOPICS
                    and record.id not in profile_ids
                )
                or self.index is None
                or record.kind != hit.kind
                or record.revision != hit.revision
                or record.model_version != self.index.model_version
                or record.state != MemoryState.ACTIVE
                or (record.expires_at is not None and record.expires_at <= now)
            ):
                invalid_hits += 1
                continue
            found[hit.id] = (record, 0)
            relevance[hit.id] = relevance.get(hit.id, 0) + 1 / (60 + position)
            channels.setdefault(hit.id, []).append("vector")
        for identifier, score in relevance.items():
            record = found[identifier][0]
            found[identifier] = (record, rank(record, min(1, score * 61 / 2), now))
        intent = Query.parse(query)
        verdicts = {
            identifier: assess(intent, record, semantic_match="vector" in channels[identifier])
            for identifier, (record, _) in found.items()
        }
        rejected = {verdict: sum(v == verdict for v in verdicts.values()) for verdict in Support}
        for identifier, verdict in verdicts.items():
            if verdict == Support.BACKGROUND:
                relevance.pop(identifier, None)
            elif verdict == Support.SUPPORTED and identifier in profile_ids:
                relevance.setdefault(identifier, 1 / 61)
        selected: list[dict[str, object]] = []
        identifiers = []
        budget_skipped = 0
        for record, _score in sorted(
            found.values(),
            key=lambda item: (
                item[0].id not in relevance,
                -relevance.get(item[0].id, 0),
                -item[1],
                item[0].id,
            ),
        ):
            if verdicts[record.id] not in (Support.SUPPORTED, Support.BACKGROUND):
                continue
            entry = context_entry(view(record))
            entry["answer_support"] = verdicts[record.id].value
            entry["recall_sources"] = channels[record.id]
            if len(json.dumps([*selected, entry], ensure_ascii=False)) > self.max_chars:
                budget_skipped += 1
                continue
            selected.append(entry)
            identifiers.append(record.id)
            if len(selected) == self.max_items:
                break
        if diagnostics is not None:
            diagnostics.update(
                profile_candidates=len(profile),
                query_candidates=len(relevance),
                vector_hits=len(hits),
                below_threshold=below_threshold,
                invalid_vector_hits=invalid_hits,
                budget_skipped=budget_skipped,
                returned_items=len(selected),
                serialized_chars=len(json.dumps(selected, ensure_ascii=False)),
                support_related=rejected[Support.RELATED],
                support_conflict=rejected[Support.CONFLICT],
                support_unknown=rejected[Support.UNKNOWN],
            )
        if identifiers and record_access:
            await self.repository.touch(scope, identifiers)
        return selected
