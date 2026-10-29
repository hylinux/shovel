from __future__ import annotations

import hashlib
import json
import math
import threading
from collections import OrderedDict
from time import monotonic
from typing import Any


class EmbeddingCache:
    """Bounded, process-local vectors; each provider instance owns its cache."""

    def __init__(self, entries: int, seconds: float, dimension: int | None = None) -> None:
        self.entries, self.seconds = entries, seconds
        self.dimension = dimension
        self._values: OrderedDict[str, tuple[float, list[float]]] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def key(args: Any, kwargs: Any) -> str:
        return hashlib.sha256(
            json.dumps([args, kwargs], sort_keys=True, ensure_ascii=True).encode("utf-8")
        ).hexdigest()

    def get(self, key: str) -> list[float] | None:
        with self._lock:
            item = self._values.pop(key, None)
            if item is None or monotonic() - item[0] >= self.seconds:
                return None
            self._values[key] = item
            return list(item[1])

    def put(self, key: str, vector: list[float]) -> None:
        if (
            not self.entries
            or not vector
            or (self.dimension is not None and len(vector) != self.dimension)
            or not all(math.isfinite(value) for value in vector)
        ):
            return
        with self._lock:
            self._values[key] = (monotonic(), list(vector))
            self._values.move_to_end(key)
            while len(self._values) > self.entries:
                self._values.popitem(last=False)
