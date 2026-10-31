from __future__ import annotations

import json
import math
import os
from pathlib import Path
from statistics import median
from time import perf_counter

import pytest
from ag_ui.core import BaseEvent, EventType

from shovel.config.settings import AppSettings
from shovel.db.model.runtime import Conversation
from shovel.runtime.diagnostics import development_logging
from shovel.runtime.model_admission import local_ollama
from shovel.runtime.schemas import MessageInput
from test_knowledge_baseline import baseline_runtime, index_corpus, isolated_settings


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("SHOVEL_RUN_CPU_BENCHMARK") != "1", reason="Explicit local CPU benchmark opt-in required"
)
async def test_local_cpu_mode_and_context_comparison(tmp_path: Path) -> None:
    path = os.environ.get("SHOVEL_CPU_CONFIG")
    assert path, "Set SHOVEL_CPU_CONFIG to the explicitly selected settings.toml."
    configured = AppSettings.load(path)
    assert local_ollama(
        configured.model.model_provider_type, configured.model.base_endpoint, configured.model.model_name
    ), "CPU benchmark only accepts local, non-cloud Ollama models."
    for embedding in (configured.embedding, configured.memory.embedder):
        assert not embedding.is_configured or local_ollama(
            embedding.provider, embedding.base_url, embedding.model
        ), "The benchmark will not call remote embedding providers."
    memory_model = configured.memory.llm
    assert not memory_model.is_configured or local_ollama(
        memory_model.model_provider_type, memory_model.base_endpoint, memory_model.model_name
    ), "The benchmark will not call remote Memory models."
    samples = int(os.environ.get("SHOVEL_CPU_SAMPLES", "3"))
    assert 1 <= samples <= 30
    settings = isolated_settings(tmp_path, configured).model_copy(
        update={
            "agent": configured.agent.model_copy(
                update={"workspace_path": str(tmp_path / "workspace"), "enable_python_extensions": False}
            ),
            "memory": configured.memory.model_copy(update={"index_path": str(tmp_path / "memory-vectors")}),
        }
    )
    from shovel.vector.store import init_vector_store

    init_vector_store(settings.zvec)
    report: list[dict[str, object]] = []
    with development_logging():
        async with baseline_runtime(settings) as runtime:
            await index_corpus(runtime, tmp_path, embed=settings.embedding.is_configured)
            await runtime.chat.warm_model()
            for profile in ("default", "compact"):
                runtime.settings.performance.context_profile = profile
                for mode in ("agent", "chat"):
                    first_texts: list[float] = []
                    totals: list[float] = []
                    output_sizes: list[int] = []
                    for _ in range(samples):
                        async with runtime.sessions() as session:
                            conversation = Conversation()
                            session.add(conversation)
                            await session.commit()
                        await runtime.chat.send(
                            conversation.id,
                            MessageInput(
                                content="I am designing a local desktop assistant. Keep answers concise.",
                                interaction_mode="chat",
                                memory_enabled=False,
                            ),
                        )
                        started = perf_counter()
                        first_text: float | None = None

                        async def receive(event: BaseEvent) -> None:
                            nonlocal first_text
                            if first_text is None and event.type == EventType.TEXT_MESSAGE_CONTENT:
                                first_text = perf_counter() - started

                        answer = await runtime.chat.send(
                            conversation.id,
                            MessageInput(
                                content="解释本地知识库的离线备份和恢复步骤。",
                                interaction_mode=mode,
                                memory_enabled=False,
                            ),
                            on_event=receive,
                        )
                        totals.append((perf_counter() - started) * 1000)
                        assert first_text is not None and answer.state == "completed"
                        first_texts.append(first_text * 1000)
                        output_sizes.append(len(answer.content))
                    report.append(
                        {
                            "profile": profile,
                            "mode": mode,
                            "samples": samples,
                            "first_text_p50_ms": median(first_texts),
                            "first_text_p95_ms": sorted(first_texts)[math.ceil(samples * 0.95) - 1],
                            "durable_p50_ms": median(totals),
                            "durable_p95_ms": sorted(totals)[math.ceil(samples * 0.95) - 1],
                            "output_chars": output_sizes,
                            "memory_enabled": False,
                        }
                    )
    print("CPU_BENCHMARK=" + json.dumps(report))
