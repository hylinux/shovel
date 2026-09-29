"""local_fs 连接器的测试。

这个连接器是协议的参考实现, 所以测试覆盖的重点不只是"能不能列出文件",
更是那几个决定了知识库会不会被全量重扫的细节:
external_id 的稳定性、glob 语义、以及 discover 不读内容。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from shovel.domain.enums import DocClass, Modality
from shovel.exceptions.resource import ConnectorPathEscapeError
from shovel.services.connector_base import DiscoveryScope
from shovel.services.connector_local_fs import LocalFsConfig, LocalFsConnector


def _connector(root: Path, **kw: object) -> LocalFsConnector:
    return LocalFsConnector(LocalFsConfig(root_path=root, **kw))


async def _ids(connector: LocalFsConnector, scope: DiscoveryScope) -> set[str]:
    return {item.external_id async for item in connector.discover(scope)}


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / "node_modules").mkdir()

    (tmp_path / "readme.md").write_text("# hi", encoding="utf-8")
    (tmp_path / "docs" / "guide.md").write_text("guide", encoding="utf-8")
    (tmp_path / "docs" / "sheet.csv").write_text("a,b", encoding="utf-8")
    (tmp_path / "src" / "main.py").write_text("print()", encoding="utf-8")
    (tmp_path / ".hidden.md").write_text("secret", encoding="utf-8")
    (tmp_path / ".git" / "config").write_text("[core]", encoding="utf-8")
    (tmp_path / "node_modules" / "pkg.js").write_text("x", encoding="utf-8")

    return tmp_path


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def test_root_path_is_absolutised_at_validation_time(tmp_path: Path) -> None:
    """存进 config_json 的必须是绝对路径, 否则进程的 cwd 一变就扫错地方。"""

    config = LocalFsConfig(root_path=tmp_path)
    assert config.root_path.is_absolute()


def test_unknown_config_field_is_rejected(tmp_path: Path) -> None:
    """拼错字段名该立刻报错, 而不是静默用默认值扫出一个空资源。"""

    with pytest.raises(ValidationError):
        LocalFsConfig(rootpath=str(tmp_path))  # type: ignore[call-arg]


def test_config_is_frozen(tmp_path: Path) -> None:
    config = LocalFsConfig(root_path=tmp_path)
    with pytest.raises(ValidationError):
        config.root_path = tmp_path  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #
async def test_verify_succeeds_on_readable_dir(tree: Path) -> None:
    result = await _connector(tree).verify()

    assert result.ok
    assert result.details["top_level_entries"] > 0
    assert result.latency_ms is not None


async def test_verify_reports_missing_path(tmp_path: Path) -> None:
    result = await _connector(tmp_path / "nope").verify()

    assert not result.ok
    assert "不存在" in result.message


async def test_verify_rejects_a_file(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")

    result = await _connector(target).verify()

    assert not result.ok
    assert "不是目录" in result.message


async def test_verify_flags_empty_dir(tmp_path: Path) -> None:
    """空目录技术上通过, 但几乎总是路径配错了 —— 要让用户看见。"""

    result = await _connector(tmp_path).verify()

    assert result.ok
    assert result.details["top_level_entries"] == 0
    assert "空的" in result.message


# --------------------------------------------------------------------------- #
# discover
# --------------------------------------------------------------------------- #
async def test_discover_lists_files_recursively(tree: Path) -> None:
    ids = await _ids(_connector(tree), DiscoveryScope())

    assert ids == {"readme.md", "docs/guide.md", "docs/sheet.csv", "src/main.py"}


async def test_noise_dirs_are_always_skipped(tree: Path) -> None:
    """把 .git / node_modules 索引进去, 除了烧掉 embedding 额度没有收益。"""

    ids = await _ids(_connector(tree), DiscoveryScope())

    assert not any(i.startswith((".git/", "node_modules/")) for i in ids)


async def test_hidden_files_excluded_by_default(tree: Path) -> None:
    assert ".hidden.md" not in await _ids(_connector(tree), DiscoveryScope())


async def test_hidden_files_can_be_opted_in(tree: Path) -> None:
    ids = await _ids(_connector(tree, include_hidden=True), DiscoveryScope())
    assert ".hidden.md" in ids


async def test_external_id_is_relative_to_root(tree: Path) -> None:
    """这是整个连接器最关键的一条约定。

    如果 external_id 用绝对路径, 用户把目录改个名, 每一个文档都会被认成
    '旧的全没了 + 新的全来了', 于是全量重新 embedding 一遍。
    """

    ids = await _ids(_connector(tree), DiscoveryScope())

    assert all(not Path(i).is_absolute() for i in ids)
    # 统一用 POSIX 分隔符, 否则同一个知识库跨平台同步就等于一次全量重扫
    assert all("\\" not in i for i in ids)
    assert "docs/guide.md" in ids


async def test_include_filters(tree: Path) -> None:
    ids = await _ids(_connector(tree), DiscoveryScope(include=("*.md",)))
    assert ids == {"readme.md", "docs/guide.md"}


async def test_exclude_beats_include(tree: Path) -> None:
    ids = await _ids(
        _connector(tree),
        DiscoveryScope(include=("*.md",), exclude=("docs/*",)),
    )
    assert ids == {"readme.md"}


async def test_empty_include_means_everything(tree: Path) -> None:
    """和 .gitignore 的直觉一致: 只写 exclude 时期望的是'除此之外都扫'。"""

    ids = await _ids(_connector(tree), DiscoveryScope(exclude=("*.csv",)))

    assert "readme.md" in ids
    assert "docs/sheet.csv" not in ids


async def test_directory_prefix_pattern(tree: Path) -> None:
    ids = await _ids(_connector(tree), DiscoveryScope(include=("docs/",)))
    assert ids == {"docs/guide.md", "docs/sheet.csv"}


async def test_max_file_bytes_filters(tree: Path) -> None:
    (tree / "big.md").write_text("x" * 5000, encoding="utf-8")

    ids = await _ids(_connector(tree), DiscoveryScope(max_file_bytes=100))

    assert "big.md" not in ids
    assert "readme.md" in ids


async def test_time_window_filters_old_files(tree: Path) -> None:
    old = tree / "ancient.md"
    old.write_text("old", encoding="utf-8")

    long_ago = time.time() - 60 * 86400
    import os

    os.utime(old, (long_ago, long_ago))

    ids = await _ids(_connector(tree), DiscoveryScope(time_window_days=7))

    assert "ancient.md" not in ids
    assert "readme.md" in ids


async def test_since_watermark_is_exclusive(tree: Path) -> None:
    """增量扫描的水位线: 只要严格晚于它的, 否则每次都会重复处理边界那一批。"""

    now = int(time.time())

    assert await _ids(_connector(tree), DiscoveryScope(since=now + 10)) == set()
    assert await _ids(_connector(tree), DiscoveryScope(since=now - 3600)) != set()


async def test_target_ids_narrow_the_scan(tree: Path) -> None:
    ids = await _ids(
        _connector(tree),
        DiscoveryScope(target_ids=("readme.md", "src/main.py")),
    )
    assert ids == {"readme.md", "src/main.py"}


async def test_doc_class_and_modality_are_classified(tree: Path) -> None:
    items = {i.external_id: i async for i in _connector(tree).discover(DiscoveryScope())}

    assert items["readme.md"].doc_class is DocClass.MARKDOWN
    assert items["readme.md"].modality is Modality.TEXT
    assert items["src/main.py"].doc_class is DocClass.CODE
    assert items["docs/sheet.csv"].doc_class is DocClass.TABLE
    assert items["docs/sheet.csv"].modality is Modality.TABLE


async def test_unknown_extension_is_not_guessed(tree: Path) -> None:
    """猜错文档类型会让下游选错解析器, 而'不知道'可以被嗅探兜底。"""

    (tree / "mystery.xyz").write_text("?", encoding="utf-8")

    items = {i.external_id: i async for i in _connector(tree).discover(DiscoveryScope())}
    assert items["mystery.xyz"].doc_class is DocClass.UNKNOWN


async def test_fingerprint_changes_with_content(tree: Path) -> None:
    target = tree / "readme.md"

    async def fingerprint() -> str | None:
        async for item in _connector(tree).discover(DiscoveryScope(include=("readme.md",))):
            return item.content_hash
        return None

    before = await fingerprint()
    time.sleep(0.01)
    target.write_text("# hi, much longer now", encoding="utf-8")
    after = await fingerprint()

    assert before != after
    # 前缀标明这是 stat 指纹而非内容哈希, 免得下游拿它当 sha256 去比
    assert str(after).startswith("st:")


async def test_discover_does_not_read_content(tree: Path) -> None:
    """discover 的全部意义在于'不读内容也能知道有哪些东西、哪些变了'。"""

    async for item in _connector(tree).discover(DiscoveryScope()):
        assert not hasattr(item, "content")
        assert item.size_bytes is not None


# --------------------------------------------------------------------------- #
# open
# --------------------------------------------------------------------------- #
async def test_open_streams_bytes(tree: Path) -> None:
    connector = _connector(tree)

    async for item in connector.discover(DiscoveryScope(include=("readme.md",))):
        blocks = [block async for block in connector.open(item)]
        assert b"".join(blocks) == b"# hi"


async def test_open_refuses_to_escape_the_root(tree: Path) -> None:
    """构造出来的 ../../ 不该能读到资源根目录外面。"""

    from shovel.services.connector_base import DiscoveredItem

    connector = _connector(tree)
    evil = DiscoveredItem(external_id="../../../etc/passwd", uri="file:///x")

    with pytest.raises(ConnectorPathEscapeError):
        [block async for block in connector.open(evil)]
