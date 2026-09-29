#--------------------------------------------------------------
# Shovel 的主要运行文件, 定义 amain 函数
#
# 日期: 2026-09-15
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------------
from __future__ import annotations

import structlog

from shovel.config.settings import load_settings
from shovel.container import AppContainer


async def amain(config_file_path: str) -> None:
    """装配 DI 容器。

    host 的接入还没落地 —— 目前真正的运行入口是 ``shovel run``
    (见 ``cli/commands/run.py``), 这里只保留容器装配这一步。
    """

    log = structlog.get_logger()

    settings = load_settings(config_file_path)

    container = AppContainer()
    container.config.from_dict(settings.model_dump(mode="json"))

    log.info("shovel.container_ready", config_file_path=config_file_path)
