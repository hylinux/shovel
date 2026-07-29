from __future__ import annotations

from shovel.exceptions.base import ShovelError
from shovel.exceptions.exit_codes import ExitCodes


class WorkspaceError(ShovelError):
    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        details: str | None = None,
    ) -> None:
        super().__init__(
            message,
            exit_code=ExitCodes.WORKSPACE_ERROR,
            hint=hint,
            details=details,
        )


class WorkspaceNotInitializedError(WorkspaceError):
    pass


class WorkspaceAlreadyExistsError(WorkspaceError):
    pass


class InvalidWorkspaceError(WorkspaceError):
    pass
