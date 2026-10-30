from __future__ import annotations

import hashlib
import json

from shovel.capability.domain import CapabilityScope


def execution_request_key(scope: CapabilityScope, key: str) -> str:
    identity = json.dumps([scope.workspace_id, scope.user_id, key], ensure_ascii=False, separators=(",", ":"))
    return "capability/" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
