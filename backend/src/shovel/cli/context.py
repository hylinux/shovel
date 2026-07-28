from dataclasses import dataclass


@dataclass(slots=True, kw_only=True)
class CliContext:

    def __init__(self):
        """
        根据操作系统读出默认的用户Profile路径
        """
