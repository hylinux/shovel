from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class DocumentType(Enum):
    TEXT = 1
    CSV  = 2
    MARKDOWN = 3
    PDF = 4
    WORD = 5
    HTML = 6
    XML = 7


class DocumentSettings(BaseModel):
    """``[document]`` 段。

    空串而非 ``None``: TOML 无法表达 null, ``None`` 会让这个键在
    ``shovel init`` 生成的配置文件里直接消失。
    """

    #: 文档根目录; 留空表示不限制, 由各资源自己给出路径。
    root_dir: str = ""
