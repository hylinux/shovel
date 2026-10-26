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
    memory_enabled: bool = False
    search_mode: SearchMode = "auto"


class CredentialInput(InputModel):
    secret: str = Field(min_length=1, max_length=16000, repr=False)
    identity_ref: str | None = None
