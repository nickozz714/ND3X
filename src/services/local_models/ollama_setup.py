"""
services/local_models/ollama_setup.py

Detect / start / install the Ollama runtime on the host the backend runs on.

- start(): if the binary exists but the daemon is down, spawn `ollama serve`
  (detached) — used to auto-recover on deploy.
- install(): platform install (brew on macOS when available, else the official
  install script). Explicit action (host-modifying), surfaced behind a button.

Only meaningful for a LOCAL host (localhost / 127.0.0.1). A backend in a
container cannot install/start Ollama on the host — `can_manage()` reflects that.

`which`, `run`, `spawn` are injectable for unit tests (no real processes).
"""
from __future__ import annotations

import platform
import shutil
import subprocess
from typing import Callable, Dict, List, Optional, Tuple

from component.logging import get_logger

log = get_logger(__name__)

# run(cmd, timeout) -> (returncode, stdout, stderr)
RunFn = Callable[..., Tuple[int, str, str]]
# spawn(cmd) -> None  (fire-and-forget, detached)
SpawnFn = Callable[[List[str]], None]
WhichFn = Callable[[str], Optional[str]]

_OLLAMA_INSTALL_SH = "curl -fsSL https://ollama.com/install.sh | sh"


def _default_run(cmd: List[str], timeout: float = 30.0) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout or "", p.stderr or ""
    except Exception as exc:  # noqa: BLE001
        return 1, "", f"{type(exc).__name__}: {exc}"


def _default_spawn(cmd: List[str]) -> None:
    # Detached so the daemon survives a backend restart; output to a log.
    logf = open("/tmp/ollama_serve.log", "ab")  # noqa: SIM115
    subprocess.Popen(cmd, stdout=logf, stderr=logf, start_new_session=True)


class OllamaSetupService:
    def __init__(
        self,
        *,
        system: Optional[str] = None,
        which: WhichFn = shutil.which,
        run: RunFn = _default_run,
        spawn: SpawnFn = _default_spawn,
    ):
        self._system = system or platform.system()
        self._which = which
        self._run = run
        self._spawn = spawn

    def detect(self) -> Dict:
        path = self._which("ollama")
        installed = bool(path)
        version = None
        if installed:
            rc, out, _ = self._run(["ollama", "--version"])
            if rc == 0:
                version = (out or "").strip()
        return {
            "os": self._system,
            "installed": installed,
            "binary_path": path,
            "version": version,
            "has_brew": bool(self._which("brew")),
        }

    @staticmethod
    def in_container() -> bool:
        """True when the backend itself runs inside a container — installing or
        starting Ollama there would land INSIDE the app container (wrong and
        non-persistent); deploys use the compose sidecar instead."""
        import os
        from pathlib import Path
        return (
            Path("/.dockerenv").exists()
            or bool(os.environ.get("KUBERNETES_SERVICE_HOST"))
        )

    @staticmethod
    def can_manage(host: str) -> bool:
        # Inside a container "localhost" is the container itself — never
        # installable/manageable, regardless of what the host string says.
        if OllamaSetupService.in_container():
            return False
        h = (host or "").lower()
        return any(tok in h for tok in ("localhost", "127.0.0.1", "[::1]"))

    def start(self) -> Dict:
        if not self._which("ollama"):
            return {"ok": False, "message": "Ollama is not installed — install it first."}
        try:
            self._spawn(["ollama", "serve"])
            log.infox("Ollama serve gestart")
            return {"ok": True, "message": "Started `ollama serve`."}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"Could not start Ollama: {exc}"}

    def install(self) -> Dict:
        if self._system == "Darwin" and self._which("brew"):
            cmd = ["brew", "install", "ollama"]
        elif self._system in ("Darwin", "Linux"):
            cmd = ["/bin/bash", "-lc", _OLLAMA_INSTALL_SH]
        else:
            return {"ok": False, "message": f"Automatic install is not supported on {self._system}. Install Ollama from ollama.com."}
        rc, out, err = self._run(cmd, timeout=900)
        ok = rc == 0
        tail = ((out or "")[-3000:] + ("\n" + err[-1500:] if err else "")).strip()
        log.infox("Ollama install uitgevoerd", ok=ok, command=" ".join(cmd))
        return {
            "ok": ok,
            "message": "Ollama installed." if ok else "Install failed — see output.",
            "command": " ".join(cmd),
            "output": tail,
        }
