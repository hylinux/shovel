from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from shovel.capability.domain import CapabilityKind
from shovel.cli.context import cli_context
from shovel.cli.decorators import command_handler
from shovel.cli.ui.console import console
from shovel.config.settings import load_settings
from shovel.domain.identity import new_id
from shovel.enterprise.domain import ConnectionDraft
from shovel.identity.domain import IdentityDraft
from shovel.memory.experience import ExperienceReview, ExperienceSubmission
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
connection = typer.Typer(no_args_is_help=True)
identity = typer.Typer(no_args_is_help=True)
authentication = typer.Typer(no_args_is_help=True)
experience = typer.Typer(no_args_is_help=True)


@experience.command("collect")
@command_handler()
def collect_experience(
    execution_id: str,
    submission_file: Path,
    revision: Annotated[int, typer.Option(min=1)],
) -> None:
    try:
        submission = ExperienceSubmission.model_validate_json(submission_file.read_text(encoding="utf-8"))
    except OSError, UnicodeError, ValidationError:
        raise RuntimeFailure(
            "Experience submission must be a bounded, credential-free JSON document."
        ) from None
    _run(
        PlatformCommand(
            action="experience.collect",
            execution_id=execution_id,
            expected_revision=revision,
            input=submission.model_dump(mode="json"),
        )
    )


@experience.command("list")
@command_handler()
def list_experiences() -> None:
    _run(PlatformCommand(action="experience.list"))


@experience.command("show")
@command_handler()
def show_experience(identifier: str) -> None:
    _run(PlatformCommand(action="experience.show", identifier=identifier))


@experience.command("review")
@command_handler()
def review_experience(
    identifier: str,
    revision: Annotated[int, typer.Option(min=1)],
    decision: Annotated[str, typer.Option(help="retain or reject; retaining does not activate Memory.")],
    note: Annotated[str, typer.Option()],
) -> None:
    review = ExperienceReview.model_validate({"decision": decision, "note": note})
    _run(
        PlatformCommand(
            action="experience.review",
            identifier=identifier,
            expected_revision=revision,
            input=review.model_dump(mode="json"),
        )
    )


@experience.command("forget")
@command_handler()
def forget_experience(identifier: str, revision: Annotated[int, typer.Option(min=1)]) -> None:
    _run(PlatformCommand(action="experience.forget", identifier=identifier, expected_revision=revision))


def _identity_input(path: Path) -> dict[str, Any]:
    try:
        draft = IdentityDraft.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError, UnicodeError, ValidationError:
        raise RuntimeFailure(
            "Identity configuration must be a credential-free JSON draft.", status=422
        ) from None
    return draft.model_dump(mode="json")


@identity.command("create")
@command_handler()
def create_identity(configuration_file: Path) -> None:
    _run(PlatformCommand(action="enterprise.identity.create", input=_identity_input(configuration_file)))


@identity.command("update")
@command_handler()
def update_identity(
    identifier: str,
    configuration_file: Path,
    revision: Annotated[int, typer.Option(min=1)],
) -> None:
    _run(
        PlatformCommand(
            action="enterprise.identity.update",
            identifier=identifier,
            expected_revision=revision,
            input=_identity_input(configuration_file),
        )
    )


def _register_identities() -> None:
    @identity.command("list")
    @command_handler()
    def list_identities() -> None:
        _run(PlatformCommand(action="enterprise.identity.list"))

    for action in ("show", "status", "disable"):

        def register(name: str) -> None:
            @identity.command(name)
            @command_handler()
            def act(identifier: str, revision: Annotated[int | None, typer.Option(min=1)] = None) -> None:
                if name == "disable" and revision is None:
                    raise RuntimeFailure("Identity disable requires an explicit revision.")
                _run(
                    PlatformCommand.model_validate(
                        {
                            "action": f"enterprise.identity.{name}",
                            "identifier": identifier,
                            "expected_revision": revision,
                        }
                    )
                )

        register(action)


_register_identities()


@authentication.command("list")
@command_handler()
def list_authentication() -> None:
    _run(PlatformCommand(action="authentication.list"))


@authentication.command("show")
@command_handler()
def show_authentication(identifier: str) -> None:
    _run(PlatformCommand(action="authentication.show", identifier=identifier))


@authentication.command("cancel")
@command_handler()
def cancel_authentication(
    identifier: str, execution_id: Annotated[str, typer.Option("--execution-id")]
) -> None:
    _run(PlatformCommand(action="authentication.cancel", identifier=identifier, execution_id=execution_id))


@connection.command("options")
@command_handler()
def connection_options() -> None:
    _run(PlatformCommand(action="enterprise.connection.options"))


@connection.command("list")
@command_handler()
def list_connections() -> None:
    _run(PlatformCommand(action="enterprise.connection.list"))


@connection.command("show")
@command_handler()
def show_connection(identifier: str) -> None:
    _run(PlatformCommand(action="enterprise.connection.show", identifier=identifier))


def _connection_input(path: Path) -> dict[str, Any]:
    try:
        draft = ConnectionDraft.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError, UnicodeError, ValidationError:
        raise RuntimeFailure(
            "Connection configuration must be a valid, credential-free JSON draft.", status=422
        ) from None
    return draft.model_dump(mode="json")


@connection.command("create")
@command_handler()
def create_connection(configuration_file: Path) -> None:
    _run(PlatformCommand(action="enterprise.connection.create", input=_connection_input(configuration_file)))


@connection.command("update")
@command_handler()
def update_connection(
    identifier: str,
    configuration_file: Path,
    revision: Annotated[int, typer.Option(min=1)],
) -> None:
    _run(
        PlatformCommand(
            action="enterprise.connection.update",
            identifier=identifier,
            expected_revision=revision,
            input=_connection_input(configuration_file),
        )
    )


@connection.command("validate")
@command_handler()
def validate_connection(identifier: str, revision: Annotated[int, typer.Option(min=1)]) -> None:
    _run(
        PlatformCommand(
            action="enterprise.connection.validate",
            identifier=identifier,
            expected_revision=revision,
        )
    )


@connection.command("disable")
@command_handler()
def disable_connection(identifier: str, revision: Annotated[int, typer.Option(min=1)]) -> None:
    _run(
        PlatformCommand(
            action="enterprise.connection.disable",
            identifier=identifier,
            expected_revision=revision,
        )
    )


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


def _register_effect_actions() -> None:
    for action in ("reconcile", "resume_effect"):

        def register(name: str) -> None:
            @execution.command(name.replace("_", "-"))
            @command_handler()
            def manage(identifier: str) -> None:
                _run(
                    PlatformCommand.model_validate(
                        {
                            "action": f"execution.{name}",
                            "identifier": identifier,
                        }
                    )
                )

        register(action)


_register_effect_actions()


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
