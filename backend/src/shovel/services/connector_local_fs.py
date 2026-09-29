#---------------------------------------------------------
# 本地文件系统连接器
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------
"""``local_fs``: 把本机上的一个目录当作数据源。

这是第一个真实连接器, 它的作用不只是"能扫本地目录", 更重要的是把
Connector 协议完整地走一遍 —— spec / config_model / verify / discover /
open 五件事都有真实实现, 后来的连接器可以照着抄。
"""

from __future__ import annotations

import fnmatch
import os
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path, PurePosixPath
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from shovel.domain.enums import DocClass, Modality, Sensitivity
from shovel.exceptions.resource import ConnectorCapabilityError, ConnectorPathEscapeError
from shovel.services.connector_base import (
    ConnectorCapability,
    ConnectorSpec,
    DiscoveredItem,
    DiscoveryScope,
    VerifyResult,
)
from shovel.services.connector_registry import register

#: 一次 read 的块大小。1 MiB 是个折中: 再小则 syscall 太频繁,
#: 再大则一个 PDF 就能在内存里顶出一个明显的尖峰。
_CHUNK_BYTES = 1024 * 1024

#: 扩展名 -> DocClass。只覆盖有把握的那些, 其余一律 UNKNOWN ——
#: 猜错文档类型会让下游选错解析器, 而"不知道"是可以被下游用嗅探
#: 兜底的, "猜错了"则不会有人去复查。
_EXT_TO_DOC_CLASS: dict[str, DocClass] = {
    ".txt": DocClass.PLAIN_TEXT,
    ".log": DocClass.PLAIN_TEXT,
    ".md": DocClass.MARKDOWN,
    ".markdown": DocClass.MARKDOWN,
    ".rst": DocClass.MARKDOWN,
    ".py": DocClass.CODE,
    ".pyi": DocClass.CODE,
    ".js": DocClass.CODE,
    ".ts": DocClass.CODE,
    ".tsx": DocClass.CODE,
    ".jsx": DocClass.CODE,
    ".go": DocClass.CODE,
    ".rs": DocClass.CODE,
    ".java": DocClass.CODE,
    ".c": DocClass.CODE,
    ".h": DocClass.CODE,
    ".cpp": DocClass.CODE,
    ".cs": DocClass.CODE,
    ".sh": DocClass.CODE,
    ".ps1": DocClass.CODE,
    ".sql": DocClass.CODE,
    ".toml": DocClass.CODE,
    ".yaml": DocClass.CODE,
    ".yml": DocClass.CODE,
    ".json": DocClass.CODE,
    ".pdf": DocClass.PDF_DOC,
    ".doc": DocClass.OFFICE_DOC,
    ".docx": DocClass.OFFICE_DOC,
    ".ppt": DocClass.OFFICE_DOC,
    ".pptx": DocClass.OFFICE_DOC,
    ".xls": DocClass.TABLE,
    ".xlsx": DocClass.TABLE,
    ".csv": DocClass.TABLE,
    ".tsv": DocClass.TABLE,
    ".htm": DocClass.HTML,
    ".html": DocClass.HTML,
    ".eml": DocClass.EMAIL,
    ".msg": DocClass.EMAIL,
    ".ics": DocClass.CALENDAR,
    ".png": DocClass.IMAGE,
    ".jpg": DocClass.IMAGE,
    ".jpeg": DocClass.IMAGE,
    ".gif": DocClass.IMAGE,
    ".bmp": DocClass.IMAGE,
    ".webp": DocClass.IMAGE,
    ".svg": DocClass.IMAGE,
}

#: DocClass -> Modality。绝大多数是 TEXT, 只有少数几类不是。
_DOC_CLASS_TO_MODALITY: dict[DocClass, Modality] = {
    DocClass.IMAGE: Modality.IMAGE,
    DocClass.TABLE: Modality.TABLE,
}

#: 无论用户怎么配 include, 都不该进知识库的目录。
#: 这不是"默认 exclude", 而是硬性跳过 —— 把 .git 或 node_modules 索引进去
#: 除了烧掉几个小时的 embedding 额度之外没有任何收益。
_ALWAYS_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn",
    "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache",
    "node_modules", ".venv", "venv", ".tox",
    ".next", ".turbo", "dist", "build", ".gradle",
    ".idea", ".vscode",
})


