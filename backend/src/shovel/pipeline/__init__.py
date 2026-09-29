#---------------------------------------------------------
# 扫描流水线
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------
"""把连接器列出来的条目变成可检索的 chunk。

这是 ``Connector.discover()`` 的消费者。整条链路:

```text
discover   连接器列出有什么             -> DiscoveredItem
fetch      按需取字节                   -> bytes
parse      字节 -> 有结构的文本块       -> ParsedDocument
chunk      文本块 -> parent + chunk 两层 -> ChunkDraft
embed      chunk -> 向量                -> list[float]
persist    写 SQLite (权威) + 写 Zvec   -> Document / Chunk 行
reconcile  本轮没见到的文档立碑         -> tombstoned
```

每个阶段是一个独立模块, 之间只靠纯数据结构通信。好处很具体: 测一个
解析器不需要数据库, 测切块不需要 embedding 服务, 测编排不需要真实
文件 —— 三者可以分别验证, 而不是只能端到端"跑一遍看看"。

本包**刻意不在 __init__ 里 import 具体解析器**: 它们背后是 pypdf /
lxml / openpyxl 等重依赖, 惰性导入交给 ``parsers.registry``。
"""

from shovel.pipeline.chunker import (
    CHUNKER_VERSION,
    ChunkDraft,
    ChunkerConfig,
    chunk_document,
)
from shovel.pipeline.embedder import Embedder, NullEmbedder, build_embedder
from shovel.pipeline.parsers import (
    Block,
    BlockKind,
    ParsedDocument,
    ParseHint,
    can_parse,
    parse_bytes,
    supported_suffixes,
)
from shovel.pipeline.scan_service import ScanOutcome, ScanService
from shovel.pipeline.sink import (
    ChunkVectorSink,
    NullVectorSink,
    VectorRecord,
    open_chunk_sink,
)

__all__ = [
    "CHUNKER_VERSION",
    "Block",
    "BlockKind",
    "ChunkDraft",
    "ChunkVectorSink",
    "ChunkerConfig",
    "Embedder",
    "NullEmbedder",
    "NullVectorSink",
    "ParseHint",
    "ParsedDocument",
    "ScanOutcome",
    "ScanService",
    "VectorRecord",
    "build_embedder",
    "can_parse",
    "chunk_document",
    "open_chunk_sink",
    "parse_bytes",
    "supported_suffixes",
]
