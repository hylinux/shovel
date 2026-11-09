"""Bounded lexical candidates and conservative, query-specific Memory support checks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from shovel.db.search_terms import keyword_terms
from shovel.memory.domain import PROFILE_TOPICS, MemoryRecord, MemoryType, normalize

_FUNCTION_WORDS = frozenset(
    {
        "a",
        "an",
        "am",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "or",
        "our",
        "should",
        "that",
        "the",
        "their",
        "them",
        "these",
        "they",
        "this",
        "those",
        "to",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "user",
        "users",
        "use",
        "uses",
        "using",
        "prefer",
        "prefers",
        "like",
        "likes",
        "remember",
        "recall",
        "tell",
        "know",
        "about",
        "favorite",
        "favourite",
    }
)
_CHINESE_FUNCTIONS = re.compile(r"什么|哪个|哪些|哪种|我的|我在|我用|我|使用|喜欢|请问|告诉|记得|喝")
_NAME = r"[a-z0-9][a-z0-9_.-]{0,63}"
_ENTITY = re.compile(
    rf"\b(?P<kind>project|repository|repo|team|customer|client)\s+"
    rf"(?:(?:named|called|is)\s+)?[\"']?(?P<name>{_NAME})"
    rf"|(?P<before>{_NAME})\s*(?P<chinese>项目|仓库|团队|客户)"
    rf"|[“「\"](?P<quoted>[^”」\"]{{1,32}})[”」\"]\s*(?P<quoted_kind>项目|仓库|团队|客户)",
    re.IGNORECASE,
)
_KINDS = {
    "repo": "repository",
    "client": "customer",
    "项目": "project",
    "仓库": "repository",
    "团队": "team",
    "客户": "customer",
}
_ALIAS = re.compile(rf"(?:also known as|aka|alias|别名(?:为|是)?)\s+[\"']?({_NAME})", re.IGNORECASE)
_NAMED_EVALUATION = re.compile(
    r"\b([A-Z][A-Za-z0-9_.-]{0,63})\s+(?:offline\s+)?(?:evaluation|benchmark|assessment)\b"
)
_MIXED_EVALUATION = re.compile(r"(?<![a-zA-Z0-9_.-])([a-zA-Z][a-zA-Z0-9_.-]{0,63})\s*(?:离线)?(?:评估|测评)")

# Explicit category words/types, not inferred object identities or project-name allowlists.
_DOMAINS: dict[str, tuple[str, tuple[str, ...]]] = {
    "database": (
        r"\bdatabases?\b|数据库",
        ("database", "数据库", "postgresql", "postgres", "mysql", "sqlite", "sqlserver"),
    ),
    "drink": (
        r"\b(?:drinks?|beverages?|tea|coffee)\b|饮品|喝|茶|咖啡",
        ("drink", "beverage", "tea", "coffee", "饮品", "茶", "咖啡"),
    ),
    "notes": (r"\b(?:notes?|note-taking|notebook)\b|笔记", ("notes", "note", "notebook", "笔记")),
    "airline": (
        r"\b(?:airlines?|carriers?)\b|航空公司|航司",
        ("airline", "carrier", "flights", "航空公司", "航司", "航班"),
    ),
    "evaluation": (
        r"\b(?:evaluations?|benchmarks?|assessments?)\b|评估|测评",
        ("evaluation", "benchmark", "assessment", "评估", "测评"),
    ),
    "programming": (
        r"\bprogramming\b|编程语言",
        ("programming", "编程", "python", "rust", "java", "go", "typescript", "javascript"),
    ),
}
_PROFILES = {
    "has_name": r"\bname\b|名字|姓名",
    "has_role": r"\b(?:occupation|profession|job|background)\b|职业|工作",
    "has_timezone": r"\btimezone\b|时区",
    "has_language": r"\blanguage\b|语言",
}


class Support(StrEnum):
    SUPPORTED = "supported"
    BACKGROUND = "background"
    RELATED = "related"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


def literal_pattern(term: str) -> str | None:
    if re.fullmatch(r"[a-z0-9_]+", term, re.IGNORECASE):
        return rf"(?i)(?<![a-z0-9_]){re.escape(term)}(?![a-z0-9_])"
    return None


def literal_match(term: str, text: str) -> bool:
    if pattern := literal_pattern(term):
        return re.search(pattern, text) is not None
    return normalize(term) in normalize(text)


def entities(text: str, topic: str = "") -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for match in _ENTITY.finditer(text):
        kind = match["kind"] or match["chinese"] or match["quoted_kind"]
        value = normalize(match["name"] or match["before"] or match["quoted"]).rstrip(".")
        if value not in _FUNCTION_WORDS:
            result.setdefault(_KINDS.get(kind.casefold(), kind.casefold()), set()).add(value)
    for kind, value in re.findall(
        r"(?:^|[.:/])(project|repository|team|customer)[.:/]([a-z0-9_-]+)", topic, re.I
    ):
        result.setdefault(kind.casefold(), set()).add(value.casefold())
    for pattern in (_NAMED_EVALUATION, _MIXED_EVALUATION):
        for value in pattern.findall(text):
            if value.casefold() not in _FUNCTION_WORDS:
                result.setdefault("project", set()).add(value.casefold())
    if len(result) == 1 and len(next(iter(result.values()))) == 1:
        next(iter(result.values())).update(normalize(alias) for alias in _ALIAS.findall(text))
    return result


def query_terms(query: str) -> list[str]:
    terms = [
        term for term in keyword_terms(_CHINESE_FUNCTIONS.sub(" ", query)) if term not in _FUNCTION_WORDS
    ]
    for pattern, aliases in _DOMAINS.values():
        if re.search(pattern, query, re.IGNORECASE):
            terms.extend(aliases)
    return list(dict.fromkeys(terms))[:64]


@dataclass(frozen=True)
class Query:
    entities: dict[str, set[str]]
    domains: frozenset[str]
    profiles: frozenset[str]
    predicate: str | None
    event_action: str | None
    asks_time: bool
    question: bool
    known_request: bool
    unresolved_entity: bool
    audience: str | None
    personal_actor: bool
    terms: tuple[str, ...]

    @classmethod
    def parse(cls, text: str) -> Query:
        conditions = entities(text)
        entity_hint = bool(
            re.search(r"\b(?:project|repository|repo|team|customer|client)\b|项目|仓库|团队|客户", text, re.I)
        )
        domains = frozenset(name for name, (pattern, _) in _DOMAINS.items() if re.search(pattern, text, re.I))
        if conditions and re.search(r"\blanguages?\b|语言", text, re.I):
            domains |= {"programming"}
        profiles = frozenset(
            name
            for name, pattern in _PROFILES.items()
            if not entity_hint and "programming" not in domains and re.search(pattern, text, re.I)
        )
        predicate = (
            "dislikes"
            if re.search(r"\bdislikes?\b|不喜欢|讨厌", text, re.I)
            else "prefers"
            if re.search(r"\b(?:prefers?|likes?|favou?rite)\b|喜欢|偏好", text, re.I)
            else "uses"
            if re.search(r"\buses?\b|使用|用什么|用的", text, re.I)
            else None
        )
        action = (
            "start"
            if re.search(r"\b(?:begin|began|start(?:ed)?)\b|开始|启动", text, re.I)
            else "finish"
            if re.search(r"\b(?:finish(?:ed)?|complet(?:e|ed))\b|完成|结束", text, re.I)
            else None
        )
        asks_time = bool(re.search(r"\bwhen\b|何时|什么时候|日期", text, re.I))
        actor = re.search(r"\b(?:do|does|did)\s+(.+?)\s+(?:use|prefer|like|dislike)\b", text, re.I)
        unknown_actor = bool(
            actor and normalize(actor[1]) not in {"i", "the user"} and not entities(actor[1])
        )
        known_request = bool(
            (asks_time and action and "evaluation" in domains)
            or re.search(
                r"\b(?:what|which)\s+(?:(?:is|are|my|the|a|an|preferred|favorite|favourite|programming)\s+)*"
                r"(?:database|drink|beverage|tea|coffee|application|tool|notebook|notes|airline|carrier|language|name|occupation|profession|job|timezone)\b"
                r"|(?:什么|哪种|哪个|哪家)(?:数据库|饮品|茶|咖啡|笔记|应用|工具|航空公司|航司|编程语言|语言|名字|姓名|职业|时区)"
                r"|(?:数据库|饮品|笔记|航空公司|编程语言)(?:是|用的是什么)",
                text,
                re.I,
            )
        )
        return cls(
            conditions,
            domains,
            profiles,
            predicate,
            action,
            asks_time,
            bool(re.search(r"[?\uff1f]|\b(?:what|which|when|who|why|how)\b|什么|何时|哪种|哪个", text, re.I)),
            known_request,
            (entity_hint and not conditions) or unknown_actor,
            "personal"
            if re.search(r"\b(?:personal|private)\b|个人|私人", text, re.I)
            else "team"
            if re.search(r"\b(?:team|shared)\b|团队|共享", text, re.I)
            else None,
            bool(re.search(r"\bi\s+(?:use|prefer|like|dislike)|我(?:使用|喜欢|用)", text, re.I)),
            tuple(query_terms(text)),
        )


def assess(query: Query, record: MemoryRecord, *, semantic_match: bool) -> Support:
    if record.subject == "user" and record.predicate in PROFILE_TOPICS:
        return (
            Support.SUPPORTED
            if record.predicate in query.profiles
            and not query.asks_time
            and (not query.question or query.known_request)
            else Support.BACKGROUND
        )
    if query.profiles:
        return Support.RELATED
    if query.unresolved_entity:
        return Support.UNKNOWN
    if query.personal_actor and record.kind == MemoryType.SEMANTIC and record.subject != "user":
        return Support.RELATED
    body = " ".join((record.text, record.object, record.topic))
    actual_entities = entities(record.text, record.topic)
    text_entities = entities(record.text)
    topic_entities = entities("", record.topic)
    for kind, requested in query.entities.items():
        actual = actual_entities.get(kind)
        if not actual:
            return Support.UNKNOWN
        if (
            text_entities.get(kind)
            and topic_entities.get(kind)
            and text_entities[kind].isdisjoint(topic_entities[kind])
        ):
            return Support.UNKNOWN
        if requested.isdisjoint(actual):
            return Support.CONFLICT
    if query.audience:
        audiences = {
            audience
            for audience, pattern in (
                ("personal", r"\b(?:personal|private)\b|个人|私人"),
                ("team", r"\b(?:team|shared)\b|团队|共享"),
            )
            if re.search(pattern, body, re.I)
        }
        if len(audiences) != 1:
            return Support.UNKNOWN
        if query.audience not in audiences:
            return Support.CONFLICT
    if query.question and not query.known_request:
        return Support.UNKNOWN
    if query.predicate and (record.kind != MemoryType.SEMANTIC or record.predicate != query.predicate):
        return Support.RELATED
    if query.domains and not any(
        any(literal_match(term, body) for term in _DOMAINS[domain][1]) for domain in query.domains
    ):
        return Support.RELATED
    if query.asks_time or query.event_action:
        if record.kind != MemoryType.EPISODIC:
            return Support.RELATED
        if query.asks_time and (
            record.occurred_at is None
            or record.episode_time_source not in ("explicit", "user")
            or record.episode_time_precision not in ("day", "instant")
        ):
            return Support.UNKNOWN
        if query.event_action:
            patterns = {
                "start": r"\b(?:began|begin|start(?:ed)?)\b|开始|启动",
                "finish": r"\b(?:finish(?:ed)?|complet(?:e|ed))\b|完成|结束",
            }
            if not re.search(patterns[query.event_action], record.text, re.I):
                return Support.RELATED
    if query.domains or query.entities or query.predicate or query.asks_time:
        return Support.SUPPORTED
    if query.question:
        return Support.UNKNOWN
    return (
        Support.SUPPORTED
        if semantic_match or any(literal_match(t, body) for t in query.terms)
        else Support.RELATED
    )
