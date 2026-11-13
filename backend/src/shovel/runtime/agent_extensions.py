from __future__ import annotations

import asyncio
import importlib.util
import inspect
import os
import re
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4

from agent_framework import (
    BaseAgent,
    Content,
    FileSkill,
    FileSkillScript,
    FunctionTool,
    MCPStdioTool,
    MCPStreamableHTTPTool,
    SkillsProvider,
    Workflow,
)
from agent_framework.openai import OpenAIChatCompletionClient
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from shovel.config.agent_config import AgentSettings
from shovel.protection.egress import EgressChannel, require_ordinary
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.mcp_auth import (
    APIKeyAuth,
    BearerAuth,
    MCPAuth,
    MCPCredentials,
    MCPHTTPStatus,
    NoAuth,
    authenticated_http_client,
    authenticated_url,
    credential_reference,
    header_name,
    secret_header,
)
from shovel.runtime.workspace_config import content_revision as mcp_revision
from shovel.services.credentials import CredentialStore

if TYPE_CHECKING:
    from shovel.runtime.mcp_oauth import MCPOAuthService


class MCPServerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    disabled: bool = False
    allowed_tools: list[str] = Field(default_factory=list)
    type: Literal["stdio", "http"] | None = None
    auth: MCPAuth | None = None
    credential_id: UUID | None = None
    env_credentials: dict[str, str] = Field(default_factory=dict)

    @field_validator("env_credentials")
    @classmethod
    def credential_environment(cls, values: dict[str, str]) -> dict[str, str]:
        for key in values:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError("Invalid MCP credential environment variable name.")
        return {key: credential_reference(value) for key, value in values.items()}

    def credential_binding(self) -> dict[str, object]:
        return {
            "url": self.url,
            "command": self.command,
            "args": self.args,
            "auth": self.auth.model_dump(mode="json") if self.auth else None,
            "env_credentials": self.env_credentials,
            "env": self.env,
            "headers": self.headers,
        }

    def credential_references(self) -> list[str]:
        references = list(self.env_credentials.values())
        if isinstance(self.auth, (APIKeyAuth, BearerAuth)):
            references.append(self.auth.credential_ref)
        elif self.auth is not None and self.auth.type == "oauth_client_credentials":
            references.append(self.auth.client_secret_ref)
        return sorted(set(references))

    def resolved_credential_binding(self) -> dict[str, object]:
        binding = self.credential_binding()
        binding["env"] = {key: _environment(value) for key, value in self.env.items()} if self.command else {}
        binding["headers"] = (
            {key: _environment(value) for key, value in self.headers.items()} if self.url else {}
        )
        if self.url:
            url = _environment(self.url)
            authenticated_url(url)
            binding["url"] = url
        else:
            binding["command"] = _environment(self.command)
            binding["args"] = [_environment(arg) for arg in self.args]
        return binding

    @model_validator(mode="after")
    def transport(self) -> MCPServerSettings:
        if bool(self.command) == bool(self.url):
            raise ValueError("MCP server requires exactly one of command or url.")
        # Configuration can be saved before its Host environment is ready.
        if self.url and not (
            self.url.startswith(("http://", "https://"))
            or re.match(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}", self.url)
        ):
            raise ValueError("MCP url must use HTTP or HTTPS.")
        if self.type is not None and (self.type == "stdio") != bool(self.command):
            raise ValueError("MCP type does not match command/url.")
        explicit_auth = self.auth is not None and not isinstance(self.auth, NoAuth)
        if self.command and explicit_auth:
            raise ValueError("HTTP authentication cannot be used with stdio.")
        if self.url and self.env_credentials:
            raise ValueError("Environment credentials require stdio.")
        if (explicit_auth or self.env_credentials) and self.credential_id is None:
            raise ValueError("Referenced authentication requires a stable credential_id UUID.")
        if self.env_credentials:
            keys = [key.casefold() for key in (*self.env, *self.env_credentials)]
            if len(keys) != len(set(keys)):
                raise ValueError("Conflicting MCP environment credential names.")
        if explicit_auth:
            keys = [key.lower() for key in self.headers]
            if len(keys) != len(set(keys)):
                raise ValueError("Duplicate MCP headers.")
            if "authorization" in keys or (
                isinstance(self.auth, APIKeyAuth) and self.auth.header.lower() in keys
            ):
                raise ValueError("Authentication conflicts with static headers.")
            for key in self.headers:
                header_name(key)
            if not self.url.startswith("${"):
                try:
                    authenticated_url(self.url)
                except RuntimeFailure as exc:
                    raise ValueError(exc.message) from None
        return self


class MCPSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mcpServers: dict[str, MCPServerSettings] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_credentials(self) -> MCPSettings:
        ids = [server.credential_id for server in self.mcpServers.values() if server.credential_id]
        if len(ids) != len(set(ids)):
            raise ValueError("MCP credential_id must be unique for each service.")
        return self


