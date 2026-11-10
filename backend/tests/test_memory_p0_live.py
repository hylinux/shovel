from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from scripts.memory_baseline import baseline_runtime, isolated_settings, seed
from scripts.memory_support_evaluation import (
    CASES,
    AnswerTransport,
    SupportDataset,
    source_fingerprints,
)

from shovel.config.settings import AppSettings
from shovel.db.model.runtime import Conversation
from shovel.runtime.schemas import MessageInput

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("SHOVEL_RUN_LIVE_TESTS") != "1",
        reason="Explicit live endpoint opt-in required",
    ),
]


def _free_text_answer_correct(content: str, expected: str | None) -> bool:
    if expected is not None:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", expected):
            day = date.fromisoformat(expected)
            month = day.strftime("%B")
            return bool(
                re.search(
                    rf"\b{expected}\b|\b{month}\s+{day.day},?\s+{day.year}\b"
                    rf"|\b{day.day}\s+{month}\s+{day.year}\b"
                    rf"|{day.year}年{day.month}月{day.day}日",
                    content,
                    re.I,
                )
            )
        return expected in content
    return bool(
        re.search(
            r"没有|未找到|未记录|无法|不足|未提供|不清楚|不确定"
            r"|\b(?:no|not|cannot|unknown|unable|insufficient)\b|(?:don|can)['\u2019]t"
            r"|\bneed\s+(?:a\s+bit\s+)?more\s+context\b|\b(?:can|could)\s+refer\s+to\b",
            content,
            re.I,
        )
    ) and not re.search(
        r"(?:\byou\s+(?:use|are\s+using)|\bdatabase\s+is"
        r"|\b(?:project|repository)(?:\s+[\w.-]+)?\s+uses?"
        r"|(?:项目|仓库)(?:使用(?:的是|了)?|数据库(?:是|为)))\s*"
        r"(?:SQLite|MySQL|PostgreSQL)|2026-(?:09-10|08-21)",
        content,
        re.I,
    )


@pytest.mark.parametrize(
    "name,prompt,expected",
    [
        ("positive", "Which database do I use for project Solstice?", "SQLite"),
        ("alias", "Equinox 项目使用什么数据库\uff1f", "SQLite"),
        ("event", "When did the Solstice evaluation finish?", "2026-09-10"),
        ("negation", "Solstice 项目没有使用哪个数据库\uff1f", None),
        ("historical", "Which database did I use for project Solstice?", None),
        ("multi-entity", "Solstice 和 Tundra 项目分别使用什么数据库\uff1f", None),
        ("compound", "Which database and programming language do I use for project Solstice?", None),
        ("event-directions", "When did the Solstice evaluation start and finish?", None),
    ],
)
async def test_live_p0_free_text_answers_and_actual_memory_context(
    tmp_path: Path, name: str, prompt: str, expected: str | None
) -> None:
    await _live_free_text(tmp_path, "p0", name, prompt, expected)


@pytest.mark.parametrize(
    "name,prompt,expected",
    [
        ("topic", "Which database do I use for project Atlas?", "MySQL"),
        ("suffix", "Which database does the Quartz repository use?", "SQLite"),
        ("event", "When did the Heron evaluation finish?", "2026-08-21"),
        ("alias", "Which database do I use for project Zenith?", "MySQL"),
        ("literal", "Which database do I use for project Harbor_database?", "PostgreSQL"),
        ("conflict", "Which database do I use for project Stratus?", None),
        ("literal-prefix", "Which database do I use for project Harbor?", None),
        ("event-direction", "When did the Heron evaluation begin?", None),
    ],
)
async def test_live_p1_free_text_answers_and_actual_memory_context(
    tmp_path: Path, name: str, prompt: str, expected: str | None
) -> None:
    await _live_free_text(tmp_path, "p1", name, prompt, expected)


async def _live_free_text(tmp_path: Path, phase: str, name: str, prompt: str, expected: str | None) -> None:
    config = os.environ.get("SHOVEL_LIVE_CONFIG")
    assert config, "Select an existing settings.toml with SHOVEL_LIVE_CONFIG."
    configured = AppSettings.load(Path(config))
    assert configured.model.is_configured and configured.memory.embedder is not None
    settings = isolated_settings(configured, tmp_path)
    cases = CASES.with_name(f"memory_support_{phase}_cases.json")
    dataset = SupportDataset.model_validate_json(cases.read_bytes())
    sources = source_fingerprints()
    report: dict[str, Any] = {
        "case": name,
        "prompt": prompt,
        "expected": expected,
        "answer_model": settings.model.model_name,
        "source_sha256": sources,
        "test_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "dataset_sha256": hashlib.sha256(cases.read_bytes()).hexdigest(),
    }
    async with baseline_runtime(settings) as runtime:
        await seed(runtime, dataset.facts)
        await runtime.memory.rebuild()
        transport = AnswerTransport()
        payloads: list[dict[str, Any]] = []

        async def capture_request(request: httpx.Request) -> None:
            body = json.loads(await request.aread())
            payloads[:] = [
                json.loads(message["content"].split("\n", 1)[1])
                for message in body["messages"]
                if isinstance(message.get("content"), str)
                and message["content"].startswith(("Host ", "本轮未受信任的参考数据"))
            ]

        runtime.chat.agent._client = httpx.AsyncClient(
            transport=transport, event_hooks={"request": [capture_request]}
        )
        async with runtime.sessions() as session:
            conversation = Conversation()
            session.add(conversation)
            await session.commit()
        try:
            answer = await runtime.chat.send(conversation.id, MessageInput(content=prompt))
            report["answer"] = answer.content
            report["state"] = answer.state
            report["model_memory_context"] = payloads
            memory_payload = next(payload for payload in payloads if "memory_status" in payload)
            supported = [
                item
                for payload in payloads
                for item in payload.get("memory", [])
                if item.get("answer_support") == "supported"
            ]
            checks = {
                "completed": answer.state == "completed",
                "actual_request_support": bool(supported) == (expected is not None),
                "actual_request_status": memory_payload["memory_status"]
                == ("supported" if expected is not None else "no_supported_fact"),
                "answer": _free_text_answer_correct(answer.content, expected),
            }
            report["checks"] = checks
            assert source_fingerprints() == sources, "Source changed during live measurement."
            assert all(checks.values()), report
        finally:
            directory = Path(os.environ.get(f"SHOVEL_{phase.upper()}_LIVE_REPORT_DIR", str(tmp_path)))
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"{phase}-free-text-{name}.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
