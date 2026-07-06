from __future__ import annotations

from contextvars import ContextVar
from typing import Any


native_attachment_resources: ContextVar[dict[str, Any]] = ContextVar(
    "native_attachment_resources", default={}
)
