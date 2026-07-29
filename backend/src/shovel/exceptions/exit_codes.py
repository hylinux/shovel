from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True,kw_only=True)
class ExitCodes:
    SUCCESS = 0

    GENERAL_ERROR = 1

    CONFIG_ERROR = 10

    WORKSPACE_ERROR = 20

    MODEL_ERROR = 30

    DEPENDENCY_ERROR = 40

    NETWORK_ERROR = 50

    VALIDATION_ERROR = 60

    PERMISSION_ERROR = 70

