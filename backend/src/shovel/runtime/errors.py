from shovel.exceptions.base import ShovelError


class RuntimeFailure(ShovelError):
    def __init__(self, message: str, *, status: int = 400, hint: str | None = None) -> None:
        super().__init__(message, hint=hint)
        self.status = status


class NotFound(RuntimeFailure):
    def __init__(self, kind: str, identifier: str) -> None:
        super().__init__(f"{kind} 不存在: {identifier}", status=404)
