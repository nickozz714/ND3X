from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None

    try:
        value = value.replace("Z", "+00:00")
        return datetime.fromisoformat(value)
    except Exception:
        return None


class FileLogRepository:
    """
    Reads rotating JSON log files produced by component.logging.

    Expected files:
      logs/app.log
      logs/app.log.1
      logs/app.log.2
      ...

    Works best with:
      LOG_FORMAT=json
      LOG_FILE=logs/app.log
    """

    def __init__(self, *, log_file: str | Path):
        self.log_file = Path(log_file)

    def _candidate_files(self) -> List[Path]:
        base = self.log_file

        files: List[Path] = []

        if base.exists():
            files.append(base)

        parent = base.parent
        name = base.name

        if parent.exists():
            rotated = sorted(
                parent.glob(f"{name}.*"),
                key=lambda p: self._rotation_sort_key(p),
            )
            files.extend(rotated)

        # Newest first by mtime. Good enough for rotated log files.
        return sorted(
            [p for p in files if p.exists() and p.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    @staticmethod
    def _rotation_sort_key(path: Path) -> int:
        suffix = path.name.rsplit(".", 1)[-1]
        try:
            return int(suffix)
        except Exception:
            return 999999

    def _read_entries(self, *, max_lines: int = 20000) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        read_lines = 0

        for file_path in self._candidate_files():
            try:
                lines = file_path.read_text(
                    encoding="utf-8",
                    errors="replace",
                ).splitlines()
            except Exception:
                continue

            # Current newest-ish first from end of file.
            for line in reversed(lines):
                if read_lines >= max_lines:
                    return entries

                read_lines += 1
                line = line.strip()

                if not line:
                    continue

                try:
                    obj = json.loads(line)
                except Exception:
                    obj = {
                        "ts": None,
                        "level": "INFO",
                        "logger": str(file_path),
                        "msg": line,
                        "_raw": line,
                    }

                if not isinstance(obj, dict):
                    continue

                obj.setdefault("_file", str(file_path))
                entries.append(obj)

        return entries

    @staticmethod
    def _matches_q(entry: Dict[str, Any], q: str) -> bool:
        haystack = json.dumps(entry, ensure_ascii=False, default=str).lower()
        return q.lower() in haystack

    @staticmethod
    def _entry_datetime(entry: Dict[str, Any]) -> Optional[datetime]:
        return _parse_dt(
            entry.get("ts")
            or entry.get("created_at")
            or entry.get("timestamp")
        )

    def search(
        self,
        *,
        q: Optional[str] = None,
        level: Optional[str] = None,
        logger: Optional[str] = None,
        trace_id: Optional[str] = None,
        sequence: Optional[str] = None,
        step: Optional[str] = None,
        created_from: Optional[str] = None,
        created_to: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
        max_lines: int = 20000,
    ) -> Tuple[int, List[Dict[str, Any]]]:
        skip = max(0, int(skip or 0))
        limit = max(1, min(int(limit or 100), 500))

        level = level.upper().strip() if level else None
        created_from_dt = _parse_dt(created_from)
        created_to_dt = _parse_dt(created_to)

        entries = self._read_entries(max_lines=max_lines)
        filtered: List[Dict[str, Any]] = []

        for entry in entries:
            entry_level = str(entry.get("level") or "").upper()
            entry_logger = str(entry.get("logger") or "")
            entry_trace_id = str(entry.get("trace_id") or "")
            entry_sequence = str(entry.get("sequence") or "")
            entry_step = str(entry.get("step") or "")

            if level and level in LEVELS and entry_level != level:
                continue

            if logger and logger.lower() not in entry_logger.lower():
                continue

            if trace_id and trace_id != entry_trace_id:
                continue

            if sequence and sequence.lower() not in entry_sequence.lower():
                continue

            if step and step.lower() not in entry_step.lower():
                continue

            dt = self._entry_datetime(entry)

            if created_from_dt and dt and dt < created_from_dt:
                continue

            if created_to_dt and dt and dt > created_to_dt:
                continue

            if q and not self._matches_q(entry, q):
                continue

            filtered.append(self._normalize_entry(entry))

        total = len(filtered)
        return total, filtered[skip: skip + limit]

    @staticmethod
    def _normalize_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
        extra_json = entry.get("extra_json")

        if extra_json is None:
            extra_fields = {
                k: v
                for k, v in entry.items()
                if k not in {
                    "ts",
                    "level",
                    "logger",
                    "msg",
                    "trace_id",
                    "span_id",
                    "sequence",
                    "step",
                    "duration_ms",
                    "since_prev_ms",
                    "context",
                    "exc_type",
                    "exc",
                    "exc_text",
                    "_file",
                    "_raw",
                }
            }
            extra_json = json.dumps(extra_fields, ensure_ascii=False, default=str)

        return {
            "id": abs(hash(json.dumps(entry, ensure_ascii=False, default=str))),
            "created_at": entry.get("ts") or entry.get("created_at"),
            "level": entry.get("level") or "INFO",
            "logger": entry.get("logger") or "",
            "message": entry.get("msg") or entry.get("message") or entry.get("_raw") or "",
            "trace_id": entry.get("trace_id"),
            "span_id": entry.get("span_id"),
            "sequence": entry.get("sequence"),
            "step": entry.get("step"),
            "duration_ms": entry.get("duration_ms"),
            "since_prev_ms": entry.get("since_prev_ms"),
            "context": entry.get("context"),
            "extra_json": extra_json,
            "exc_type": entry.get("exc_type"),
            "exc_text": entry.get("exc_text") or entry.get("exc"),
            "source_file": entry.get("_file"),
        }