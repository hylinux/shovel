from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from shovel.runtime.errors import RuntimeFailure


def request_operation_id(key: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
        raise RuntimeFailure("请求键必须为 1-128 个字母、数字或 . _ : -。")
    return f"op_request_{hashlib.sha256(key.encode()).hexdigest()}"


def request_fingerprint(kind: str, payload: dict[str, Any], ref: str | None = None) -> str:
    canonical = json.dumps([kind, ref, payload], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
