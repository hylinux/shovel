#--------------------------------------------------------------
# Shovel 的主要运行文件，定义amain函数
#
# 日期: 2026-09-15
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------------
from __future__ import annotations

from shovel.container import AppContainer

import structlog

from py_generic_host.hosting.builder import HostContext, WebHostBuilder


async def amain(config_file_path: str) -> None:
    container = AppContainer()

    

    log = structlog.get_logger()

