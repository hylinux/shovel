#---------------------------------------------------------
# 解析层
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------
"""字节 -> 有结构的文本块。

当前支持: txt/log、markdown、pdf、docx、xlsx、pptx。

注意本包**只导出协议与入口**, 不在这里 import 具体解析器 ——
六个解析器背后是 pypdf / lxml / openpyxl / Pillow 等一堆重依赖,
在 ``__init__`` 里 import 等于让每一条 CLI 命令都付这笔启动开销。
真正的 import 由 :mod:`registry` 在第一次路由到时才做。
"""

from shovel.domain.enums import BlockKind
from shovel.pipeline.parsers.base import (
    DEFAULT_MAX_CHARS,
    Block,
    HeadingStack,
    ParsedDocument,
    ParseHint,
    Parser,
    ParserSpec,
    clean_text,
)
from shovel.pipeline.parsers.registry import (
    can_parse,
    find_parser,
    parse_bytes,
    supported_suffixes,
)

__all__ = [
    "DEFAULT_MAX_CHARS",
    "Block",
    "BlockKind",
    "HeadingStack",
    "ParseHint",
    "ParsedDocument",
    "Parser",
    "ParserSpec",
    "can_parse",
    "clean_text",
    "find_parser",
    "parse_bytes",
    "supported_suffixes",
]
