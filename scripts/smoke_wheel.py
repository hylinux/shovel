"""Run with the wheel-installed interpreter, not `uv run` or an editable checkout."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import tomli_w

import shovel
from shovel.config.settings import AppSettings
from shovel.runtime.client import execute_command
from shovel.runtime.hosting import build_host
from shovel.runtime.locking import RuntimeLock, endpoint_file
from shovel.vector.store import init_vector_store


async def smoke(root: Path) -> None:
    package = Path(shovel.__file__).parent
    assert "site-packages" in package.parts, f"Expected an installed wheel, got {package}"
    frontend = package / "static"
    assert (frontend / "index.html").is_file()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = root / "settings.toml"
    config.write_text(
        tomli_w.dumps(
            {
                "http_port": port,
                "database": {"url": f"sqlite+aiosqlite:///{(root / 'runtime.db').as_posix()}"},
                "zvec": {"knowledge_path": str(root / "vectors"), "log_dir": str(root / "logs"), "dim": 4},
                "agent": {"workspace_path": str(root / "workspace")},
                "memory": {"index_path": str(root / "memory-vectors"), "dim": 4},
            }
        ),
        encoding="utf-8",
    )
    settings = AppSettings.load(config)
    init_vector_store(settings.zvec)
    notes = root / "notes"
    notes.mkdir()
    (notes / "note.txt").write_text("The shipped workbench uses the durable queue.", encoding="utf-8")
    host = build_host(settings, development=False, frontend=frontend)
    try:
        await host.start_async()
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            async with asyncio.timeout(30):
                while True:
                    try:
                        if (await client.get("/api/v1/status")).status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    await asyncio.sleep(0.05)
            assert (await client.get("/")).status_code == 200
            created = await execute_command(
                settings,
                "resource.create",
                {
                    "name": "wheel/notes",
                    "connector_kind": "local_fs",
                    "config": {"root_path": str(notes)},
                },
            )
            assert created["state"] == "succeeded"
            scanned = await execute_command(settings, "scan", {"embed": False}, ref="wheel/notes")
            assert scanned["result"]["parsed"] == 1
            for _ in range(2):
                receipt = await execute_command(
                    settings,
                    "profile.save",
                    {
                        "name": "text",
                        "profile": {"include": ["*.txt"]},
                    },
                    ref="wheel/notes",
                    request_key="wheel-profile",
                )
                assert receipt["state"] == "succeeded"
            results = await client.post("/api/v1/search", json={"query": "shipped", "mode": "keyword"})
            assert results.status_code == 200 and results.json()["items"]
    finally:
        host.lifetime.stop_application()
        await host.stop_async()
    assert not endpoint_file(settings).exists()
    with RuntimeLock(settings):
        pass
    print("Wheel smoke passed: bundled Web, REST, online CLI, profile replay, retrieval, clean shutdown.")


if __name__ == "__main__":
    with TemporaryDirectory(prefix="shovel-wheel-") as directory:
        asyncio.run(smoke(Path(directory)))
