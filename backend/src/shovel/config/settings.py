#---------------------------------------------------------
# 应用配置
#
# 日期: 2026-09-15
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------
from __future__ import annotations

import os
import shutil
import tomllib
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

import tomli_w
from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from .database_config import DatabaseSettings
from .document_settings import DocumentSettings
from .embedding_config import EmbeddingSettings
from .errors import (
    ConfigError,
    format_toml_error,
    format_validation_error,
)
from .memory_config import MemorySettings
from .model_settings import DefaultAgentModelSettings
from .zvec_config import ZvecSettings

# ================================================================
# TOML 值转换
# ================================================================

def _to_toml_safe(value: Any) -> Any:
    """把 model_dump() 的结果转换成 TOML 能表达的形式。

    1. None -> 省略该键
       TOML 没有 null, "未设置"的正确表达是键不存在,
       回读时由 pydantic 的字段默认值兜底。

       ⚠️ 正因为 None 会让键从生成的配置文件里消失, 配置模型里的
       "可空"字段一律用空串 / 空 SecretStr 表达未设置 —— 否则
       'shovel init' 会吐出一堆空表, 用户看不到有哪些键可以填。

    2. SecretStr -> 真实值
       model_dump(mode="json") 会输出 "**********",
       直接写盘等于用掩码覆盖掉用户真实的密钥。

    3. Enum -> 成员值
       mode="python" 拿到的是枚举对象本身, tomli_w 不认识它。

    4. Path -> posix 字符串
    """

    if value is None:
        return None

    if isinstance(value, SecretStr):
        return value.get_secret_value()

    if isinstance(value, Enum):
        return _to_toml_safe(value.value)

    if isinstance(value, Path):
        return value.as_posix()

    if isinstance(value, dict):
        return {
            k: cleaned
            for k, v in value.items()
            if (cleaned := _to_toml_safe(v)) is not None
        }

    if isinstance(value, (list, tuple)):
        return [
            cleaned
            for v in value
            if (cleaned := _to_toml_safe(v)) is not None
        ]

    return value


# ================================================================
# 旧配置项的迁移
# ================================================================

#: 已经被移除的配置项: {表名(None 表示顶层): {键名}}。
#:
#: 存在的理由是 extra="forbid": 它让配置文件里的错别字能被报出来, 代价是
#: 昨天还合法的键今天会让 'shovel init' 直接失败。配置文件归用户所有,
#: 不能因为我们改了实现就要求用户手工删几行。
#:
#: 这里只丢弃"我们自己曾经写进去过"的键, 因此不会掩盖真正的错别字。
_REMOVED_KEYS: dict[str | None, set[str]] = {
    # 记忆改由 mem0 托管, qdrant 的配置搬到了 [memory.qdrant];
    # redis 整个从项目里移除了, 老配置文件里的 [redis] 段直接丢弃
    None: {"qdrant", "redis"},
    # Zvec 只负责知识库了, 记忆的 collection 不再由它管
    "zvec": {"memory_path", "memory_collection"},
}


def drop_removed_keys(data: dict[str, Any]) -> list[str]:
    """就地删除已废弃的配置项, 返回被删掉的键名(用于提示用户)。"""

    dropped: list[str] = []

    for table, keys in _REMOVED_KEYS.items():
        section = data if table is None else data.get(table)

        if not isinstance(section, dict):
            continue

        for key in keys:
            if key in section:
                del section[key]
                dropped.append(key if table is None else f"{table}.{key}")

    return dropped


# ================================================================
# AppSettings
# ================================================================

