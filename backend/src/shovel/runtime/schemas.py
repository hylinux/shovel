from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from shovel.domain.enums import ScanMode, Sensitivity

SearchMode = Literal["auto", "keyword", "vector", "hybrid"]


def _validate_config_secrets(value: dict[str, Any]) -> dict[str, Any]:
    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                normalized = str(key).lower()
                if child and (
                    normalized in {"secret", "password", "token", "api_key", "security_key", "authorization"}
                    or normalized.endswith(("_password", "_secret", "_token", "_api_key"))
                ):
                    raise ValueError("连接配置不能包含密钥;请使用 identity_ref 引用系统凭据库或环境变量。")
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return value


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class MCPConfigurationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(max_length=1_048_576)
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class ResourceInput(InputModel):
    name: str = Field(min_length=1, max_length=200)
    connector_kind: str = Field(min_length=1, max_length=100)
    config: dict[str, Any]
    description: str | None = Field(default=None, max_length=4000)
    sensitivity: Sensitivity | None = None
    authority: float | None = Field(default=None, ge=0, le=1)
    identity_ref: str | None = None
    verify: bool = True
    scan: bool = False
    embed: bool = True

    @field_validator("config")
    @classmethod
    def no_plaintext_credentials(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_config_secrets(value)

    @model_validator(mode="after")
    def validate_scan(self) -> ResourceInput:
        if self.scan and not self.verify:
            raise ValueError("首次扫描需要先验证资源。")
        return self


class ScanInput(InputModel):
    mode: ScanMode | None = None
    profile_name: str | None = Field(default=None, max_length=200)
    target_ids: list[str] = Field(default_factory=list, max_length=1000)
    limit: int | None = Field(default=None, ge=1)
    embed: bool = True
    force: bool = False

    @model_validator(mode="after")
    def validate_targets(self) -> ScanInput:
        if self.mode == ScanMode.TARGETED and not self.target_ids:
            raise ValueError("targeted 扫描必须指定 target_ids。")
        return self


class ReasonInput(InputModel):
    reason: str | None = Field(default=None, max_length=2000)


class ResourceUpdate(InputModel):
    config: dict[str, Any] | None = None
    description: str | None = Field(default=None, max_length=4000)
    sensitivity: Sensitivity | None = None
    authority: float | None = Field(default=None, ge=0, le=1)

    @field_validator("config")
    @classmethod
    def no_plaintext_credentials(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _validate_config_secrets(value) if value is not None else None


class CleanupInput(InputModel):
    resource_id: str | None = None


class ChunkerInput(InputModel):
    chunk_chars: int = Field(default=800, ge=1, le=1_000_000)
    overlap_chars: int = Field(default=120, ge=0)
    parent_chars: int = Field(default=3200, ge=1, le=4_000_000)
    min_chunk_chars: int = Field(default=120, ge=1)
    max_chunk_chars: int = Field(default=2400, ge=1, le=4_000_000)

    @model_validator(mode="after")
    def valid_budgets(self) -> ChunkerInput:
        from shovel.pipeline.chunker import ChunkerConfig

        ChunkerConfig(**self.model_dump())
        if self.min_chunk_chars > self.max_chunk_chars:
            raise ValueError("min_chunk_chars 不能大于 max_chunk_chars。")
        return self


class ParserInput(InputModel):
    max_chars: int = Field(default=2_000_000, ge=1, le=20_000_000)


class PipelineInput(InputModel):
    chunker: ChunkerInput | None = None
    parser: ParserInput | None = None


class ProfileInput(InputModel):
    scan_mode: Literal["incremental", "full_sweep"] = "incremental"
    is_enabled: bool = True
    include: list[str] = Field(default_factory=list, max_length=1000)
    exclude: list[str] = Field(default_factory=list, max_length=1000)
    max_file_bytes: int = Field(default=200 * 1024 * 1024, ge=1)
    time_window_days: int | None = Field(default=None, ge=1)
    pipeline: PipelineInput = Field(default_factory=PipelineInput)


class HistoryPruneInput(InputModel):
    keep_days: int = Field(default=30, ge=1, le=36500)
    limit: int = Field(default=1000, ge=1, le=10000)


class CommandInput(InputModel):
    kind: Literal[
        "resource.create",
        "resource.update",
        "resource.delete",
        "resource.verify",
        "resource.pause",
        "resource.resume",
        "resource.revoke",
        "scan",
        "cleanup",
        "profile.save",
        "profile.delete",
        "history.prune",
    ]
    ref: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class SearchInput(InputModel):
    query: str = Field(min_length=1, max_length=8000)
    resource_ids: list[str] = Field(default_factory=list, max_length=200)
    mode: SearchMode = "auto"
    limit: int = Field(default=6, ge=1, le=30)

    @field_validator("query")
    @classmethod
    def nonempty_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("搜索内容不能为空。")
        return value


class ConversationInput(InputModel):
    title: str = Field(default="新对话", min_length=1, max_length=200)


class MessageInput(InputModel):
    content: str = Field(min_length=1, max_length=16000)
    resource_ids: list[str] = Field(default_factory=list, max_length=200)
    memory_enabled: bool = True
    search_mode: SearchMode = "auto"


class CredentialInput(InputModel):
    secret: str = Field(min_length=1, max_length=16000, repr=False)
    identity_ref: str | None = None
