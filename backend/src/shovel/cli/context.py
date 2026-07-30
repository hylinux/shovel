from __future__ import annotations

from shovel.services.config_server import ShovelContext


class CliContext(ShovelContext):

    def __init__(self):
        """
        根据操作系统读出默认的用户Profile路径
        """
        super().__init__()

cli_context = CliContext()

