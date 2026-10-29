from __future__ import annotations

import asyncio
import json
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_framework import Agent, MCPStreamableHTTPTool
from agent_framework.openai import OpenAIChatCompletionClient
from mcp.server.fastmcp import FastMCP
from openai import AsyncOpenAI

from shovel.config.agent_config import AgentSettings
from shovel.runtime.agent_extensions import AgentExtensions
from shovel.runtime.errors import RuntimeFailure


def completion(*, content: str | None = None, calls: list[dict[str, Any]] | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "test",
            "created": 1,
            "model": "test",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls" if calls else "stop",
                    "message": {"role": "assistant", "content": content, "tool_calls": calls},
                }
            ],
        },
    )


def tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


async def test_native_skills_python_functions_and_workflow_execute(tmp_path: Path) -> None:
    skill = tmp_path / "skills" / "handbook"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: handbook\ndescription: Read the Shovel handbook.\n---\nHandbook marker.\n",
        encoding="utf-8",
    )
    (skill / "scripts").mkdir()
    (skill / "scripts" / "echo.py").write_text(
        "import sys\nprint('script marker: ' + sys.argv[1])\n", encoding="utf-8"
    )
    functions = tmp_path / "functions"
    functions.mkdir()
    (functions / "math_tools.py").write_text(
        "from dataclasses import dataclass\n"
        "@dataclass\nclass Result:\n    value: int\n"
        "def double(value: int) -> int:\n"
        "    '''Double an integer.'''\n"
        "    return Result(value * 2).value\n"
        "tools = [double]\n",
        encoding="utf-8",
    )
    pipeline = tmp_path / "pipeline"
    pipeline.mkdir()
    (pipeline / "flow.py").write_text(
        "from agent_framework import Message, WorkflowBuilder, WorkflowContext, executor\n"
        "@executor(id='echo')\n"
        "async def echo(task: list[Message], ctx: WorkflowContext) -> None:\n"
        "    await ctx.yield_output('workflow marker: ' + task[-1].text)\n"
        "def create_agent(client):\n"
        "    return WorkflowBuilder(start_executor=echo).build()\n",
        encoding="utf-8",
    )
    requests: list[dict[str, Any]] = []

    def model(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            names = {item["function"]["name"] for item in body["tools"]}
            assert {"double", "pipeline_flow", "load_skill"}.issubset(names)
            assert "Read the Shovel handbook" in json.dumps(body["messages"])
            assert "Handbook marker" not in json.dumps(body["messages"])
            return completion(
                calls=[
                    tool_call("double", {"value": 6}),
                    tool_call("pipeline_flow", {"task": "test"}),
                    tool_call("load_skill", {"skill_name": "handbook"}),
                    tool_call(
                        "run_skill_script",
                        {"skill_name": "handbook", "script_name": "scripts/echo.py", "args": ["caf\u00e9"]},
                    ),
                ]
            )
        results = [message["content"] for message in body["messages"] if message["role"] == "tool"]
        assert any("12" in text for text in results)
        assert any("workflow marker: test" in text for text in results)
        assert any("Handbook marker" in text for text in results)
        assert any("script marker: caf\u00e9" in text for text in results)
        return completion(content="Tools completed.")

    async with AsyncOpenAI(
        api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(model))
    ) as sdk:
        client = OpenAIChatCompletionClient(model="test", async_client=sdk)
        extensions = AgentExtensions(AgentSettings(workspace_path=tmp_path, enable_python_extensions=True))
        try:
            await extensions.load(client)
            assert extensions.skills is not None
            agent = Agent(client, tools=extensions.tools, context_providers=[extensions.skills])
            assert (await agent.run("Read handbook and use the tools.")).text == "Tools completed."
        finally:
            await extensions.close()


async def test_python_extensions_are_not_imported_without_explicit_opt_in(tmp_path: Path) -> None:
    for folder in ("functions", "pipeline"):
        directory = tmp_path / folder
        directory.mkdir()
        (directory / "untrusted.py").write_text("raise RuntimeError('must not import')", encoding="utf-8")
    async with AsyncOpenAI(api_key="test") as sdk:
        extensions = AgentExtensions(AgentSettings(workspace_path=tmp_path))
        try:
            await extensions.load(OpenAIChatCompletionClient(model="test", async_client=sdk))
            assert extensions.tools == []
        finally:
            await extensions.close()


