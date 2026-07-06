"""
services/local_models/deploy_status.py

In-memory status for local-model deploys so the workbench can poll progress and
surface clear failure messages for troubleshooting. Keyed by (host, model).
States: pulling | ready | error.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional


_STATUS: Dict[str, Dict] = {}


def _key(host: str, model: str) -> str:
    return f"{host}|{model}"


def set_status(host: str, model: str, state: str, message: Optional[str] = None) -> Dict:
    entry = {
        "host": host,
        "model": model,
        "state": state,
        "message": message,
        "updated_at": int(time.time() * 1000),
    }
    _STATUS[_key(host, model)] = entry
    return entry


def set_progress(
    host: str,
    model: str,
    *,
    percent: Optional[float] = None,
    status: Optional[str] = None,
    completed: Optional[int] = None,
    total: Optional[int] = None,
) -> Dict:
    """Update live pull progress (state stays 'pulling'). `percent` is 0..1 across
    all layers; `status` is Ollama's current step (downloading, verifying, …)."""
    entry = dict(_STATUS.get(_key(host, model)) or {"host": host, "model": model})
    entry["state"] = "pulling"
    entry["updated_at"] = int(time.time() * 1000)
    if status is not None:
        entry["status_text"] = status
    if percent is not None:
        entry["progress"] = max(0.0, min(1.0, float(percent)))
    if completed is not None:
        entry["completed"] = int(completed)
    if total is not None:
        entry["total"] = int(total)
    pct = entry.get("progress")
    step = entry.get("status_text") or "Pulling"
    entry["message"] = step + (f" — {round(pct * 100)}%" if isinstance(pct, (int, float)) else "…")
    _STATUS[_key(host, model)] = entry
    return entry


def get_status(host: str, model: str) -> Optional[Dict]:
    return _STATUS.get(_key(host, model))


def all_statuses() -> List[Dict]:
    return list(_STATUS.values())
