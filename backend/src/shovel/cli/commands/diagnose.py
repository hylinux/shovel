from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from shovel.cli.context import cli_context
from shovel.cli.decorators import command_handler
from shovel.cli.ui.console import console
from shovel.config.settings import load_settings
from shovel.runtime.client import diagnose_retrieval, diagnostic_configuration_id, workspace_id
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.schemas import RetrievalDiagnosticInput

app = typer.Typer(no_args_is_help=True, help="Explicit diagnostics against an already running Host.")


@app.command("retrieval")
@command_handler()
def retrieval(
    query: Annotated[str, typer.Argument(help="Query text; may invoke the configured embedding provider.")],
    resource: Annotated[
        list[str] | None, typer.Option("--resource", help="Readable resource ID; repeatable.")
    ] = None,
    compare_query: Annotated[
        list[str] | None, typer.Option("--compare-query", help="Additional query; at most two.")
    ] = None,
    repetitions: Annotated[
        int, typer.Option(min=0, max=10, help="Native repeats after each full retrieval.")
    ] = 3,
    limit: Annotated[
        int, typer.Option(min=1, max=30, help="Result limit; native topk is min(300, limit * 10).")
    ] = 6,
    budget_seconds: Annotated[
        float, typer.Option(min=1, max=600, help="Stops subsequent samples, not native I/O.")
    ] = 180,
    output: Annotated[Path | None, typer.Option(help="Write a sanitized JSON report to a new file.")] = None,
) -> None:
    settings = load_settings(cli_context.get_default_config_file())
    request = RetrievalDiagnosticInput(
        query=query,
        resource_ids=resource or [],
        comparison_queries=compare_query or [],
        repetitions=repetitions,
        limit=limit,
        budget_seconds=budget_seconds,
        workspace_id=workspace_id(settings),
        configuration_id=diagnostic_configuration_id(settings),
    )
    if output is not None and output.exists():
        raise RuntimeFailure("Diagnostic output already exists; choose a new file.", status=409)
    report = asyncio.run(diagnose_retrieval(settings, request))
    if output is not None:
        try:
            with output.open("x", encoding="utf-8") as destination:
                destination.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        except OSError as exc:
            raise RuntimeFailure(
                "Unable to write diagnostic report; check output directory and permissions."
            ) from exc
    else:
        console.print_json(data=report)
    if report["status"] != "complete":
        raise typer.Exit(1)