@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_real_mcp_transports_execute_and_close_on_owner_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport: str
) -> None:
    async with AsyncExitStack() as stack:
        if transport == "stdio":
            script = tmp_path / "server.py"
            script.write_text(
                "from mcp.server.fastmcp import FastMCP\n"
                "server = FastMCP('test')\n"
                "@server.tool()\n"
                "def echo(value: str) -> str:\n"
                "    return 'mcp marker: ' + value\n"
                "server.run(transport='stdio')\n",
                encoding="utf-8",
            )
            server_config = {"command": sys.executable, "args": [str(script)]}
        else:
            server = FastMCP("test", stateless_http=True, json_response=True)

            @server.tool()
            def echo(value: str) -> str:
                return "mcp marker: " + value

            app = server.streamable_http_app()
            await stack.enter_async_context(server.session_manager.run())
            http_client = await stack.enter_async_context(
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
            )

            def connection(**kwargs: Any) -> MCPStreamableHTTPTool:
                return MCPStreamableHTTPTool(**kwargs, http_client=http_client)

            monkeypatch.setattr("shovel.runtime.agent_extensions.MCPStreamableHTTPTool", connection)
            monkeypatch.setenv("SHOVEL_TEST_MCP_URL", "http://127.0.0.1:8000/mcp")
            server_config = {"url": "${SHOVEL_TEST_MCP_URL}"}
        (tmp_path / "mcp.json").write_text(
            json.dumps({"mcpServers": {"test": {**server_config, "allowed_tools": ["echo"]}}}),
            encoding="utf-8",
        )
        requests: list[dict[str, Any]] = []

        def model(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                name = body["tools"][0]["function"]["name"]
                assert name.startswith("mcp_test_")
                return completion(calls=[tool_call(name, {"value": "test"})])
            results = [message["content"] for message in body["messages"] if message["role"] == "tool"]
            assert any("mcp marker: test" in text for text in results)
            return completion(content="MCP completed.")

        sdk = await stack.enter_async_context(
            AsyncOpenAI(api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(model)))
        )
        extensions = AgentExtensions(AgentSettings(workspace_path=tmp_path))
        try:
            await extensions.load(OpenAIChatCompletionClient(model="test", async_client=sdk))
            agent = Agent(OpenAIChatCompletionClient(model="test", async_client=sdk), tools=extensions.tools)
            assert (await agent.run("Echo test.")).text == "MCP completed."
        finally:
            await asyncio.create_task(extensions.close())


@pytest.mark.parametrize(
    ("disabled", "url"),
    [(False, None), (False, "file:///private-mcp-token"), (True, None)],
)
async def test_mcp_environment_urls_are_resolved_and_checked_only_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disabled: bool, url: str | None
) -> None:
    variable = "SHOVEL_TEST_UNRESOLVED_MCP_URL"
    monkeypatch.delenv(variable, raising=False)
    if url is not None:
        monkeypatch.setenv(variable, url)
    (tmp_path / "mcp.json").write_text(
        json.dumps({"mcpServers": {"test": {"url": f"${{{variable}}}", "disabled": disabled}}}),
        encoding="utf-8",
    )
    async with AsyncOpenAI(api_key="test") as sdk:
        extensions = AgentExtensions(AgentSettings(workspace_path=tmp_path))
        try:
            client = OpenAIChatCompletionClient(model="test", async_client=sdk)
            if disabled:
                await extensions.load(client)
                assert extensions.tools == []
            else:
                with pytest.raises(RuntimeFailure) as failure:
                    await extensions.load(client)
                assert failure.value.status == 503
                assert "private-mcp-token" not in failure.value.message
        finally:
            await extensions.close()


async def test_invalid_mcp_configuration_fails_explicitly_without_credentials_in_error(
    tmp_path: Path,
) -> None:
    (tmp_path / "mcp.json").write_text(
        '{"mcpServers":{"bad":{"url":"not-a-url-private-token"}}}', encoding="utf-8"
    )
    async with AsyncOpenAI(api_key="test") as sdk:
        extensions = AgentExtensions(AgentSettings(workspace_path=tmp_path))
        try:
            with pytest.raises(RuntimeFailure, match="extensions could not load") as failure:
                await extensions.load(OpenAIChatCompletionClient(model="test", async_client=sdk))
            assert "private-token" not in failure.value.message
        finally:
            await extensions.close()
