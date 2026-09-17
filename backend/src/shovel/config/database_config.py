#---------------------------------------------------------------------
# 数据库配置
#
# 日期: 2026-09-15
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------------------
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, model_validator


class DatabaseSettings(BaseModel):
    """数据库配置。

    继承 BaseModel 而非 BaseSettings:
    环境变量由父级 AppSettings 通过 env_nested_delimiter 统一处理
    (SHOVEL_DATABASE__URL -> database.url)。
    嵌套的 BaseSettings 会自带一套无前缀的环境读取, 导致裸 URL /
    ECHO 变量意外生效, 且行为随"配置文件里有没有这一节"而变化。
    """

    # 不再是 str | None:
    # 1) TOML 没有 null, None 无法表达, 只能靠"省略该键"
    # 2) 空值由下面的 validator 补全, 外部拿到的一定是可用 URL
    # 3) 类型收紧后 Pylance 能守住 create_async_engine(url)
    url: str = ""

    echo: bool = False

    @model_validator(mode="after")
    def _resolve_default_url(self) -> DatabaseSettings:
        """未配置时回落到用户目录下的 SQLite。"""

        if not self.url:
            object.__setattr__(
                self,
                "url",
                f"sqlite+aiosqlite:///{_default_database_path().as_posix()}",
            )

        return self


def _default_database_path() -> Path:
    path = Path.home() / ".shovel" / "data" / "shovel.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path

