from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from shovel.capability.domain import CapabilityKind
from shovel.cli.context import cli_context
from shovel.cli.decorators import command_handler
from shovel.cli.ui.console import console
from shovel.config.settings import load_settings
from shovel.domain.identity import new_id
from shovel.platform.domain import PlatformCommand
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.platform_client import platform_command


def _confirm(preview: dict[str, Any]) -> bool:
    console.print_json(data=preview)
    return typer.confirm("Confirm this exact action and the structured IDs shown above?", default=False)


def _run(command: PlatformCommand) -> None:
    result = asyncio.run(
        platform_command(
            load_settings(cli_context.get_default_config_file()),
            command,
            confirm=_confirm,
        )
    )
    console.print_json(data=result)


def _object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise RuntimeFailure("参数必须是合法 JSON。") from exc
    if not isinstance(value, dict):
        raise RuntimeFailure("参数必须是 JSON 对象。")
    return value


def capability_app(kind: CapabilityKind | None = None) -> typer.Typer:
    app = typer.Typer(no_args_is_help=True)

    @app.command("list")
    @command_handler()
    def list_capabilities() -> None:
        _run(PlatformCommand(action="capability.list", kind=kind))

    @app.command("show")
    @command_handler()
    def show(identifier: str, version: Annotated[int | None, typer.Option(min=1)] = None) -> None:
        _run(PlatformCommand(action="capability.show", identifier=identifier, kind=kind, version=version))

    @app.command("search")
    @command_handler()
    def search(query: str) -> None:
        _run(PlatformCommand(action="capability.search", kind=kind, query=query))

    @app.command("versions")
    @command_handler()
    def versions(identifier: str) -> None:
        _run(PlatformCommand(action="capability.versions", identifier=identifier, kind=kind))

    if kind is not None:

        @app.command("validate")
        @command_handler()
        def validate(identifier: str, version: int) -> None:
            _run(
                PlatformCommand(
                    action="capability.validate", identifier=identifier, kind=kind, version=version
                )
            )

        @app.command("publish")
        @command_handler()
        def publish(
            identifier: str, version: int, revision: Annotated[int | None, typer.Option(min=1)] = None
        ) -> None:
            _run(
                PlatformCommand(
                    action="capability.publish",
                    identifier=identifier,
                    kind=kind,
                    version=version,
                    expected_revision=revision,
                )
            )

    if kind == CapabilityKind.WORKFLOW:

        @app.command("run")
        @command_handler()
        def run(identifier: str, version: int, input: Annotated[str, typer.Option()] = "{}") -> None:
            _run(
                PlatformCommand(
                    action="execution.run",
                    identifier=identifier,
                    kind=kind,
                    version=version,
                    input=_object(input),
                    request_key=cli_context.request_key or new_id("request"),
                )
            )

    return app


capability = capability_app()
skill = capability_app(CapabilityKind.SKILL)
workflow = capability_app(CapabilityKind.WORKFLOW)
proactive = typer.Typer(no_args_is_help=True)
approval = typer.Typer(no_args_is_help=True)
execution = typer.Typer(no_args_is_help=True)
interaction = typer.Typer(no_args_is_help=True)


def _register_services() -> None:
    for action in ("list", "show", "pause", "resume", "enable", "disable", "run", "history", "explain"):

        def register(name: str) -> None:
            if name == "list":

                @proactive.command(name)
                @command_handler()
                def list_services() -> None:
                    _run(PlatformCommand(action="proactive.list"))
            else:

                @proactive.command(name)
                @command_handler()
                def service(
                    identifier: str,
                    revision: Annotated[int | None, typer.Option(min=1)] = None,
                    input: Annotated[str, typer.Option()] = "{}",
                ) -> None:
                    action = PlatformCommand.model_validate(
                        {
                            "action": f"proactive.{name}",
                            "identifier": identifier,
                            "expected_revision": revision,
                            "input": _object(input),
                            "request_key": (cli_context.request_key or new_id("request"))
                            if name == "run"
                            else None,
                        }
                    )
                    _run(action)

        register(action)


_register_services()


@approval.command("list")
@command_handler()
def list_approvals() -> None:
    _run(PlatformCommand(action="approval.list"))


def _register_approvals() -> None:
    for action in ("show", "approve", "reject"):

        def register(name: str) -> None:
            @approval.command(name)
            @command_handler()
            def decision(
                identifier: str, execution_id: Annotated[str, typer.Option("--execution-id")]
            ) -> None:
                _run(
                    PlatformCommand.model_validate(
                        {
                            "action": f"approval.{name}",
                            "identifier": identifier,
                            "execution_id": execution_id,
                        }
                    )
                )

        register(action)


_register_approvals()


@execution.command("list")
@command_handler()
def list_executions() -> None:
    _run(PlatformCommand(action="execution.list"))


def _register_executions() -> None:
    for action in ("show", "cancel", "resume"):

        def register(name: str) -> None:
            @execution.command(name)
            @command_handler()
            def manage(identifier: str) -> None:
                _run(
                    PlatformCommand.model_validate({"action": f"execution.{name}", "identifier": identifier})
                )

        register(action)


_register_executions()


@interaction.command("list")
@command_handler()
def list_interactions() -> None:
    _run(PlatformCommand(action="interaction.list"))


@interaction.command("input")
@command_handler()
def provide_input(
    identifier: str,
    execution_id: Annotated[str, typer.Option("--execution-id")],
    response: Annotated[str, typer.Option("--response")],
) -> None:
    try:
        value = json.loads(response)
        command = PlatformCommand(
            action="interaction.input", identifier=identifier, execution_id=execution_id, response=value
        )
    except (ValueError, ValidationError) as exc:
        raise RuntimeFailure("Input Response 必须是合法且有界的 JSON。") from exc
    _run(command)
