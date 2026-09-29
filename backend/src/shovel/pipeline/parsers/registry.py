#---------------------------------------------------------
# 解析器注册表
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------
"""按文档类型挑解析器。

和连接器注册表是同一套思路 (注册进去的是类, 不是数据行), 但路由规则
不同, 这里只有一条, 且必须写清楚:

**扩展名优先于 doc_class。**

原因是 ``DocClass.OFFICE_DOC`` 一个值同时覆盖 docx / xlsx / pptx, 而这
三者的解析器毫无共同之处。如果按 doc_class 路由, 三个解析器会抢同一个
key, 注册表只能随机挑一个。扩展名才是真正有区分度的那一维; doc_class
退化成兜底 —— 当扩展名完全不认识时, 才拿它来猜。

惰性导入: pypdf / python-docx / openpyxl / python-pptx 加起来的 import
开销在百毫秒量级。``shovel resource list`` 不该为一个它永远用不到的
PDF 解析器付这笔钱, 所以真正的 import 发生在第一次路由到它的时候。
"""

from __future__ import annotations

import importlib
import threading
from typing import TYPE_CHECKING

from shovel.domain.enums import DocClass
from shovel.exceptions.pipeline import UnsupportedDocumentError

if TYPE_CHECKING:
    from shovel.pipeline.parsers.base import ParsedDocument, ParseHint, Parser

#: 扩展名 -> 实现模块。值是模块名, 不是类 —— 这正是惰性导入的载体。
_BY_SUFFIX: dict[str, str] = {
    ".txt": "plain_text",
    ".log": "plain_text",
    ".text": "plain_text",
    ".md": "markdown_doc",
    ".markdown": "markdown_doc",
    ".mdx": "markdown_doc",
    ".pdf": "pdf_doc",
    ".docx": "word_doc",
    ".xlsx": "excel_doc",
    ".xlsm": "excel_doc",
    ".pptx": "powerpoint_doc",
}

#: doc_class -> 实现模块。只在扩展名认不出时使用。
#:
#: OFFICE_DOC 刻意**不在**这里: 一个没有扩展名、只知道"是个 office 文档"
#: 的文件, 猜 docx 还是 xlsx 都是五五开, 猜错的代价是一条看不懂的报错。
#: 不如直接报"不支持", 让用户把扩展名补上。
_BY_DOC_CLASS: dict[DocClass, str] = {
    DocClass.PLAIN_TEXT: "plain_text",
    DocClass.CODE: "plain_text",
    DocClass.MARKDOWN: "markdown_doc",
    DocClass.PDF_DOC: "pdf_doc",
}

#: 旧版二进制 Office (.doc/.xls/.ppt)。单独列出来, 是为了给一条
#: "另存为 .docx"的可行建议, 而不是一句泛泛的"不支持"。
_LEGACY_OFFICE = frozenset({".doc", ".xls", ".ppt"})

_CACHE: dict[str, Parser] = {}
_LOCK = threading.Lock()


def _load(module_name: str) -> Parser:
    """按模块名取解析器实例, 带进程级缓存。

    解析器无状态, 因此复用同一个实例是安全的, 也省掉了每篇文档都
    重新构造一次的开销 (在几万篇的扫描里这不是零)。
    """

    cached = _CACHE.get(module_name)
    if cached is not None:
        return cached

    with _LOCK:
        # 双检: 拿锁期间可能已经被另一个线程装好了
        cached = _CACHE.get(module_name)
        if cached is not None:
            return cached

        module = importlib.import_module(
            f"shovel.pipeline.parsers.{module_name}"
        )
        parser: Parser = module.PARSER
        _CACHE[module_name] = parser

        return parser


def find_parser(hint: ParseHint) -> Parser:
    """给一篇文档挑解析器。挑不出来抛 :class:`UnsupportedDocumentError`。"""

    suffix = hint.suffix

    if suffix in _LEGACY_OFFICE:
        raise UnsupportedDocumentError(hint.uri, suffix)

    module_name = _BY_SUFFIX.get(suffix)

    if module_name is None:
        module_name = _BY_DOC_CLASS.get(hint.doc_class)

    if module_name is None:
        raise UnsupportedDocumentError(hint.uri, suffix)

    return _load(module_name)


def can_parse(hint: ParseHint) -> bool:
    """能不能解析。用于 discover 之后、读盘之前的预筛。

    预筛是有实际收益的: 一个 4 GB 的 .iso 文件, 提前判定为不支持就
    完全不必读它, 而"读完再发现不支持"要白跑一趟 IO。
    """

    if hint.suffix in _LEGACY_OFFICE:
        return False

    return hint.suffix in _BY_SUFFIX or hint.doc_class in _BY_DOC_CLASS


def parse_bytes(payload: bytes, hint: ParseHint) -> ParsedDocument:
    """路由 + 解析。这是解析层对外的唯一入口。"""

    return find_parser(hint).parse(payload, hint)


def supported_suffixes() -> tuple[str, ...]:
    """当前支持的扩展名, 供 CLI 展示。"""

    return tuple(sorted(_BY_SUFFIX))


def _reset_for_tests() -> None:
    """清空实例缓存。只给测试用。"""

    with _LOCK:
        _CACHE.clear()


__all__ = [
    "can_parse",
    "find_parser",
    "parse_bytes",
    "supported_suffixes",
]
