from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import inspect
import os
import re
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from types import ModuleType
from typing import Any, Literal
from uuid import uuid4

from agent_framework import (
    BaseAgent,
    FileSkill,
    FileSkillScript,
    FunctionTool,
    MCPStdioTool,
    MCPStreamableHTTPTool,
    SkillsProvider,
    Workflow,
)
from agent_framework.openai import OpenAIChatCompletionClient
from pydantic import BaseModel, ConfigDict, Field, model_validator

from shovel.config.agent_config import AgentSettings
from shovel.runtime.errors import RuntimeFailure


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
        return self


class MCPSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mcpServers: dict[str, MCPServerSettings] = Field(default_factory=dict)


def mcp_revision(content: bytes | None) -> str:
    return hashlib.sha256(b"missing" if content is None else b"file:" + content).hexdigest()


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


class AgentExtensions:
    def __init__(self, settings: AgentSettings) -> None:
        self.settings = settings
        self.tools: list[Any] = []
        self.skills: SkillsProvider | None = None
        self._stack: AsyncExitStack | None = None
        self._lock = asyncio.Lock()
        self._modules: list[str] = []
        self.mcp_revision: str | None = None

    async def load(self, client: OpenAIChatCompletionClient[Any]) -> None:
        async with self._lock:
            if self._stack is not None:
                return
            stack = AsyncExitStack()
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
                if content is not None:
                    config = MCPSettings.model_validate_json(content)
                    for name, server in config.mcpServers.items():
                        if server.disabled:
                            continue
                        common: dict[str, Any] = {
                            "name": name,
                            "tool_name_prefix": f"mcp_{name}_",
                            "load_prompts": False,
                            "request_timeout": 30,
                        }
                        if server.allowed_tools:
                            common["allowed_tools"] = server.allowed_tools
                        connection: MCPStdioTool | MCPStreamableHTTPTool
                        if server.command:
                            connection = MCPStdioTool(
                                **common,
                                command=_environment(server.command),
                                args=[_environment(arg) for arg in server.args],
                                env={key: _environment(value) for key, value in server.env.items()},
                            )
                        else:
                            url = _environment(server.url)
                            if not url.startswith(("http://", "https://")):
                                raise RuntimeFailure("MCP url must use HTTP or HTTPS.", status=503)
                            connection = MCPStreamableHTTPTool(
                                **common,
                                url=url,
                                static_headers={
                                    key: _environment(value) for key, value in server.headers.items()
                                },
                            )
                        tools.append(await stack.enter_async_context(connection))
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
                await stack.aclose()
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
            for name in self._modules:
                sys.modules.pop(name, None)
            self._modules.clear()
