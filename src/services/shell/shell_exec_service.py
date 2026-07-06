from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Dict, List, Optional

from component.logging import get_logger

log = get_logger(__name__)

_DEFAULT_TIMEOUT = 60.0


def _interpreter_bindirs() -> List[str]:
    """Directories holding the backend's own Python interpreter (venv `bin`).

    A non-interactive subprocess shell does NOT see the user's interactive `python`
    alias, so an agent command using `python` (or `pip`) would fail even though
    Python is installed. Putting the running interpreter's bin dir on PATH makes
    `python`/`python3`/`pip` resolve to the backend's interpreter inside shell tools."""
    dirs: List[str] = []
    for d in (os.path.join(sys.prefix, "bin"), os.path.dirname(sys.executable or "")):
        if d and os.path.isdir(d) and d not in dirs:
            dirs.append(d)
    return dirs


async def exec_command(
    command: str,
    *,
    env: Optional[Dict[str, str]] = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    merged_env = {**os.environ, **(env or {})}
    bindirs = _interpreter_bindirs()
    if bindirs:
        merged_env["PATH"] = os.pathsep.join([*bindirs, merged_env.get("PATH", "")]).rstrip(os.pathsep)
    log.infox("Shell commando uitvoeren", command=command[:120])

    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=merged_env,
    )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise asyncio.TimeoutError(f"Commando timed out na {timeout}s: {command[:80]}")

    result = {
        "exit_code": proc.returncode,
        "stdout": stdout_bytes.decode(errors="replace"),
        "stderr": stderr_bytes.decode(errors="replace"),
        "command": command,
    }
    log.infox(
        "Shell commando afgerond",
        exit_code=result["exit_code"],
        stdout_len=len(result["stdout"]),
        stderr_len=len(result["stderr"]),
    )
    return result


async def exec_script(
    script: str,
    parameters: List[str],
    env_defaults: Dict[str, str],
    args: Dict[str, str],
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    missing = [p for p in parameters if p not in args]
    if missing:
        raise ValueError(f"Verplichte parameters ontbreken: {', '.join(missing)}")
    return await exec_command(script, env={**env_defaults, **args}, timeout=timeout)
