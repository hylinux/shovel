from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse

from shovel.capability.domain import CapabilityKind
from shovel.platform.domain import PlatformCommand
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.service import Runtime


def platform_router(dependency: Any) -> APIRouter:
    router = APIRouter(prefix="/api/v1/platform", tags=["platform"])
    runtime_dependency = Depends(dependency)

    @router.post("/commands")
    async def command(
        body: PlatformCommand,
        rt: Runtime = runtime_dependency,
        request_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        if request_key is not None:
            if body.request_key is not None and body.request_key != request_key:
                raise RuntimeFailure("Idempotency-Key conflicts with request_key.", status=409)
            body = body.model_copy(update={"request_key": request_key})
        result = await rt.platform.execute(body, human=True)
        status = 202 if body.action in {"execution.run", "proactive.run", "execution.resume"} else 200
        return JSONResponse(result, status_code=status)

    @router.get("/capabilities")
    async def capabilities(
        rt: Runtime = runtime_dependency, kind: CapabilityKind | None = None, query: str = ""
    ) -> dict[str, Any]:
        return await rt.platform.execute(
            PlatformCommand(action="capability.list", kind=kind, query=query), human=True
        )

    @router.get("/options")
    async def options(rt: Runtime = runtime_dependency) -> dict[str, Any]:
        return await rt.platform.execute(PlatformCommand(action="capability.options"), human=True)

    @router.get("/automation")
    async def automation(rt: Runtime = runtime_dependency) -> dict[str, Any]:
        return await rt.platform.execute(PlatformCommand(action="automation.show"), human=True)

    @router.get("/requests/{key}")
    async def request_receipt(
        key: str,
        rt: Runtime = runtime_dependency,
        service_id: str | None = None,
    ) -> dict[str, Any]:
        return await rt.platform.execute(
            PlatformCommand(action="execution.find", request_key=key, identifier=service_id),
            human=True,
        )

    @router.get("/capabilities/{identifier}/versions")
    async def versions(identifier: str, rt: Runtime = runtime_dependency) -> dict[str, Any]:
        return await rt.platform.execute(
            PlatformCommand(action="capability.versions", identifier=identifier),
            human=True,
        )

    @router.get("/proactive/{identifier}")
    async def service(identifier: str, rt: Runtime = runtime_dependency) -> dict[str, Any]:
        return await rt.platform.execute(
            PlatformCommand(action="proactive.show", identifier=identifier),
            human=True,
        )

    @router.get("/proactive/{identifier}/history")
    async def service_history(identifier: str, rt: Runtime = runtime_dependency) -> dict[str, Any]:
        return await rt.platform.execute(
            PlatformCommand(action="proactive.history", identifier=identifier),
            human=True,
        )

    @router.get("/capabilities/{identifier}")
    async def capability(
        identifier: str, rt: Runtime = runtime_dependency, version: int | None = None
    ) -> dict[str, Any]:
        return await rt.platform.execute(
            PlatformCommand(action="capability.show", identifier=identifier, version=version), human=True
        )

    @router.get("/executions")
    async def executions(rt: Runtime = runtime_dependency) -> dict[str, Any]:
        return await rt.platform.execute(PlatformCommand(action="execution.list"), human=True)

    @router.get("/executions/{identifier}")
    async def execution(identifier: str, rt: Runtime = runtime_dependency) -> dict[str, Any]:
        return await rt.platform.execute(
            PlatformCommand(action="execution.show", identifier=identifier), human=True
        )

    @router.get("/proactive")
    async def proactive(rt: Runtime = runtime_dependency) -> dict[str, Any]:
        return await rt.platform.execute(PlatformCommand(action="proactive.list"), human=True)

    @router.get("/approvals")
    async def approvals(rt: Runtime = runtime_dependency) -> dict[str, Any]:
        return await rt.platform.execute(PlatformCommand(action="approval.list"), human=True)

    @router.get("/interactions")
    async def interactions(rt: Runtime = runtime_dependency) -> dict[str, Any]:
        return await rt.platform.execute(PlatformCommand(action="interaction.list"), human=True)

    @router.get("/interactions/{identifier}")
    async def input_request(
        identifier: str,
        execution_id: str,
        rt: Runtime = runtime_dependency,
    ) -> dict[str, Any]:
        return await rt.platform.execute(
            PlatformCommand(action="interaction.show", identifier=identifier, execution_id=execution_id),
            human=True,
        )

    return router
