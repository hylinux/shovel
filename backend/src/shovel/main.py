# --------------------------------------------------------------
# Shovel 的主要运行文件, 定义 amain 函数
#
# 日期: 2026-09-15
# 作者: HongWei Guo <hongweig@163.com>
# ---------------------------------------------------------------
from __future__ import annotations

from pathlib import Path

from shovel.config.settings import load_settings
from shovel.runtime.hosting import build_host


async def amain(config_file_path: str) -> None:
    """Run the hosted backend; attach exported assets when frontend_path is set."""
    settings = load_settings(config_file_path)
    host = build_host(
        settings,
        development=False,
        frontend=Path(settings.frontend_path) if settings.frontend_path else None,
    )
    await host.run_async()
    if host.exit_code:
        raise SystemExit(host.exit_code)