class LocalFsConfig(BaseModel):
    """``local_fs`` 的配置。

    ``extra="forbid"``: 用户把 ``root_path`` 拼成 ``rootpath`` 时, 应该立刻
    收到一条"多了个未知字段"的报错, 而不是得到一个静默使用默认值、
    扫了个空目录还显示成功的资源。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    root_path: Path = Field(
        description="要扫描的目录的绝对路径。",
    )

    follow_symlinks: bool = Field(
        default=False,
        description=(
            "是否跟随符号链接。默认关闭 —— 符号链接成环会让遍历永不终止, "
            "而指向家目录外的链接会让扫描范围悄悄超出用户的预期。"
        ),
    )

    include_hidden: bool = Field(
        default=False,
        description="是否包含以点开头的文件和目录。",
    )

    @field_validator("root_path")
    @classmethod
    def _expand_and_absolutize(cls, value: Path) -> Path:
        """把 ``~`` 展开并转成绝对路径。

        在校验阶段就做, 而不是等到 discover 时 —— 这样存进
        ``config_json`` 的就是一个确定的绝对路径, 之后不管进程的
        工作目录怎么变, 扫描的都是同一个地方。
        """

        return Path(value).expanduser().resolve()


@register
class LocalFsConnector:
    """扫描本机的一个目录。

    刻意不继承 ``Connector`` —— 那是个 Protocol, 只实现不继承。
    原因见 ``connector_base.Connector`` 的文档。
    """

    spec: ClassVar[ConnectorSpec] = ConnectorSpec(
        kind="local_fs",
        display_name="本地文件系统",
        description="把本机上的一个目录当作数据源, 按 glob 规则递归扫描其中的文件。",
        config_model=LocalFsConfig,
        capabilities=frozenset({
            ConnectorCapability.DISCOVER,
            # 文件系统给了 mtime, 增量只需比对水位线
            ConnectorCapability.INCREMENTAL,
            ConnectorCapability.RANDOM_READ,
            # 一次完整遍历就是权威的完整列表, 差集可以安全地推断删除
            ConnectorCapability.DELETE_DETECT,
        }),
        requires_identity=False,
        # 本地目录是用户自己放的东西, 可信度高于任何远端来源
        default_authority=0.8,
        # 家目录里的文件默认按"个人"对待, 而不是"普通"
        default_sensitivity=Sensitivity.PERSONAL,
    )

    def __init__(
        self,
        config: BaseModel,
        credential: SecretStr | None = None,
    ) -> None:
        if not isinstance(config, LocalFsConfig):
            raise TypeError(
                f"LocalFsConnector 需要 LocalFsConfig, 收到的是 {type(config).__name__}。"
            )

        # local_fs 不需要凭据 —— 能不能读由操作系统的文件权限决定,
        # 这里显式忽略传进来的 credential, 而不是假装用得上它。
        self._config = config

    @property
    def root(self) -> Path:
        return self._config.root_path

    # ----------------------------------------------------------------- #
    # verify
    # ----------------------------------------------------------------- #
    async def verify(self) -> VerifyResult:
        """检查目录存在、是目录、可读。

        顺带数一下顶层条目数。一个"存在且可读但空无一物"的目录, 技术上
        校验通过, 实际上几乎总是用户配错了路径 —— 把条目数放进 details,
        CLI 就能把这件事显示出来让用户自己判断。
        """

        started = time.perf_counter()
        root = self.root

        def elapsed() -> int:
            return int((time.perf_counter() - started) * 1000)

        if not root.exists():
            return VerifyResult.failure(
                f"路径不存在: {root}",
                details={"root_path": str(root)},
                latency_ms=elapsed(),
            )

        if not root.is_dir():
            return VerifyResult.failure(
                f"路径不是目录: {root}",
                details={"root_path": str(root)},
                latency_ms=elapsed(),
            )

        try:
            top_level = 0
            with os.scandir(root) as entries:
                for _ in entries:
                    top_level += 1
        except PermissionError as exc:
            return VerifyResult.failure(
                f"没有读取权限: {root}",
                details={"root_path": str(root), "errno": exc.errno},
                latency_ms=elapsed(),
            )
        except OSError as exc:
            return VerifyResult.failure(
                f"无法读取目录 {root}: {exc.strerror or exc}",
                details={"root_path": str(root), "errno": exc.errno},
                latency_ms=elapsed(),
            )

        if top_level == 0:
            return VerifyResult.success(
                f"目录可读, 但它是空的: {root}",
                details={"root_path": str(root), "top_level_entries": 0},
                latency_ms=elapsed(),
            )

        return VerifyResult.success(
            f"目录可读, 顶层有 {top_level} 个条目: {root}",
            details={"root_path": str(root), "top_level_entries": top_level},
            latency_ms=elapsed(),
        )

    # ----------------------------------------------------------------- #
    # discover
    # ----------------------------------------------------------------- #
    async def discover(self, scope: DiscoveryScope) -> AsyncIterator[DiscoveredItem]:
        """递归列举文件。

        实现是同步的 ``os.walk``, 但签名是 async generator。没有把它丢进
        线程池, 是刻意的: 目录遍历是 syscall 密集而非 CPU 密集, 丢线程池
        换不来吞吐, 反而会让"politeness budget"(ScanProfile 里的 cpu_budget /
        max_concurrency) 失去着力点 —— 真正需要限流的是下游的解析和
        embedding, 不是这里。
        """

        cutoff = self._time_cutoff(scope)
        targets = set(scope.target_ids) or None

        for path, external_id in self._walk():
            if targets is not None and external_id not in targets:
                continue

            if not _matches(external_id, scope.include, scope.exclude):
                continue

            try:
                stat = path.stat()
            except OSError:
                # 遍历期间文件被删/被移走是常态, 不是错误。
                # 少一个条目由下一次全量扫描的差集去对账。
                continue

            if scope.max_file_bytes is not None and stat.st_size > scope.max_file_bytes:
                continue

            modified_at = int(stat.st_mtime)

            if cutoff is not None and modified_at < cutoff:
                continue

            if scope.since is not None and modified_at <= scope.since:
                continue

            doc_class = _classify(path)

            yield DiscoveredItem(
                external_id=external_id,
                uri=path.as_uri(),
                title=path.name,
                size_bytes=stat.st_size,
                modified_at=modified_at,
                content_hash=_cheap_fingerprint(stat.st_size, stat.st_mtime_ns),
                doc_class=doc_class,
                modality=_DOC_CLASS_TO_MODALITY.get(doc_class, Modality.TEXT),
                extra={"suffix": path.suffix.lower()},
            )

    # ----------------------------------------------------------------- #
    # open
    # ----------------------------------------------------------------- #
    async def open(self, item: DiscoveredItem) -> AsyncIterator[bytes]:
        """按块读出一个条目的内容。

        用 ``external_id`` 重新拼路径, 而不是用 ``item.uri``:
        uri 是给人看的, external_id 才是权威标识。而且重新拼接时会做一次
        越界检查 —— 一个构造出来的 ``../../etc/passwd`` 不该能读到根目录外面。
        """

        if not self.spec.supports(ConnectorCapability.RANDOM_READ):
            raise ConnectorCapabilityError(self.spec.kind, "random_read")

        path = self._resolve(item.external_id)

        with path.open("rb") as handle:
            while True:
                block = handle.read(_CHUNK_BYTES)
                if not block:
                    break
                yield block

    # ----------------------------------------------------------------- #
    # internals
    # ----------------------------------------------------------------- #
    def _resolve(self, external_id: str) -> Path:
        """external_id -> 绝对路径, 并确保结果仍在 root 之内。"""

        candidate = (self.root / PurePosixPath(external_id)).resolve()

        # 一个构造出来的 "../../../etc/passwd" 不该能读到 root 外面去。
        # external_id 最终会来自数据库, 而数据库里的值未必全是本连接器
        # 自己写进去的 —— 边界检查放在读之前, 而不是信任调用方。
        if not candidate.is_relative_to(self.root):
            raise ConnectorPathEscapeError(self.spec.kind, external_id, str(self.root))

        return candidate

    def _walk(self) -> Iterator[tuple[Path, str]]:
        """产出 (绝对路径, external_id) 二元组。

        ``external_id`` 用的是**相对 root 的 POSIX 路径**, 不是绝对路径。
        这个选择很关键: 用户把 ``D:\\notes`` 改名成 ``D:\\my-notes`` 之后,
        如果 external_id 是绝对路径, 整个资源的每一个文档都会被认成
        "旧的全没了 + 新的全来了", 于是全量重新 embedding 一遍。
        用相对路径则改名只影响 ``Resource.config``, 文档一个都不用动。

        统一用 POSIX 分隔符, 是为了让同一个知识库在 Windows 和 Linux 上
        算出来的 id 一致 —— 否则一次跨平台同步就等于一次全量重扫。
        """

        root = self.root
        include_hidden = self._config.include_hidden
        follow = self._config.follow_symlinks

        for dirpath, dirnames, filenames in os.walk(root, followlinks=follow):
            # 原地改 dirnames 才能真正阻止 os.walk 进去,
            # 换成新 list 赋值是没用的 —— walk 持有的是同一个对象。
            dirnames[:] = [
                d for d in dirnames
                if d not in _ALWAYS_SKIP_DIRS
                and (include_hidden or not d.startswith("."))
            ]

            current = Path(dirpath)

            for name in filenames:
                if not include_hidden and name.startswith("."):
                    continue

                path = current / name

                try:
                    relative = path.relative_to(root)
                except ValueError:
                    # followlinks=True 时可能走出 root 之外
                    continue

                yield path, relative.as_posix()

    @staticmethod
    def _time_cutoff(scope: DiscoveryScope) -> int | None:
        if scope.time_window_days is None:
            return None
        return int(time.time()) - scope.time_window_days * 86400


def _classify(path: Path) -> DocClass:
    return _EXT_TO_DOC_CLASS.get(path.suffix.lower(), DocClass.UNKNOWN)


def _cheap_fingerprint(size: int, mtime_ns: int) -> str:
    """``(size, mtime_ns)`` 组成的廉价内容指纹。

    刻意**不**算真正的哈希。理由是成本: 真哈希要求把每个文件完整读一遍,
    而 discover 的全部意义就在于"不读内容也能知道有哪些东西、哪些变了"。
    在一个几十 GB 的目录上, 读盘算哈希会让一次增量扫描退化成全量 IO。

    代价是存在漏报: 一个文件被改成同样大小、且 mtime 被刻意改回去,
    这里会认为它没变。这个场景在个人知识库里可以忽略, 而真正需要
    强校验的场合(例如 parse 阶段)可以在读内容时顺手算真哈希。

    前缀 ``st:`` 标明"这是 stat 指纹, 不是内容哈希", 免得下游把它
    误当成 sha256 去比对。
    """

    return f"st:{size:x}-{mtime_ns:x}"


def _matches(
    external_id: str,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
) -> bool:
    """glob 过滤。exclude 优先于 include。

    ``include`` 为空表示"全要"。这和 ``.gitignore`` 的直觉一致:
    用户只写 exclude 时, 期望的是"除了这些以外都扫", 而不是"什么都不扫"。
    """

    if any(_glob(external_id, pattern) for pattern in exclude):
        return False

    if not include:
        return True

    return any(_glob(external_id, pattern) for pattern in include)


def _glob(external_id: str, pattern: str) -> bool:
    """匹配一个 glob 模式。

    ``fnmatch`` 的 ``*`` 会跨越 ``/``, 这一点和 shell 不同, 但对这里是
    想要的行为: 用户写 ``*.md`` 时, 期望的几乎一定是"所有层级的 md",
    而不是"只有根目录下的 md"。想限定层级的人会写 ``docs/*.md``。

    额外支持"目录前缀"写法: 模式以 ``/`` 结尾时, 匹配该目录下的一切。
    """

    if pattern.endswith("/"):
        return external_id.startswith(pattern)

    return fnmatch.fnmatch(external_id, pattern)
