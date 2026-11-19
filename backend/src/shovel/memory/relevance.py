"""Bounded lexical candidates and conservative, query-specific Memory support checks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

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
        "start",
        "started",
        "begin",
        "began",
        "finish",
        "finished",
        "complete",
        "completed",
        "offline",
    }
)
_CHINESE_FUNCTIONS = re.compile(r"什么|哪个|哪些|哪种|我的|我在|我用|我|使用|喜欢|请问|告诉|记得|喝")
_NAME = r"(?:[\u3400-\u9fff]{1,32}-)?[a-z0-9][a-z0-9_.-]{0,63}"
_CHINESE_ENTITY_CONTEXT = r"(?:(?:我(?:们)?|用户)(?:在|的)\s*)?"
_ENTITY = re.compile(
    rf"\b(?P<kind>project|repository|repo|team|customer|client)\s+"
    rf"(?:(?:named|called|is)\s+)?[\"']?(?P<name>{_NAME})"
    rf"|(?P<prefix>项目|仓库|团队|客户)\s*[“「\"']?(?P<after>{_NAME})"
    rf"|(?<![\w.-]){_CHINESE_ENTITY_CONTEXT}(?P<before>{_NAME})\s*(?P<chinese>项目|仓库|团队|客户)"
    rf"|[“「\"](?P<quoted>[^”」\"]{{1,32}})[”」\"]\s*(?P<quoted_kind>项目|仓库|团队|客户)",
    re.IGNORECASE,
)
_KINDS = {
    "repo": "repository",
    "client": "customer",
    "projects": "project",
    "repositories": "repository",
    "repos": "repository",
    "teams": "team",
    "customers": "customer",
    "clients": "customer",
    "项目": "project",
    "仓库": "repository",
    "团队": "team",
    "客户": "customer",
}
_ENTITY_KIND = r"projects?|repositories|repository|repos?|teams?|customers?|clients?"
_SUFFIX_ENTITY = re.compile(
    rf"(?:\bthe\s+(?P<lower>{_NAME})|(?<![a-z0-9_.-])(?P<capital>(?-i:[A-Z])[a-z0-9_.-]{{0,63}}))"
    r"\s+(?P<kind>project|repository|repo|team|customer|client)\b",
    re.IGNORECASE,
)
_SEPARATOR = r"(?:,\s*(?:(?:and|or)\s+)?|\s+(?:and|or)\s+|\s*[、和与及]\s*)"
_NAMES = rf"{_NAME}(?:{_SEPARATOR}{_NAME})+"
_ENTITY_LIST = re.compile(
    rf"\b(?P<kind>{_ENTITY_KIND})\s+(?P<after>{_NAMES})"
    rf"|(?<![\w.-]){_CHINESE_ENTITY_CONTEXT}(?P<before>{_NAMES})"
    rf"\s*(?P<suffix>项目|仓库|团队|客户|\b(?:{_ENTITY_KIND})\b)"
    rf"|(?P<prefix>项目|仓库|团队|客户)\s*(?P<following>{_NAMES})",
    re.IGNORECASE,
)
_ALIAS = re.compile(rf"(?:also known as|aka|alias|别名(?:为|是)?)\s+[\"']?({_NAME})", re.IGNORECASE)
_NAMED_EVALUATION = re.compile(
    r"\b([A-Z][A-Za-z0-9_.-]{0,63})\s+(?:offline\s+)?(?:evaluation|benchmark|assessment)\b",
    re.I,
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
    "has_name": r"\b(?:name|nickname)\b|名字|姓名|昵称",
    "has_role": r"\b(?:occupation|profession|job|background)\b|职业|工作",
    "has_timezone": r"\btimezone\b|时区",
    "has_language": r"\blanguage\b|语言",
    "has_gender": r"\bgender\b|性别",
    "has_bio": r"\b(?:bio|biography)\b|自我介绍",
    "has_avatar": r"\bavatar\b|头像",
}
_PREDICATES = {
    "dislikes": r"\bdislikes?\b|不喜欢|讨厌",
    "prefers": r"\b(?:prefers?|likes?|favou?rite)\b|(?<!不)喜欢|偏好",
    "uses": r"\b(?:uses?|using)\b|使用|用什么|用的",
}
_ACTIONS = {
    "start": r"\b(?:begin|began|start(?:ed)?)\b|开始|启动",
    "finish": r"\b(?:finish(?:ed)?|complet(?:e|ed))\b|完成|结束",
}
_EVENT_PROJECT = (
    rf"(?:the\s+)?(?:(?:test\s+)?project\s+[\"']?{_NAME}[\"']?|{_NAME}\s+project"
    rf"|(?:测试)?项目\s*[“「\"']?{_NAME}[”」\"']?|{_NAME}\s*项目)"
)
_EVENT_EVALUATION = (
    rf"(?:(?:the\s+)?(?:offline\s+)?(?:evaluation|benchmark|assessment)\s+(?:for|of)\s+{_EVENT_PROJECT}"
    rf"|(?:the\s+)?{_NAME}\s+(?:offline\s+)?(?:evaluation|benchmark|assessment)"
    rf"|{_EVENT_PROJECT}\s*的?\s*(?:离线)?(?:评估|测评)"
    rf"|{_NAME}\s*(?:离线)?(?:评估|测评))"
)
_EVENT_ACTOR = r"(?:i|the user|user)"
_EVENT_VERB = rf"(?P<verb>{'|'.join(_ACTIONS.values())})(?:了)?"
_CHINESE_EVENT_TIME = r"(?:什么时候|何时|哪一天|哪天)"
_EVENT_QUESTIONS = tuple(
    re.compile(pattern, re.I)
    for pattern in (
        rf"when\s+did\s+(?P<actor>{_EVENT_ACTOR})\s+{_EVENT_VERB}\s+(?P<target>.+)",
        rf"when\s+(?:did|was)\s+(?P<target>.+?)\s+{_EVENT_VERB}",
        rf"(?P<actor>我|用户)\s*(?:是|在)?{_CHINESE_EVENT_TIME}\s*{_EVENT_VERB}\s*(?P<target>.+?)\s*的?",
        rf"(?P<target>.+?)\s*(?:是|在)?{_CHINESE_EVENT_TIME}\s*{_EVENT_VERB}\s*的?",
    )
)
_EVENT_STATEMENTS = tuple(
    re.compile(pattern, re.I)
    for pattern in (
        rf"(?P<actor>{_EVENT_ACTOR})\s+(?:has\s+)?{_EVENT_VERB}\s+(?P<target>.+)",
        rf"(?P<actor>我|用户)\s*{_EVENT_VERB}\s*(?P<target>.+)",
        rf"(?P<target>.+?)\s+(?:was\s+)?{_EVENT_VERB}(?:\s+by\s+(?P<actor>{_EVENT_ACTOR}))?",
        rf"{_EVENT_VERB}\s+(?P<target>.+)",
        rf"(?P<target>.+?)\s*{_EVENT_VERB}",
    )
)
_EVENT_DATE = re.compile(
    r"(?<![a-z0-9_.-])(?:on\s+|在\s*|于\s*)?"
    r"(?:\d{4}-\d{2}-\d{2}|\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)(?![0-9-])",
    re.I,
)
_NEGATION = re.compile(
    r"\b(?:not|never|neither|nor|without|no(?:\s+longer)?)\b|\b[a-z]+n['\u2019]t\b"
    r"|没有|并未|未曾|从未|从不|不再|不(?:使用|用|是|曾)|没(?:使用|用|完成|开始)|未(?:使用|用|完成|开始)",
    re.IGNORECASE,
)
_HISTORICAL = re.compile(
    r"\b(?:previous(?:ly)?|former(?:ly)?|historical(?:ly)?|earlier|before|used\s+to|in\s+the\s+past)\b"
    r"|(?:^|\b(?:i|we|you|they|he|she|the\s+user)\s+)used\b"
    r"|\blast\s+(?:year|month|week)\b|\bdid\b.{0,80}\b(?:use|prefer|like|dislike)\b"
    r"|\b(?:was|were)\b.{0,80}\b(?:using|preferring)\b"
    r"|以前|之前|过去|曾经|原先|原来",
    re.IGNORECASE,
)
_CALENDAR = re.compile(
    r"\b(?:in|during|for|as\s+of)\s+(?:19|20)\d{2}\b"
    r"|(?<![a-z0-9_])(?:19|20)\d{2}(?:-\d{2}(?:-\d{2})?|\s*年)(?![a-z0-9_])",
    re.IGNORECASE,
)
_QUESTION_WORD = re.compile(
    r"\b(?:what|which|when|who|why|how)\b|什么|何时|哪一天|哪天|哪种|哪个|哪家|谁", re.I
)
_RESPONSE_DIRECTIVE = re.compile(
    r"(?:请)?(?:只|仅)(?:依据|根据|基于)(?:我(?:已)?保存的|已保存的(?:我的)?|我的)?"
    r"(?:个人)?(?:记忆|记录)(?:来)?(?:回答|作答)"
    r"|(?:如果|若)?(?:没有|未找到|找不到)(?:对应的?|相关的?)?(?:记忆|记录)"
    r"(?:就请|则请|就|则|请)(?:明确|直接|如实)?(?:说明|告知|回答)(?:没有(?:记录)?|不知道|未知)?"
)
_MEMORY_RECHECK = re.compile(
    r"(?:请)?(?:你)?(?:再)?(?:查一下|查查|查询|检索|检查一下|看看)(?:长期)?记忆"
    r"(?:[\uff0c,]\s*(?:再)?看看记忆里(?:有没有|是否有|没有)记录)?"
    r"|记忆里(?:有没有|没有)记录(?:吗)?"
    r"|(?:please\s+)?(?:recheck|check(?:\s+again)?)\s+(?:the\s+)?"
    r"(?:saved\s+)?(?:long-term\s+)?memory(?:\s+again)?",
    re.I,
)


def factual_query(text: str) -> str:
    # Preserve qualifiers; only complete response policies and identical question clauses are redundant.
    clauses = re.split(r"([。\uff01\uff1f\uff1b;!?\n]+)", text)
    seen: set[str] = set()
    result = []
    for index in range(0, len(clauses), 2):
        clause = clauses[index]
        if _RESPONSE_DIRECTIVE.fullmatch(clause.strip()):
            continue
        if _QUESTION_WORD.search(clause):
            key = normalize(clause)
            if key in seen:
                continue
            seen.add(key)
        result.append(clause)
        if index + 1 < len(clauses):
            result.append(clauses[index + 1])
    return "".join(result)


def memory_only_query(text: str) -> bool:
    return any(
        _RESPONSE_DIRECTIVE.fullmatch(clause.strip()) and re.match(r"(?:请)?(?:只|仅)", clause.strip())
        for clause in re.split(r"[。\uff01\uff1f\uff1b;!?\n]+", text)
    )


def relation_matches(text: str, patterns: dict[str, str]) -> frozenset[str]:
    return frozenset(name for name, pattern in patterns.items() if re.search(pattern, text, re.I))


def unverified_qualifier(text: str) -> bool:
    # Records have no polarity or semantic validity interval; do not infer either from overlap.
    return bool(_NEGATION.search(text) or _HISTORICAL.search(text))


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


def _topic_entities(topic: str, explicit: dict[str, set[str]]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for kind, value in re.findall(
        r"(?:^|[.:/])(project|repository|team|customer)[.:/]((?:[\u3400-\u9fff]{1,32}-)?[a-z0-9_-]+)",
        topic,
        re.I,
    ):
        kind, value = kind.casefold(), value.casefold()
        names = explicit.get(kind, set())
        if len(names) == 1:
            name = next(iter(names))
            # A topic attribute suffix is not another entity, but only explicit text can disambiguate it.
            if any(value == f"{name}_{attribute}" for attribute in (*_DOMAINS, "language")):
                value = name
        result.setdefault(kind, set()).add(value)
    return result


def entities(text: str, topic: str = "", *, include_aliases: bool = True) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for match in _ENTITY.finditer(text):
        kind = match["kind"] or match["prefix"] or match["chinese"] or match["quoted_kind"]
        value = normalize(match["name"] or match["after"] or match["before"] or match["quoted"]).rstrip(".")
        if value not in _FUNCTION_WORDS:
            result.setdefault(_KINDS.get(kind.casefold(), kind.casefold()), set()).add(value)
    for match in _SUFFIX_ENTITY.finditer(text):
        value = normalize(match["lower"] or match["capital"]).rstrip(".")
        if value not in _FUNCTION_WORDS:
            kind = match["kind"].casefold()
            result.setdefault(_KINDS.get(kind, kind), set()).add(value)
    for match in _ENTITY_LIST.finditer(text):
        kind = (match["kind"] or match["suffix"] or match["prefix"]).casefold()
        names = re.split(_SEPARATOR, match["after"] or match["before"] or match["following"], flags=re.I)
        result.setdefault(_KINDS.get(kind, kind), set()).update(
            normalize(name).rstrip(".") for name in names if normalize(name) not in _FUNCTION_WORDS
        )
    for pattern in (_NAMED_EVALUATION, _MIXED_EVALUATION):
        for value in pattern.findall(text):
            if value.casefold() not in _FUNCTION_WORDS:
                result.setdefault("project", set()).add(value.casefold())
    for kind, topic_names in _topic_entities(topic, result).items():
        result.setdefault(kind, set()).update(topic_names)
    if include_aliases and len(result) == 1 and len(next(iter(result.values()))) == 1:
        next(iter(result.values())).update(normalize(alias) for alias in _ALIAS.findall(text))
    return result


def query_terms(query: str) -> list[str]:
    query = factual_query(query)
    event = event_statement(query, question=True)
    parts = event_question_parts(query)
    terms = [
        *([event.target] if event else []),
        *(part.event.target for part in parts if part.event is not None),
        *(term for term in keyword_terms(_CHINESE_FUNCTIONS.sub(" ", query)) if term not in _FUNCTION_WORDS),
    ]
    for pattern, aliases in _DOMAINS.values():
        if re.search(pattern, query, re.IGNORECASE):
            terms.extend(aliases)
    return list(dict.fromkeys(terms))[:64]


@dataclass(frozen=True)
class Event:
    actor: str | None
    target_kind: str
    target: str


def event_statement(text: str, *, question: bool = False) -> Event | None:
    """Bind one complete event clause; ownership never supplies a missing actor."""
    if not question:
        text = _EVENT_DATE.sub("", text, count=1)
    text = text.strip(" \t\r\n.,;:!?。\uff0c\uff1b\uff1a\uff01\uff1f")
    for pattern in _EVENT_QUESTIONS if question else _EVENT_STATEMENTS:
        if match := pattern.fullmatch(text):
            target = match["target"].strip()
            if re.fullmatch(_EVENT_EVALUATION, target, re.I):
                kind = "evaluation"
            elif re.fullmatch(_EVENT_PROJECT, target, re.I):
                kind = "project"
            else:
                continue
            names = entities(target, include_aliases=False)
            if set(names) != {"project"} or len(names["project"]) != 1:
                continue
            actor = match.groupdict().get("actor")
            if actor is not None:
                actor = normalize(actor)
                if actor in {"i", "user", "the user", "我", "用户"}:
                    actor = "user"
            return Event(actor, kind, next(iter(names["project"])))
    return None


def _event_entities(
    text: str, event: Event | None, topic: str = "", *, include_aliases: bool = True
) -> dict[str, set[str]]:
    if event is None:
        return entities(text, topic, include_aliases=include_aliases)
    # Only a completely bound clause can separate a Chinese name from its actor/verb.
    result = {"project": {event.target}}
    for kind, names in _topic_entities(topic, result).items():
        result.setdefault(kind, set()).update(names)
    return result


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
    unverified_conditions: bool
    audience: str | None
    personal_actor: bool
    terms: tuple[str, ...]
    event: Event | None = None

    @classmethod
    def parse(cls, text: str) -> Query:
        text = factual_query(text)
        actions = relation_matches(text, _ACTIONS)
        action = next(iter(actions)) if len(actions) == 1 else None
        asks_time = bool(re.search(rf"\bwhen\b|{_CHINESE_EVENT_TIME}|日期", text, re.I))
        event = event_statement(text, question=True) if asks_time and action else None
        conditions = _event_entities(text, event)
        entity_hint = bool(re.search(rf"\b(?:{_ENTITY_KIND})\b|项目|仓库|团队|客户", text, re.I))
        domains = frozenset(name for name, (pattern, _) in _DOMAINS.items() if re.search(pattern, text, re.I))
        if conditions and re.search(r"\blanguages?\b|语言", text, re.I):
            domains |= {"programming"}
        profiles = frozenset(
            name
            for name, pattern in _PROFILES.items()
            if not entity_hint and "programming" not in domains and re.search(pattern, text, re.I)
        )
        predicates = relation_matches(text, _PREDICATES)
        predicate = next(iter(predicates)) if len(predicates) == 1 else None
        actor = re.search(r"\b(?:do|does|did)\s+(.+?)\s+(?:use|prefer|like|dislike)\b", text, re.I)
        unknown_actor = bool(
            actor and normalize(actor[1]) not in {"i", "the user"} and not entities(actor[1])
        )
        known_request = bool(
            event is not None
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
            bool(re.search(r"[?\uff1f]", text) or _QUESTION_WORD.search(text)),
            known_request,
            (entity_hint and not conditions) or unknown_actor,
            unverified_qualifier(text)
            or bool(_CALENDAR.search(text))
            or any(len(names) > 1 for names in conditions.values())
            or len(domains) > 1
            or len(predicates) > 1
            or len(actions) > 1
            or len(_QUESTION_WORD.findall(text)) > max(1, len(profiles)),
            "personal"
            if re.search(r"\b(?:personal|private)\b|个人|私人", text, re.I)
            else "team"
            if re.search(r"\b(?:team|shared)\b|团队|共享", text, re.I)
            else None,
            bool(
                re.search(
                    r"\bi\s+(?:use|prefer|like|dislike)|我(?:使用|喜欢|用)"
                    rf"|我在\s*{_NAME}\s*(?:项目|仓库|团队|客户)(?:中|里)?(?:使用|喜欢|用)",
                    text,
                    re.I,
                )
            ),
            tuple(query_terms(text)),
            event,
        )


_PAIR_VERB = rf"(?:{'|'.join(_ACTIONS.values())})(?:了)?"
_PAIR_SEPARATOR = r"\s*(?:、|\uff0c|,|和|与|及)\s*"
_EVENT_PAIRS = (
    (
        re.compile(
            rf"(?P<target>{_EVENT_PROJECT})\s*(?:是|在)?"
            rf"{_CHINESE_EVENT_TIME}\s*(?P<first>{_PAIR_VERB}){_PAIR_SEPARATOR}"
            rf"(?:{_CHINESE_EVENT_TIME}\s*)?(?P<second>{_PAIR_VERB})\s*的?",
            re.I,
        ),
        "{target}何时{action}?",
    ),
    (
        re.compile(
            rf"(?P<actor>我|用户)\s*(?:是|在)?{_CHINESE_EVENT_TIME}\s*"
            rf"(?P<first>{_PAIR_VERB}){_PAIR_SEPARATOR}(?:{_CHINESE_EVENT_TIME}\s*)?"
            rf"(?P<second>{_PAIR_VERB})\s*(?P<target>{_EVENT_PROJECT})\s*的?",
            re.I,
        ),
        "{actor}何时{action}{target}?",
    ),
    (
        re.compile(
            rf"when\s+did\s+(?P<actor>{_EVENT_ACTOR})\s+(?P<first>{_PAIR_VERB})"
            rf"\s+and\s+(?P<second>{_PAIR_VERB})\s+(?P<target>.+)",
            re.I,
        ),
        "When did {actor} {action} {target}?",
    ),
    (
        re.compile(
            rf"when\s+(?:did|was)\s+(?P<target>.+?)\s+(?P<first>{_PAIR_VERB})"
            rf"\s+and\s+(?P<second>{_PAIR_VERB})",
            re.I,
        ),
        "When did {target} {action}?",
    ),
)


def event_question_parts(text: str) -> tuple[Query, ...]:
    """Split only a complete shared-project start/finish pair, never arbitrary clauses."""
    clauses = re.split(r"[。\uff01\uff1f\uff1b;!?\n]+", text.strip().rstrip("."))
    if any(
        clause.strip() not in ("请分别说明", "没有记录的部分不要推测")
        and not _RESPONSE_DIRECTIVE.fullmatch(clause.strip())
        for clause in clauses[1:]
        if clause.strip()
    ):
        return ()
    for pattern, template in _EVENT_PAIRS:
        if match := pattern.fullmatch(clauses[0].strip()):
            if relation_matches(f"{match['first']} {match['second']}", _ACTIONS) != {"start", "finish"}:
                return ()
            parts = tuple(
                Query.parse(template.format(**match.groupdict(), action=match[name]))
                for name in ("first", "second")
            )
            if (
                all(
                    part.event is not None
                    and part.event.target_kind == "project"
                    and part.known_request
                    and not part.unverified_conditions
                    for part in parts
                )
                and parts[0].event == parts[1].event
            ):
                return parts
    return ()


def resolve_memory_query(text: str, previous_question: str | None) -> str | None:
    """Resolve only an explicit recheck against one immediately preceding user request."""
    if not _MEMORY_RECHECK.fullmatch(text.strip().rstrip(".。!?\uff01\uff1f").strip()):
        return text
    if not previous_question:
        return None
    query = Query.parse(previous_question)
    if event_question_parts(previous_question) or independent_questions(previous_question):
        return previous_question
    single_target = (
        len(query.entities) <= 1
        and all(len(names) == 1 for names in query.entities.values())
        and bool(query.domains or query.predicate or query.event)
    ) or (len(query.profiles) == 1 and not query.entities)
    if (
        not single_target
        or not query.question
        or not query.known_request
        or query.unresolved_entity
        or len(query.domains) > 1
        or len(_QUESTION_WORD.findall(factual_query(previous_question))) > 1
    ):
        return None
    return previous_question


def independent_questions(text: str) -> tuple[str, ...]:
    """Split at most four complete questions, never an inherited target or a trailing qualifier."""
    clauses = tuple(
        clause.strip()
        for clause in re.split(r"[。\uff01\uff1f\uff1b;!?\n]+", factual_query(text))
        if clause.strip()
    )
    if not 2 <= len(clauses) <= 4:
        return ()
    profile_attribute = "(?:" + "|".join(_PROFILES.values()) + ")"
    for clause in clauses:
        query = Query.parse(clause)
        named_target = len(query.entities) == 1 and all(len(names) == 1 for names in query.entities.values())
        own_profile = (
            len(query.profiles) == 1
            and not query.entities
            and re.fullmatch(
                rf"(?:what\s+is\s+my\s+{profile_attribute}|我的{profile_attribute}(?:是什么|是哪个))",
                clause,
                re.I,
            )
        )
        if not (query.question and query.known_request and (named_target or own_profile)):
            return ()
    return clauses


def assess(
    query: Query,
    record: MemoryRecord,
    *,
    semantic_match: bool,
    diagnostics: dict[str, int] | None = None,
) -> Support:
    def reject(
        reason: Literal[
            "unresolved_entity",
            "unverified_conditions",
            "unsupported_question",
            "event_binding",
            "event_actor",
            "event_time",
            "entity_missing",
            "entity_binding",
            "entity_conflict",
        ],
        verdict: Support = Support.UNKNOWN,
    ) -> Support:
        if diagnostics is not None:
            key = f"support_rejected_{reason}"
            diagnostics[key] = diagnostics.get(key, 0) + 1
        return verdict

    if record.subject == "user" and record.predicate in PROFILE_TOPICS:
        return (
            Support.SUPPORTED
            if record.predicate in query.profiles
            and not query.asks_time
            and not query.unverified_conditions
            and (not query.question or query.known_request)
            else Support.BACKGROUND
        )
    if query.profiles:
        return Support.RELATED
    if query.unresolved_entity:
        return reject("unresolved_entity")
    if query.unverified_conditions:
        return reject("unverified_conditions")
    if query.personal_actor and record.kind == MemoryType.SEMANTIC and record.subject != "user":
        return Support.RELATED
    body = " ".join((record.text, record.object, record.topic))
    actual_event = event_statement(record.text) if record.kind == MemoryType.EPISODIC else None
    actual_entities = _event_entities(record.text, actual_event, record.topic)
    text_entities = _event_entities(record.text, actual_event)
    if (
        unverified_qualifier(record.text)
        or len(relation_matches(record.text, _PREDICATES)) > 1
        or len(relation_matches(record.text, _ACTIONS)) > 1
        or (record.kind == MemoryType.SEMANTIC and _CALENDAR.search(record.text))
    ):
        return Support.UNKNOWN
    if any(
        len(names) > 1
        for names in _event_entities(record.text, actual_event, record.topic, include_aliases=False).values()
    ):
        return reject("entity_binding")
    topic_entities = _topic_entities(
        record.topic, _event_entities(record.text, actual_event, include_aliases=False)
    )
    for kind, requested in query.entities.items():
        actual = actual_entities.get(kind)
        if not actual:
            return reject("entity_missing")
        if (
            text_entities.get(kind)
            and topic_entities.get(kind)
            and text_entities[kind].isdisjoint(topic_entities[kind])
        ):
            return reject("entity_binding")
        if requested.isdisjoint(actual):
            return reject("entity_conflict", Support.CONFLICT)
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
        return reject("unsupported_question")
    if query.predicate and (record.kind != MemoryType.SEMANTIC or record.predicate != query.predicate):
        return Support.RELATED
    if query.domains and not any(
        any(literal_match(term, body) for term in _DOMAINS[domain][1]) for domain in query.domains
    ):
        return Support.RELATED
    if query.asks_time or query.event_action:
        if record.kind != MemoryType.EPISODIC:
            return Support.RELATED
        if query.event is None or actual_event is None or query.event.target_kind != actual_event.target_kind:
            return reject("event_binding")
        if query.event.target != actual_event.target:
            return Support.CONFLICT
        if query.event.actor is not None and query.event.actor != actual_event.actor:
            return reject("event_actor")
        if query.asks_time and (
            record.occurred_at is None
            or record.episode_time_source not in ("explicit", "user")
            or record.episode_time_precision not in ("day", "instant")
        ):
            return reject("event_time")
        if query.event_action and not re.search(_ACTIONS[query.event_action], record.text, re.I):
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
