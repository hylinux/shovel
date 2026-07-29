from __future__ import annotations

from shovel.exceptions.base import ShovelError
from shovel.exceptions.exit_codes import ExitCodes


class ConfigError(ShovelError):
    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        details: str | None = None,
    ) -> None:
        super().__init__(
            message,
            exit_code=ExitCodes.CONFIG_ERROR,
            hint=hint,
            details=details,
        )


class ConfigNotFoundError(ConfigError):
    pass


class InvalidConfigError(ConfigError):
    pass


class ConfigValueError(ConfigError):
    pass
