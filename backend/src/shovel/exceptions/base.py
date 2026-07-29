from __future__ import annotations

from .exit_codes import ExitCodes


class ShovelError(Exception):

    def __init__(
            self,
            message: str,
            *,
            exit_code : int = ExitCodes.GENERAL_ERROR,
            hint: str | None = None,
            details: str | None = None,
    ) -> None:

        super().__init__(message)

        self.message = message
        self.exit_code = exit_code
        self.hint = hint
        self.details = details

