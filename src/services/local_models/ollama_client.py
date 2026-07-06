"""
services/local_models/ollama_client.py

Thin async client for the Ollama management API with diagnosable errors.

The `host` selects the deploy target — `http://localhost:11434` for a host-run
daemon, `http://host.docker.internal:11434` for a backend-in-container talking to
a host daemon, or a sidecar hostname for an in-container/sidecar daemon.

All transport failures are converted to OllamaUnreachableError with an actionable
message; HTTP/body errors to OllamaError carrying Ollama's own message.
An httpx.AsyncClient can be injected for testing (httpx.MockTransport).
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, List, Optional

import httpx

from component.logging import get_logger

log = get_logger(__name__)


def _default_host() -> str:
    """Effective default Ollama host. Docker/K8s deploys set OLLAMA_HOST on the
    backend container (e.g. ``ollama:11434`` → the compose sidecar); without it
    everything silently pointed at the container's own localhost, which is why
    Ollama 'did not work in Docker'. A bare host:port gets an http scheme."""
    raw = (os.environ.get("OLLAMA_HOST") or "").strip()
    if not raw:
        return "http://localhost:11434"
    if "://" not in raw:
        raw = f"http://{raw}"
    return raw.rstrip("/")


DEFAULT_HOST = _default_host()


class OllamaError(RuntimeError):
    pass


class OllamaUnreachableError(OllamaError):
    """The Ollama daemon could not be contacted at all."""


def _unreachable_message(host: str, exc: Exception) -> str:
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return (
            f"Cannot reach Ollama at {host} — connection refused. "
            f"Make sure Ollama is installed and running (`ollama serve`). "
            f"If the backend runs in a container, point it at the host with "
            f"http://host.docker.internal:11434."
        )
    if isinstance(exc, httpx.TimeoutException):
        return f"Ollama at {host} did not respond in time (timeout)."
    return f"Could not reach Ollama at {host}: {type(exc).__name__}: {exc}"


def _ollama_body_error(resp: httpx.Response) -> Optional[str]:
    try:
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            return str(data["error"])
    except Exception:  # noqa: BLE001 — non-JSON body
        pass
    return (resp.text or "").strip() or None


class OllamaClient:
    def __init__(self, host: str = DEFAULT_HOST, *, client: Optional[httpx.AsyncClient] = None, timeout: float = 30.0):
        self.host = (host or DEFAULT_HOST).rstrip("/")
        self._client = client
        self._timeout = timeout

    async def _request(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            if self._client is not None:
                return await self._client.request(method, f"{self.host}{path}", **kw)
            async with httpx.AsyncClient(base_url=self.host, timeout=self._timeout) as c:
                return await c.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise OllamaUnreachableError(_unreachable_message(self.host, exc)) from exc

    async def version(self) -> Dict[str, Any]:
        r = await self._request("GET", "/api/version")
        r.raise_for_status()
        return r.json()

    async def is_available(self) -> bool:
        try:
            await self.version()
            return True
        except Exception:  # noqa: BLE001
            return False

    async def list_models(self) -> List[Dict[str, Any]]:
        r = await self._request("GET", "/api/tags")
        r.raise_for_status()
        return (r.json() or {}).get("models", []) or []

    async def has_model(self, model: str) -> bool:
        names = {m.get("name") for m in await self.list_models()}
        # Ollama lists e.g. "qwen2.5:7b"; a bare "qwen2.5" implies ":latest".
        target = model if ":" in model else f"{model}:latest"
        return target in names or model in names

    async def delete(self, model: str) -> bool:
        r = await self._request("DELETE", "/api/delete", json={"name": model})
        if r.status_code >= 400:
            raise OllamaError(_ollama_body_error(r) or f"delete failed: HTTP {r.status_code}")
        return True

    async def pull(
        self,
        model: str,
        on_progress: Optional[Callable[[str, Optional[float], int, int], None]] = None,
    ) -> Dict[str, Any]:
        """Pull a model via Ollama. With ``on_progress`` it STREAMS Ollama's pull
        progress and calls ``on_progress(status, percent, completed, total)`` as the
        download advances (percent is 0..1 across all layers, or None before sizes
        are known). Without it, a blocking pull. Raises OllamaError with Ollama's own
        message on failure. Run from a background task for responsiveness."""
        if on_progress is None:
            r = await self._request("POST", "/api/pull", json={"name": model, "stream": False}, timeout=None)
            if r.status_code >= 400:
                msg = _ollama_body_error(r) or f"HTTP {r.status_code}"
                raise OllamaError(f"Ollama could not pull '{model}': {msg}")
            data = r.json() if r.content else {"status": "success"}
            if isinstance(data, dict) and data.get("error"):
                raise OllamaError(f"Ollama could not pull '{model}': {data['error']}")
            return data

        # Streaming pull: aggregate per-layer total/completed for an overall percent.
        layers: Dict[str, Dict[str, int]] = {}
        last: Dict[str, Any] = {"status": "success"}
        client = self._client
        own = client is None
        if own:
            client = httpx.AsyncClient(timeout=self._timeout)
        try:
            async with client.stream(
                "POST", f"{self.host}/api/pull",
                json={"name": model, "stream": True}, timeout=None,
            ) as r:
                if r.status_code >= 400:
                    await r.aread()
                    raise OllamaError(f"Ollama could not pull '{model}': {_ollama_body_error(r) or f'HTTP {r.status_code}'}")
                async for line in r.aiter_lines():
                    line = (line or "").strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:  # noqa: BLE001 — skip a partial/garbled line
                        continue
                    if isinstance(obj, dict) and obj.get("error"):
                        raise OllamaError(f"Ollama could not pull '{model}': {obj['error']}")
                    if isinstance(obj, dict):
                        last = obj
                    digest = obj.get("digest")
                    total_layer = obj.get("total")
                    if digest and total_layer:
                        layers[digest] = {"total": int(total_layer or 0), "completed": int(obj.get("completed") or 0)}
                    total = sum(v["total"] for v in layers.values())
                    completed = sum(v["completed"] for v in layers.values())
                    percent = (completed / total) if total else None
                    try:
                        on_progress(str(obj.get("status") or ""), percent, completed, total)
                    except Exception:  # noqa: BLE001 — progress is best-effort
                        pass
        except httpx.HTTPError as exc:
            raise OllamaUnreachableError(_unreachable_message(self.host, exc)) from exc
        finally:
            if own:
                await client.aclose()
        return last
