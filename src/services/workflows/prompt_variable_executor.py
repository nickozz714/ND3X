# services/workflows/prompt_variable_executor.py

from __future__ import annotations

import multiprocessing
from typing import Any

from component.logging import get_logger


log = get_logger(__name__)


ALLOWED_MODULES = {
    "datetime",
    "zoneinfo",
    "math",
    "json",
    "re",
}


def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    log.debugx(
        "Prompt variable import controleren",
        module=name,
        root_module=name.split(".")[0] if isinstance(name, str) else None,
        fromlist=fromlist,
        level=level,
    )

    root_name = name.split(".")[0]

    if root_name not in ALLOWED_MODULES:
        log.warningx(
            "Prompt variable import geblokkeerd",
            module=name,
            root_module=root_name,
            allowed_modules=sorted(ALLOWED_MODULES),
        )
        raise ImportError(f"Import not allowed: {name}")

    log.debugx(
        "Prompt variable import toegestaan",
        module=name,
        root_module=root_name,
    )
    return __import__(name, globals, locals, fromlist, level)


ALLOWED_BUILTINS = {
    "__import__": safe_import,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "len": len,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "list": list,
    "dict": dict,
    "range": range,
}


def _worker(code: str, queue: multiprocessing.Queue) -> None:
    log.infox(
        "Prompt variable worker gestart",
        code_length=len(code or ""),
        allowed_modules=sorted(ALLOWED_MODULES),
        allowed_builtin_names=sorted(ALLOWED_BUILTINS.keys()),
    )

    try:
        safe_globals = {
            "__builtins__": ALLOWED_BUILTINS,
        }

        safe_locals = {
            "result": None,
        }

        log.debugx(
            "Prompt variable code uitvoeren gestart",
            code_length=len(code or ""),
            safe_global_keys=list(safe_globals.keys()),
            safe_local_keys=list(safe_locals.keys()),
        )

        exec(code, safe_globals, safe_locals)

        result = safe_locals.get("result")

        log.infox(
            "Prompt variable code uitvoeren afgerond",
            result_type=type(result).__name__,
            result_is_none=result is None,
            result_length=len(str(result)) if result is not None else 0,
        )

        queue.put({
            "ok": True,
            "result": result,
        })

        log.debugx(
            "Prompt variable worker resultaat op queue gezet",
            ok=True,
            result_type=type(result).__name__,
        )

    except Exception as exc:
        log.warningx(
            "Prompt variable worker fout",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        queue.put({
            "ok": False,
            "error": str(exc),
        })


class PromptVariableExecutor:
    def execute(self, *, code: str, timeout_ms: int = 1000) -> str:
        log.infox(
            "Prompt variable execute gestart",
            code_length=len(code or ""),
            timeout_ms=timeout_ms,
        )

        queue = multiprocessing.Queue()

        process = multiprocessing.Process(
            target=_worker,
            args=(code, queue),
        )

        log.debugx(
            "Prompt variable process aangemaakt",
            process_name=process.name,
            timeout_ms=timeout_ms,
        )

        process.start()

        log.infox(
            "Prompt variable process gestart",
            process_pid=process.pid,
            process_name=process.name,
        )

        process.join(timeout_ms / 1000)

        log.debugx(
            "Prompt variable process join afgerond",
            process_pid=process.pid,
            is_alive=process.is_alive(),
            exitcode=process.exitcode,
            timeout_s=timeout_ms / 1000,
        )

        if process.is_alive():
            log.warningx(
                "Prompt variable process timeout, process wordt beëindigd",
                process_pid=process.pid,
                timeout_ms=timeout_ms,
            )
            process.terminate()
            process.join()
            log.warningx(
                "Prompt variable process beëindigd na timeout",
                process_pid=process.pid,
                exitcode=process.exitcode,
                timeout_ms=timeout_ms,
            )
            raise TimeoutError(f"Prompt variable timed out after {timeout_ms}ms")

        if queue.empty():
            log.errorx(
                "Prompt variable process gaf geen output",
                process_pid=process.pid,
                exitcode=process.exitcode,
            )
            raise RuntimeError("Prompt variable produced no output")

        payload: dict[str, Any] = queue.get()

        log.debugx(
            "Prompt variable payload uit queue gelezen",
            payload_keys=list(payload.keys()) if isinstance(payload, dict) else None,
            ok=payload.get("ok") if isinstance(payload, dict) else None,
            has_error=bool(payload.get("error")) if isinstance(payload, dict) else None,
            result_type=type(payload.get("result")).__name__ if isinstance(payload, dict) else None,
        )

        if not payload.get("ok"):
            log.warningx(
                "Prompt variable execute mislukt",
                error=payload.get("error"),
            )
            raise RuntimeError(payload.get("error") or "Prompt variable failed")

        result = "" if payload.get("result") is None else str(payload.get("result"))

        log.infox(
            "Prompt variable execute afgerond",
            result_length=len(result),
            result_empty=result == "",
        )
        return result