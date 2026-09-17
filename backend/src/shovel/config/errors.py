#---------------------------------------------------------
# 配置载入的错误处理
#
# 目标: 把库内部抛出的技术性异常, 翻译成用户能直接照做的提示。
#---------------------------------------------------------
from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic import ValidationError


class ConfigError(Exception):
    """配置载入失败。

    message 已经是面向最终用户的可读文本, CLI 捕获后直接打印即可,
    不需要再展示 traceback (配置错误是用户错误, 不是程序缺陷)。
    """


# ================================================================
# 代码片段渲染
# ================================================================

def render_snippet(
        text: str,
        lineno: int,
        colno: int,
        context: int = 2,
) -> str:
    """渲染出错位置前后若干行, 并用 ^ 指向具体列。

    :param lineno: 1-based 行号
    :param colno:  1-based 列号
    """

    lines = text.splitlines()

    if not lines or lineno < 1 or lineno > len(lines):
        return ""

    start = max(1, lineno - context)
    end = min(len(lines), lineno + context)
    width = len(str(end))

    out: list[str] = []

    for i in range(start, end + 1):
        raw = lines[i - 1].replace("\t", "    ")
        marker = ">" if i == lineno else " "
        out.append(f"  {marker} {i:>{width}} | {raw}")

        if i == lineno:
            # tab 被展开成 4 个空格, 插入符位置要跟着偏移
            tab_fix = lines[i - 1][: max(0, colno - 1)].count("\t") * 3
            pad = " " * max(0, colno - 1 + tab_fix)
            out.append(f"    {' ' * width} | {pad}^")

    return "\n".join(out)


# ================================================================
# TOML 语法错误
# ================================================================

_TOML_POS = re.compile(r"\(at line (\d+), column (\d+)\)")
_TOML_EOF = re.compile(r"\(at end of document\)")

_TOML_HINTS: tuple[tuple[str, str], ...] = (
    (
        "Invalid value",
        "该位置的值无效。注意 TOML 没有 null —— 想表示\"未设置\"请直接删掉这一行; "
        "字符串必须用双引号包裹; 布尔值是小写的 true / false。",
    ),
    (
        "Expected '=' after a key",
        "键后面缺少 '='。TOML 的写法是 key = value。",
    ),
    (
        "Invalid statement",
        "该行不是合法的 TOML 语句。检查是否误用了 JSON 的 { } / : 语法, "
        "或表头写成了 [name] 以外的形式。",
    ),
    (
        "Cannot overwrite a value",
        "同一个键被定义了多次, 或同名 [table] 重复出现。",
    ),
    (
        "Cannot declare",
        "该表已经定义过了, 不能重复声明。",
    ),
    (
        "Unclosed",
        "字符串或数组没有正确闭合。",
    ),
    (
        "Invalid hex value",
        "字符串里有非法的转义序列。反斜杠在 TOML 字符串中是转义符 —— "
        "Windows 路径请写成 C:\\\\Users\\\\... 或 C:/Users/... , "
        "也可以改用单引号字面量 'C:\\Users\\...' (单引号内不做转义)。",
    ),
    (
        "Invalid escape",
        "字符串里有非法的转义序列。反斜杠请写成 \\\\ , "
        "或改用单引号字面量 (单引号内不做转义)。",
    ),
    (
        "Unescaped",
        "字符串中包含需要转义的字符。Windows 路径请写成 C:\\\\path 或 C:/path。",
    ),
    (
        "Illegal character",
        "出现了不该在此处出现的字符。最常见的原因是字符串缺少收尾的双引号。",
    ),
    (
        "Expected newline",
        "一行只能有一个键值对, 请检查是否漏了换行。",
    ),
)


def _toml_hint(msg: str) -> str:
    for key, hint in _TOML_HINTS:
        if key in msg:
            return hint
    return "请检查该位置的 TOML 语法。"


def format_toml_error(
        path: Path,
        text: str,
        exc: tomllib.TOMLDecodeError,
) -> str:
    """把 TOMLDecodeError 格式化成带位置和片段的可读文本。"""

    # Python 3.14+ 的 TOMLDecodeError 带 lineno / colno / msg 属性;
    # 3.13 及更早只能从消息文本里解析
    lineno: int | None = getattr(exc, "lineno", None)
    colno: int | None = getattr(exc, "colno", None)
    msg: str = getattr(exc, "msg", None) or str(exc)

    if lineno is None:
        raw = str(exc)
        m = _TOML_POS.search(raw)

        if m:
            lineno, colno = int(m.group(1)), int(m.group(2))
            msg = _TOML_POS.sub("", raw).strip()

        elif _TOML_EOF.search(raw):
            lines = text.splitlines()
            lineno = len(lines) or 1
            colno = len(lines[-1]) + 1 if lines else 1
            msg = _TOML_EOF.sub("", raw).strip() or "文档意外结束"

    head = f"配置文件不是合法的 TOML: {path}"

    if lineno is None:
        return f"{head}\n  {msg}"

    snippet = render_snippet(text, lineno, colno or 1)
    body = f"\n\n{snippet}" if snippet else ""

    return (
        f"{head}\n"
        f"  第 {lineno} 行, 第 {colno} 列: {msg}"
        f"{body}\n\n"
        f"  提示: {_toml_hint(msg)}\n"
        f"  可执行 'shovel config --force' 重新生成一份合法的默认配置。"
    )


# ================================================================
# pydantic 字段校验错误
# ================================================================

_VALIDATION_HINTS: dict[str, str] = {
    "missing": "该配置项为必填, 但文件中缺失。",
    "extra_forbidden": "配置文件中存在未知的配置项。请检查是否拼写错误, 或删除该项。",
    "int_parsing": "期望一个整数, 不要加引号。",
    "float_parsing": "期望一个小数, 不要加引号。",
    "bool_parsing": "期望布尔值。TOML 中写作小写的 true 或 false, 不加引号。",
    "string_type": "期望字符串, 需要用双引号包裹。",
    "url_parsing": "URL 格式不正确。",
    "greater_than": "取值过小。",
    "less_than": "取值过大。",
}


def format_validation_error(
        path: Path,
        exc: ValidationError,
) -> str:
    """把 pydantic 的 ValidationError 逐字段展开成可读文本。"""

    errors = exc.errors()

    lines = [
        f"配置文件校验失败: {path}",
        f"  共 {len(errors)} 处问题:",
        "",
    ]

    for err in errors:
        # loc 是嵌套路径, 如 ("database", "echo")
        loc = ".".join(str(x) for x in err["loc"]) or "<顶层>"

        lines.append(f"  • {loc}")
        lines.append(f"      {err['msg']}")

        hint = _VALIDATION_HINTS.get(err["type"])
        if hint:
            lines.append(f"      {hint}")

        if err["type"] != "missing" and "input" in err:
            lines.append(f"      当前值: {err['input']!r}")

        lines.append("")

    lines.append("  可执行 'shovel config --check' 重新校验, "
                 "或 'shovel config --force' 生成一份合法的默认配置。")

    return "\n".join(lines)