def _environment(value: str) -> str:
    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise RuntimeFailure(f"MCP environment variable is not set: {name}", status=503)
        return os.environ[name]

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", substitute, value)


async def run_skill_script(
    skill: FileSkill, script: FileSkillScript, args: dict[str, Any] | list[str] | None = None
) -> str:
    require_ordinary(EgressChannel.TOOL, args)
    path = await asyncio.to_thread(Path(script.full_path).resolve)
    root = await asyncio.to_thread(Path(skill.path).resolve)
    if path.suffix != ".py" or not path.is_relative_to(root):
        raise RuntimeFailure("Skill script must be a Python file inside its skill directory.")
    if args is not None and (not isinstance(args, list) or any(not isinstance(arg, str) for arg in args)):
        raise RuntimeFailure("Skill script arguments must be an array of strings.")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(path),
        *(args or []),
        cwd=path.parent,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise RuntimeFailure(f"Skill script failed (exit {process.returncode}).")
    output = stdout.decode("utf-8")
    if len(output) > 32000:
        raise RuntimeFailure("Skill script output exceeds the 32000-character limit.")
    return output


class CredentialBoundStdioTool(MCPStdioTool):
    def __init__(self, credential_status: MCPHTTPStatus, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._credential_status = credential_status

    async def _call_tool(self, tool_name: str, kwargs: dict[str, Any]) -> str | list[Content]:
        # Both generated FunctionTools and call_tool use this SDK dispatch hook.
        self._credential_status.check_current()
        return await super()._call_tool(tool_name, kwargs)


class AgentExtensions:
    def __init__(
        self,
        settings: AgentSettings,
        *,
        credentials: CredentialStore | None = None,
        oauth: MCPOAuthService | None = None,
    ) -> None:
        self.settings = settings
        self.tools: list[Any] = []
        self.skills: SkillsProvider | None = None
        self._stack: AsyncExitStack | None = None
        self._lock = asyncio.Lock()
        self._modules: list[str] = []
        self.mcp_revision: str | None = None
        self.mcp_attempt_revision: str | None = None
        self.mcp_connections: dict[str, MCPHTTPStatus] = {}
        self._credentials = credentials
        self._oauth = oauth

    async def _mcp_credentials(self, server: MCPServerSettings) -> tuple[dict[str, str], dict[str, str]]:
        env = {key: _environment(value) for key, value in server.env.items()} if server.command else {}
        headers = {key: _environment(value) for key, value in server.headers.items()} if server.url else {}
        if server.auth is not None and not isinstance(server.auth, (NoAuth, APIKeyAuth, BearerAuth)):
            if self._oauth is None:
                raise RuntimeFailure("MCP OAuth requires the Host authentication service.", status=503)
            return env, headers
        if server.credential_id is not None:
            binding = server.resolved_credential_binding()
            credentials = MCPCredentials(
                self.settings.workspace, server.credential_id, binding, self._credentials
            )
            for key, reference in server.env_credentials.items():
                env[key] = await credentials.get(reference)
            if isinstance(server.auth, (APIKeyAuth, BearerAuth)):
                value = await credentials.get(server.auth.credential_ref)
                if isinstance(server.auth, BearerAuth):
                    headers["Authorization"] = secret_header(value, bearer=True)
                else:
                    headers[server.auth.header] = secret_header(value)
                for header in headers.values():
                    secret_header(header)
        return env, headers

    async def load(self, client: OpenAIChatCompletionClient[Any]) -> None:
        require_ordinary(EgressChannel.TOOL)
        async with self._lock:
            if self._stack is not None:
                return
            stack = AsyncExitStack()
            loading_status: MCPHTTPStatus | None = None
            try:
                workspace = self.settings.workspace
                for directory in ("skills", "functions", "pipeline"):
                    (workspace / directory).mkdir(parents=True, exist_ok=True)
                skills = SkillsProvider.from_paths(
                    workspace / "skills",
                    script_runner=run_skill_script if self.settings.enable_python_extensions else None,
                    disable_load_skill_approval=True,
                    disable_read_skill_resource_approval=True,
                    disable_run_skill_script_approval=self.settings.enable_python_extensions,
                )
                tools: list[Any] = []
                mcp_path = workspace / "mcp.json"
                try:
                    content = mcp_path.read_bytes()
                except FileNotFoundError:
                    content = None
                self.mcp_attempt_revision = mcp_revision(content)
                self.mcp_connections = {}
                if content is not None:
                    config = MCPSettings.model_validate_json(content)
                    for name, server in config.mcpServers.items():
                        if server.disabled:
                            continue
                        http_status = MCPHTTPStatus()
                        if self._oauth is not None:
                            generations = self._oauth.credential_generations
                            generation = generations.get(name, 0)

                            def credential_current(
                                name: str = name,
                                generation: int = generation,
                                generations: dict[str, int] = generations,
                            ) -> bool:
                                return generations.get(name, 0) == generation

                            http_status.is_current = credential_current
                        http_status.state = "connecting"
                        self.mcp_connections[name] = loading_status = http_status
                        common: dict[str, Any] = {
                            "name": name,
                            "tool_name_prefix": f"mcp_{name}_",
                            "load_prompts": False,
                            "request_timeout": 30,
                        }
                        if server.allowed_tools:
                            common["allowed_tools"] = server.allowed_tools
                        try:
                            env, headers = await self._mcp_credentials(server)
                        except RuntimeFailure as exc:
                            raise RuntimeFailure(
                                f"MCP {name}: {exc.message}", status=exc.status, hint=exc.hint
                            ) from None
                        connection: MCPStdioTool | MCPStreamableHTTPTool
                        if server.command:
                            connection = CredentialBoundStdioTool(
                                credential_status=http_status,
                                **common,
                                command=_environment(server.command),
                                args=[_environment(arg) for arg in server.args],
                                env=env,
                            )
                        else:
                            url = _environment(server.url)
                            if not url.startswith(("http://", "https://")):
                                raise RuntimeFailure("MCP url must use HTTP or HTTPS.", status=503)
                            if server.auth is not None and not isinstance(server.auth, NoAuth):
                                authenticated_url(url)
                                if isinstance(server.auth, (APIKeyAuth, BearerAuth)):
                                    http_client = authenticated_http_client(http_status)
                                else:
                                    assert self._oauth is not None
                                    http_client = await self._oauth.client(name, server, http_status)
                                common["http_client"] = await stack.enter_async_context(http_client)
                            connection = MCPStreamableHTTPTool(
                                **common,
                                url=url,
                                static_headers=headers,
                            )
                        try:
                            tools.append(await stack.enter_async_context(connection))
                            http_status.state = "connected"
                            loading_status = None
                        except Exception:
                            status = http_status.denied
                            if status is not None:
                                reason = "authentication failed" if status == 401 else "permission denied"
                                raise RuntimeFailure(
                                    f"MCP {name}: {reason} (HTTP {status}).",
                                    status=503,
                                    hint="Check this MCP service's credentials and granted permissions.",
                                ) from None
                            raise
                if self.settings.enable_python_extensions:
                    for path in sorted((workspace / "functions").glob("*.py")):
                        module = self._module(path)
                        exports = getattr(module, "tools", None)
                        if not isinstance(exports, (list, tuple)) or not all(
                            isinstance(item, FunctionTool) or callable(item) for item in exports
                        ):
                            raise ValueError(f"{path.name} must export a tools list.")
                        tools.extend(exports)
                    for path in sorted((workspace / "pipeline").glob("*.py")):
                        module = self._module(path)
                        factory = getattr(module, "create_agent", None)
                        if not callable(factory):
                            raise ValueError(f"{path.name} must export create_agent(client).")
                        child = factory(client)
                        if inspect.isawaitable(child):
                            child = await child
                        if isinstance(child, Workflow):
                            child = child.as_agent(name=path.stem)
                        if not isinstance(child, BaseAgent):
                            raise ValueError(f"{path.name} must build a MAF Agent or Workflow.")
                        tools.append(child.as_tool(name=f"pipeline_{path.stem}"))
                self.tools, self.skills, self._stack = tools, skills, stack
                self.mcp_revision = mcp_revision(content)
            except BaseException as exc:
                if loading_status is not None:
                    loading_status.state = (
                        {
                            401: "authorization_required",
                            409: "configuration_changed",
                            422: "configuration_error",
                        }.get(exc.status, "unavailable")
                        if isinstance(exc, RuntimeFailure)
                        else "unavailable"
                    )
                await stack.aclose()
                for connection_status in self.mcp_connections.values():
                    if connection_status.state == "connected":
                        connection_status.state = "not_connected"
                if isinstance(exc, (asyncio.CancelledError, RuntimeFailure)):
                    raise
                raise RuntimeFailure(
                    f"Agent extensions could not load ({type(exc).__name__}).",
                    status=503,
                    hint="Check workspace skills, functions, pipeline and mcp.json.",
                ) from exc

    def _module(self, path: Path) -> ModuleType:
        name = f"shovel_user_{uuid4().hex}"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ValueError(f"Cannot load extension {path.name}.")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        self._modules.append(name)
        spec.loader.exec_module(module)
        return module

    async def close(self) -> None:
        try:
            if self._stack is not None:
                await self._stack.aclose()
                self._stack = None
        finally:
            for status in self.mcp_connections.values():
                if status.state == "connected":
                    status.state = "not_connected"
            for name in self._modules:
                sys.modules.pop(name, None)
            self._modules.clear()