class AppSettings(BaseSettings):

    model_config = SettingsConfigDict(
        env_prefix="SHOVEL_",
        env_nested_delimiter="__",     # SHOVEL_DATABASE__URL
        extra="forbid",                # 配置文件里的错别字会被报出来
    )

    service_name: str = "shovel"
    agent_host: str = "127.0.0.1"
    agent_port: int = 19566
    http_host: str = "127.0.0.1"
    dev_http_host: str = "127.0.0.1"
    dev_http_port: int = 9999
    http_port: int = 8080
    log_level: str = "INFO"

    # default_factory: 推迟到实例化时才构造, 避免导入期副作用
    document: DocumentSettings = Field(default_factory=DocumentSettings)
    model: DefaultAgentModelSettings = Field(default_factory=DefaultAgentModelSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)

    #: 知识库向量存储(Zvec)
    zvec: ZvecSettings = Field(default_factory=ZvecSettings)

    #: 知识库的 embedding 服务。与 [memory.embedder] 是两套, 原因见
    #: config/embedding_config.py —— 两个向量库的维度各自独立。
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)

    #: 记忆子系统(mem0 + 本地 Qdrant)。记忆不走 Zvec, 原因见 memory_config.py
    memory: MemorySettings = Field(default_factory=MemorySettings)

    # ------------------------------------------------------------
    # 读
    # ------------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> AppSettings:
        """从 TOML 文件载入配置(不叠加环境变量)。

        需要环境变量覆盖时请用模块级的 load_settings()。
        """

        data = read_toml_file(path)
        drop_removed_keys(data)

        try:
            return cls.model_validate(data)
        except ValidationError as e:
            raise ConfigError(format_validation_error(path, e)) from e

    # ------------------------------------------------------------
    # 写
    # ------------------------------------------------------------

    def save(
            self,
            path: Path,
            *,
            overwrite: bool = False,
            backup: bool = True,
    ) -> Path | None:
        """写入 TOML 配置文件。

        ⚠️ 本方法会丢失用户写在配置文件里的注释与自定义格式。
        因此默认拒绝覆盖已存在的文件 —— 配置文件归用户所有,
        程序只应在用户明确要求时才改写它。

        :param overwrite: 允许覆盖已存在的文件
        :param backup:    覆盖前先备份(仅 overwrite=True 时生效)
        :return:          若产生了备份, 返回备份文件路径; 否则 None
        :raises ConfigError: 文件已存在且 overwrite=False
        """

        if path.exists() and not overwrite:
            raise ConfigError(
                f"配置文件已存在: {path}\n"
                f"  为避免覆盖你写在其中的注释, 默认不会自动改写。\n"
                f"  如需重新生成, 请使用 'shovel config --force'。"
            )

        backup_path: Path | None = None
        if path.exists() and backup:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_path = path.with_suffix(f"{path.suffix}.{stamp}.bak")
            shutil.copy2(path, backup_path)

        # mode="python" 而非 "json": 保留 SecretStr 对象,
        # 由 _to_toml_safe 显式取真实值
        data = _to_toml_safe(self.model_dump(mode="python"))

        _atomic_write_toml(path, data)

        return backup_path


# ================================================================
# 写入辅助
# ================================================================

def _atomic_write_toml(path: Path, data: dict[str, Any]) -> None:
    """原子写 + 写后回读自检。

    自检的意义: TOML 中 [table] 之后的键都归属该 table, 因此标量
    必须排在所有 table 之前。tomli_w 会正确处理, 但回读比对能确保
    任何序列化偏差都在落盘前被拦下, 用户的原文件不受影响。
    """

    path.parent.mkdir(parents=True, exist_ok=True)

    tmp = path.with_suffix(path.suffix + ".tmp")

    try:
        with tmp.open("wb") as f:
            tomli_w.dump(data, f)

        with tmp.open("rb") as f:
            parsed = tomllib.load(f)

        if parsed != data:
            raise ConfigError(
                f"生成的 TOML 与预期不一致, 已中止写入: {path}\n"
                f"  这是配置序列化的内部错误, 请反馈该问题。"
            )

        os.replace(tmp, path)

    finally:
        tmp.unlink(missing_ok=True)


# ================================================================
# 模块级读取入口
# ================================================================

def read_toml_file(path: Path) -> dict[str, Any]:
    """读取并解析 TOML, 失败时抛出面向用户的 ConfigError。"""

    try:
        text = path.read_text(encoding="utf-8")

    except FileNotFoundError:
        raise ConfigError(
            f"配置文件不存在: {path}\n"
            f"  执行 'shovel config' 生成默认配置。"
        ) from None

    except PermissionError:
        raise ConfigError(f"没有权限读取配置文件: {path}") from None

    if not text.strip():
        raise ConfigError(
            f"配置文件为空: {path}\n"
            f"  执行 'shovel config --force' 重新生成。"
        )

    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(format_toml_error(path, text, e)) from e


def load_settings(config_path: Path | str) -> AppSettings:
    """从指定 TOML 文件加载 Shovel 配置, 并叠加环境变量。

    不使用 TomlConfigSettingsSource: 它在 pydantic 内部读文件,
    TOMLDecodeError 会从库深处抛出, 拿不到文件名和出错位置,
    最终用户只能看到一句 "Invalid value (at line 28, column 17)"。
    """

    path = Path(config_path).expanduser().resolve()

    data = read_toml_file(path)
    drop_removed_keys(data)

    try:
        return AppSettings(**data)
    except ValidationError as e:
        raise ConfigError(format_validation_error(path, e)) from e
