from pydantic import BaseModel, ConfigDict, Field

from shovel.capability.domain import Permission
from shovel.security.domain import UserSecurityPolicy


class PlatformSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(default="owner", min_length=1, max_length=128)
    security: UserSecurityPolicy = Field(default_factory=UserSecurityPolicy)
    agent_permissions: tuple[Permission, ...] = ()
