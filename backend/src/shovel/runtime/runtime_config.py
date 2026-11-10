from __future__ import annotations

import json
import math
import os
import tomllib
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import tomli_w
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from shovel.config.settings import AppSettings, _to_toml_safe, drop_removed_keys
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.workspace_config import WorkspaceTextFile
from shovel.services.config_server import shovel_context

# Only these scalar fields can be written; no raw TOML or caller-selected paths.
_SECTIONS = {
    "": (
        "service_name agent_host agent_port http_host dev_http_host dev_http_port "
        "dev_frontend_port http_port frontend_path log_level"
    ),
    "model": (
        "model_provider_type base_endpoint model_name api_version security_key "
        "max_input_chars max_output_tokens timeout_seconds"
    ),
    "agent": "enable_python_extensions max_tool_iterations",
    "performance": (
        "warm_index warm_local_models max_foreground_runs foreground_queue_seconds "
        "background_idle_seconds embedding_cache_entries embedding_cache_seconds "
        "max_local_model_requests context_profile max_concurrent_scans"
    ),
    "database": "echo",
    "zvec": "dim",
    "embedding": (
        "provider base_url model dimensions api_key api_version batch_size "
        "timeout_seconds max_retries max_input_chars"
    ),
    "memory": (
        "collection dim context_max_chars max_items min_similarity min_confidence "
        "summary_max_chars history_messages"
    ),
    "memory.llm": (
        "model_provider_type base_endpoint model_name api_version security_key "
        "max_input_chars max_output_tokens timeout_seconds"
    ),
    "memory.embedder": (
        "provider base_url model dimensions api_key api_version batch_size "
        "timeout_seconds max_retries max_input_chars"
    ),
}
_EDITABLE = {
    f"{section}.{name}" if section else name for section, names in _SECTIONS.items() for name in names.split()
}
_FIXED = ("agent.workspace_path", "database.url", "zvec.knowledge_path", "memory.index_path")
_SECRETS = frozenset(
    ("model.security_key", "embedding.api_key", "memory.llm.security_key", "memory.embedder.api_key")
)
_CHAT_PROVIDERS = ("", "azure_openai", "deepseek", "qianwen", "ollama")
_EMBEDDING_PROVIDERS = ("", "azure_openai", "qianwen", "ollama")


class RuntimeConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(min_length=64, max_length=64)
    changes: dict[str, Any] = Field(default_factory=dict, max_length=100)


class RuntimeConfigApplyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(min_length=64, max_length=64)


def _get(data: dict[str, Any], key: str) -> Any:
    value: Any = data
    for part in key.split("."):
        value = value[part]
    return value


def _set(data: dict[str, Any], key: str, value: Any) -> None:
    parts = key.split(".")
    for part in parts[:-1]:
        data = data.setdefault(part, {})
    if value is None:
        data.pop(parts[-1], None)
    else:
        data[parts[-1]] = value


def _invalid(key: str, reason: str) -> None:
    # Never include submitted values or Pydantic's input/context in errors.
    raise RuntimeFailure(f"{key}: {reason}", status=422)


def _validate_settings(raw: dict[str, Any], *, environment: bool = False) -> AppSettings:
    data = deepcopy(raw)
    drop_removed_keys(data)
    try:
        return AppSettings(**data) if environment else AppSettings.from_file_data(data)
    except ValidationError as exc:
        fields = ", ".join(".".join(map(str, error["loc"])) or "configuration" for error in exc.errors())
        raise RuntimeFailure(
            f"{fields}: Invalid configuration; check field types, ranges and provider requirements.",
            status=422,
        ) from None
    except ValueError:
        raise RuntimeFailure(
            "The saved configuration is invalid. Check settings.toml locally.", status=422
        ) from None


class RuntimeConfigurationService:
    def __init__(self, active: AppSettings) -> None:
        self.active = active
        self.file = WorkspaceTextFile(shovel_context.get_default_config_file())

    def _read(self) -> tuple[dict[str, Any], AppSettings, str]:
        view = self.file.read()
        if not view["exists"]:
            raise RuntimeFailure("Run shovel init before editing runtime configuration.", status=409)
        try:
            raw = tomllib.loads(view["content"])
        except tomllib.TOMLDecodeError:
            raise RuntimeFailure("settings.toml is invalid TOML. Repair it locally.", status=422) from None
        return raw, _validate_settings(raw), view["revision"]

    def read(self) -> dict[str, Any]:
        raw, saved, revision = self._read()
        return self._view(raw, saved, revision)

    def _view(self, raw: dict[str, Any], saved: AppSettings, revision: str) -> dict[str, Any]:
        effective = _validate_settings(raw, environment=True)
        values = _to_toml_safe(saved.model_dump(mode="python"))
        active = _to_toml_safe(self.active.model_dump(mode="python"))
        resolved = _to_toml_safe(effective.model_dump(mode="python"))
        items = []
        pending = []
        for key in sorted(_EDITABLE | set(_FIXED)):
            parent: BaseModel = saved
            parts = key.split(".")
            for part in parts[:-1]:
                parent = getattr(parent, part)
            field = type(parent).model_fields[parts[-1]]
            schema = TypeAdapter(field.rebuild_annotation()).json_schema()
            if key.endswith("_port"):
                schema.update(minimum=1, maximum=65535)
            elif key in {"zvec.dim", "embedding.dimensions", "memory.embedder.dimensions"}:
                schema.update(minimum=1)
            value = self._value(values, key)
            active_value = self._value(active, key)
            effective_value = self._value(resolved, key)
            changed = effective_value != active_value
            if changed:
                pending.append(key)
            secret = key in _SECRETS
            kind = (
                "boolean"
                if isinstance(value, bool)
                else ("number" if isinstance(value, (int, float)) or parts[-1] == "dimensions" else "string")
            )
            choices = schema.get("enum")
            if key.endswith("model_provider_type"):
                choices = list(_CHAT_PROVIDERS)
            elif key in ("embedding.provider", "memory.embedder.provider"):
                choices = list(_EMBEDDING_PROVIDERS)
            elif key.endswith("_host"):
                choices = (
                    ["127.0.0.1", "localhost"]
                    if key == "dev_http_host"
                    else ["127.0.0.1", "localhost", "::1"]
                )
            elif key == "log_level":
                choices = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
            if choices is not None and value not in choices:
                choices = [*choices, value]  # Preserve existing unverified providers, never relabel them.
            env_key = "SHOVEL_" + key.replace(".", "__").upper()
            prefixes = [env_key]
            while "__" in env_key:
                env_key = env_key.rsplit("__", 1)[0]
                prefixes.append(env_key)
            sources = [name for name in prefixes if name in {name.upper() for name in os.environ}]
            items.append(
                {
                    "key": key,
                    "section": ".".join(parts[:-1]),
                    "type": kind,
                    "nullable": parts[-1] == "dimensions",
                    "choices": choices,
                    "minimum": schema.get("minimum", schema.get("exclusiveMinimum")),
                    "maximum": schema.get("maximum"),
                    "integer": schema.get("type") == "integer" or parts[-1] == "dimensions",
                    "secret": secret,
                    "value": None if secret else value,
                    "active_value": None if secret else active_value,
                    "effective_value": None if secret else effective_value,
                    "configured": bool(value) if secret else None,
                    "active_configured": bool(active_value) if secret else None,
                    "effective_configured": bool(effective_value) if secret else None,
                    "environment": sources,
                    "pending": changed,
                    "readonly": key in _FIXED,
                }
            )
        return {
            "path": str(self.file.path),
            "revision": revision,
            "items": items,
            "pending": pending,
            "applied": active == resolved,
            "editable": all(_get(values, key) == _get(active, key) for key in _FIXED),
            "application": "host_restart",
        }

    @staticmethod
    def _value(data: dict[str, Any], key: str) -> Any:
        for part in key.split(".")[:-1]:
            data = data[part]
        return data.get(key.split(".")[-1])

    def save(self, body: RuntimeConfigInput) -> dict[str, Any]:
        raw, before, revision = self._read()
        if revision != body.revision:
            raise RuntimeFailure("Configuration changed. Reload the saved file before saving.", status=409)
        if not self._view(raw, before, revision)["editable"]:
            raise RuntimeFailure(
                "This Host does not use the fixed profile stores. Edit its configuration locally.",
                status=409,
            )
        for key, value in body.changes.items():
            if key not in _EDITABLE:
                _invalid("configuration", "Field is not editable.")
            parent: BaseModel = before
            for part in key.split(".")[:-1]:
                parent = getattr(parent, part)
            field = type(parent).model_fields[key.split(".")[-1]]
            try:
                TypeAdapter(field.rebuild_annotation()).validate_json(json.dumps(value), strict=True)
            except ValidationError, ValueError, TypeError:
                _invalid(key, "Invalid type or value outside the permitted range.")
            if key in _SECRETS and value == "**********":
                _invalid(key, "Enter a new key, keep the existing key, or explicitly clear it.")
            _set(raw, key, value)
        after = _validate_settings(raw)
        self._validate(after, body.changes)
        effective = _validate_settings(raw, environment=True)
        self._validate(effective, body.changes)
        self._vector_guard(before, after)
        self._vector_guard(self.active, effective)
        result = self.file.save(tomli_w.dumps(raw), revision)
        return self._view(raw, after, result["revision"])

    def check_application(self, revision: str) -> dict[str, Any]:
        result = self.read()
        if result["revision"] != revision:
            raise RuntimeFailure("Configuration changed. Reload before checking application.", status=409)
        # A partial in-process rebind would leave workers, clients and middleware on old settings.
        # Verification never restarts the live Host or claims Restart Agent applies these fields.
        return result

    def _validate(self, settings: AppSettings, changes: dict[str, Any]) -> None:
        for key in changes:
            value = self._value(_to_toml_safe(settings.model_dump(mode="python")), key)
            if isinstance(value, float) and not math.isfinite(value):
                _invalid(key, "Enter a finite number.")
            if key.endswith("_host"):
                allowed_hosts = {"127.0.0.1", "localhost"}
                if key != "dev_http_host":
                    allowed_hosts.add("::1")
                if value not in allowed_hosts:
                    _invalid(
                        key,
                        "Use a supported loopback address; the development proxy requires IPv4 or localhost.",
                    )
            if key.endswith("_port") and not 1 <= value <= 65535:
                _invalid(key, "Port must be between 1 and 65535.")
            if key == "service_name" and not value.strip():
                _invalid(key, "Service name cannot be empty.")
            if key == "memory.collection" and not value.strip():
                _invalid(key, "Collection name cannot be empty.")
            if key == "log_level" and value not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
                _invalid(key, "Choose a supported log level.")
        if (
            any(key in changes for key in ("dev_http_port", "dev_frontend_port"))
            and settings.dev_http_port == settings.dev_frontend_port
        ):
            _invalid("dev_frontend_port", "Development frontend and backend ports must differ.")
        for section in ("model", "memory.llm", "embedding", "memory.embedder"):
            if not any(key.startswith(section + ".") for key in changes):
                continue
            chat = section in {"model", "memory.llm"}
            config = settings.model_dump(mode="python")
            data = _get(config, section)
            provider_key = "model_provider_type" if chat else "provider"
            provider = data[provider_key]
            allowed = _CHAT_PROVIDERS if chat else _EMBEDDING_PROVIDERS
            previous = _get(self.active.model_dump(mode="python"), section)[provider_key]
            if provider not in allowed and provider != previous:
                _invalid(section + "." + provider_key, "Provider is outside this editor's support scope.")
            model = data["model_name" if chat else "model"]
            if bool(provider) != bool(model.strip()):
                _invalid(
                    section + "." + ("model_name" if chat else "model"),
                    "Set both provider and model, or leave both empty.",
                )
            endpoint_key = "base_endpoint" if chat else "base_url"
            endpoint = data[endpoint_key]
            if provider == "azure_openai":
                if not data["api_version"].strip():
                    _invalid(section + ".api_version", "Azure requires an API version.")
                if not endpoint:
                    _invalid(section + "." + endpoint_key, "Azure requires a resource endpoint.")
            if not chat and provider and provider not in {"ollama", "openai"} and not endpoint:
                _invalid(section + ".base_url", "This embedding provider requires an explicit endpoint.")
            if endpoint:
                try:
                    url = urlsplit(endpoint)
                    _ = url.port
                except ValueError:
                    _invalid(section + "." + endpoint_key, "Invalid HTTP(S) endpoint.")
                    continue
                if (
                    url.scheme not in {"http", "https"}
                    or not url.hostname
                    or url.username
                    or url.password
                    or url.query
                    or url.fragment
                    or any(character.isspace() for character in url.netloc)
                ):
                    _invalid(
                        section + "." + endpoint_key,
                        "Use an HTTP(S) endpoint without credentials, query or fragment.",
                    )
                if provider == "azure_openai" and url.path.rstrip("/"):
                    _invalid(section + "." + endpoint_key, "Use the Azure resource root endpoint.")
        for section, embedding, dimension in (
            ("embedding", settings.embedding, settings.zvec.dim),
            ("memory.embedder", settings.memory.embedder, settings.memory.dim),
        ):
            if embedding.dimensions is not None and embedding.dimensions != dimension:
                _invalid(
                    section + ".dimensions", "Embedding dimensions must match the corresponding index dim."
                )
        if settings.zvec.dim < 1:
            _invalid("zvec.dim", "Index dimension must be positive.")

    def _vector_guard(self, before: AppSettings, after: AppSettings) -> None:
        for section, old, new, old_dim, new_dim, path, collection_changed in (
            (
                "embedding",
                before.embedding,
                after.embedding,
                before.zvec.dim,
                after.zvec.dim,
                Path(before.zvec.knowledge_path),
                False,
            ),
            (
                "memory.embedder",
                before.memory.embedder,
                after.memory.embedder,
                before.memory.dim,
                after.memory.dim,
                before.memory.index_path,
                before.memory.collection != after.memory.collection,
            ),
        ):
            fields = (
                "provider",
                "model",
                "revision",
                "base_url",
                "api_version",
                "dimensions",
                "max_input_chars",
            )
            identity_changed = any(getattr(old, name) != getattr(new, name) for name in fields)
            if identity_changed or old_dim != new_dim or collection_changed:
                try:
                    populated = path.exists() and any(path.iterdir())
                except OSError:
                    raise RuntimeFailure("Cannot inspect the existing vector index.", status=503) from None
                if populated:
                    _invalid(
                        section,
                        "Existing vectors require a controlled rebuild before changing model, "
                        "dimension or collection. No data was changed.",
                    )
