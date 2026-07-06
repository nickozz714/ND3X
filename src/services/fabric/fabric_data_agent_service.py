"""Microsoft Fabric Data Agent: registry CRUD, auth (3 methods), and querying.

A Data Agent is consumed via its OpenAI-compatible (Assistants-shaped) endpoint:
  {base}/threads → /messages → /runs → poll → messages + run steps
where base =
  https://api.fabric.microsoft.com/v1/workspaces/{ws}/aiskills/{agent}/aiassistant/openai

ALL Fabric-wire specifics (endpoint shape, api-version, Assistants flow, run-step
parsing) live in `ask()` below and are clearly marked — that's the only part that
needs validating against a real Data Agent. No platform LLM calls here.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from component.logging import get_logger
from models.fabric_data_agent import FabricDataAgent, FABRIC_AUTH_METHODS
from schemas.fabric_data_agent import FabricDataAgentCreate, FabricDataAgentUpdate, FabricDataAgentRead
from utils.crypto import encrypt_value, decrypt_value

log = get_logger(__name__)

FABRIC_RESOURCE = "https://api.fabric.microsoft.com"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
# Azure CLI's Microsoft first-party PUBLIC client — works in any tenant with no app
# registration, loopback redirect only. Override via env for a different first-party app.
AZURE_CLI_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
# The Data Agent OpenAI-compatible endpoint requires an api-version query param on
# every call. This is the version Microsoft's official sample client uses.
DEFAULT_API_VERSION = "2024-05-01-preview"


# ── auth (per-agent: service principal / azure login / stored bearer) ───────────
async def _token_service_principal(agent: FabricDataAgent) -> str:
    if not (agent.tenant_id and agent.client_id and agent.client_secret_encrypted):
        raise RuntimeError("Service principal auth needs tenant_id, client_id and client_secret.")
    secret = decrypt_value(agent.client_secret_encrypted)
    import httpx
    url = f"https://login.microsoftonline.com/{agent.tenant_id}/oauth2/v2.0/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": agent.client_id,
        "client_secret": secret,
        "scope": FABRIC_SCOPE,
    }
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(url, data=data)
        if r.status_code != 200:
            raise RuntimeError(f"Service principal token failed ({r.status_code}): {r.text[:300]}")
        return r.json()["access_token"]


async def _token_azure_login() -> str:
    """Reuse the active Azure CLI login session to mint a Fabric-scoped token."""
    from services.shell.shell_exec_service import exec_command
    r = await exec_command(
        f"az account get-access-token --resource {FABRIC_RESOURCE} --query accessToken -o tsv"
    )
    out = (r.get("stdout") or "").strip()
    if r.get("exit_code") != 0 or not out:
        raise RuntimeError(
            "Azure login token failed — run an Azure login first (az login / device code). "
            + (r.get("stderr") or "")[:300]
        )
    return out


def _is_desktop() -> bool:
    """The Tauri desktop shell sets ND3X_DESKTOP=1. Interactive browser login needs
    the backend + browser on the same machine, which only holds for the desktop app
    (not a server/Docker deployment behind a domain)."""
    return bool(os.environ.get("ND3X_DESKTOP"))


def _token_interactive_browser_sync(agent: FabricDataAgent) -> str:
    if not _is_desktop():
        raise RuntimeError(
            "Interactive browser login is only available in the ND3X desktop app "
            "(the loopback redirect needs the browser on the same machine as the backend). "
            "Use device code / service principal / bearer token for server deployments."
        )
    try:
        import msal  # added to requirements for the desktop interactive flow
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("msal is not installed — cannot do interactive login.") from e
    from component.runtime_paths import nd3x_home

    cache_path = nd3x_home() / "fabric_msal_cache.bin"
    cache = msal.SerializableTokenCache()
    if cache_path.exists():
        cache.deserialize(cache_path.read_text())
    client_id = os.environ.get("ND3X_AAD_CLIENT_ID") or AZURE_CLI_CLIENT_ID
    authority = f"https://login.microsoftonline.com/{agent.tenant_id or 'organizations'}"
    app = msal.PublicClientApplication(client_id, authority=authority, token_cache=cache)
    scopes = [FABRIC_SCOPE]
    result = None
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(scopes, account=accounts[0])
    if not result:
        # Pin the loopback port so it's predictable (host==backend in the desktop app).
        port = int(os.environ.get("ND3X_AAD_REDIRECT_PORT", "8400"))
        result = app.acquire_token_interactive(scopes, prompt="select_account", port=port)
    if cache.has_state_changed:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(cache.serialize())
    if not result or "access_token" not in result:
        raise RuntimeError(f"Interactive login failed: {(result or {}).get('error_description') or result}")
    return result["access_token"]


async def get_token(agent: FabricDataAgent) -> str:
    method = (agent.auth_method or "azure_login").strip()
    if method == "service_principal":
        return await _token_service_principal(agent)
    if method == "azure_login":
        return await _token_azure_login()
    if method == "interactive_browser":
        # msal interactive opens a browser + runs a blocking local server → off-thread.
        return await asyncio.to_thread(_token_interactive_browser_sync, agent)
    if method == "bearer_token":
        if not agent.bearer_token_encrypted:
            raise RuntimeError("No bearer token stored for this agent.")
        return decrypt_value(agent.bearer_token_encrypted)
    raise RuntimeError(f"Unknown auth_method: {method!r}")


# ── querying (the only Fabric-wire-specific part — validate against a real agent) ─
def _first_assistant_text(messages_payload: Dict[str, Any]) -> str:
    for msg in (messages_payload.get("data") or []):
        if msg.get("role") != "assistant":
            continue
        parts = msg.get("content") or []
        texts = []
        for p in parts:
            if isinstance(p, dict) and p.get("type") == "text":
                texts.append(((p.get("text") or {}).get("value")) or "")
            elif isinstance(p, str):
                texts.append(p)
        if texts:
            return "\n".join(t for t in texts if t).strip()
    return ""


def _extract_steps(steps_payload: Dict[str, Any]) -> List[str]:
    """Pull the SQL/DAX/tool calls the Data Agent ran, for a collapsible trace."""
    steps: List[str] = []
    for s in (steps_payload.get("data") or []):
        details = s.get("step_details") or {}
        for tc in (details.get("tool_calls") or []):
            for key in ("input", "query", "code"):
                val = tc.get(key)
                if isinstance(val, str) and val.strip():
                    steps.append(val.strip())
            fn = tc.get("function") or {}
            if isinstance(fn.get("arguments"), str) and fn["arguments"].strip():
                steps.append(fn["arguments"].strip())
    return steps


async def ask(agent: FabricDataAgent, question: str) -> Dict[str, Any]:
    """Query a Data Agent. Returns {answer, steps, status}. Raises on hard failure."""
    token = await get_token(agent)
    base = (
        f"{FABRIC_RESOURCE}/v1/workspaces/{agent.workspace_id}"
        f"/aiskills/{agent.data_agent_id}/aiassistant/openai"
    )
    params = {"api-version": (agent.api_version or DEFAULT_API_VERSION)}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "OpenAI-Beta": "assistants=v2",
    }
    import httpx
    async with httpx.AsyncClient(base_url=base, headers=headers, params=params, timeout=120.0) as c:
        # Fabric's published data agent backs an OpenAI "assistant" — create one and
        # use its id for the run (the data-agent/aiskill id is NOT the assistant id).
        # `model` is ignored by Fabric but required by the Assistants schema.
        assistant = (await c.post("/assistants", json={"model": "not used"})).json()
        aid = assistant.get("id")
        if not aid:
            raise RuntimeError(f"Fabric: could not create assistant: {str(assistant)[:300]}")
        thread = (await c.post("/threads", json={})).json()
        tid = thread.get("id")
        if not tid:
            raise RuntimeError(f"Fabric: could not create thread: {str(thread)[:300]}")
        await c.post(f"/threads/{tid}/messages", json={"role": "user", "content": question})
        run = (await c.post(f"/threads/{tid}/runs", json={"assistant_id": aid})).json()
        rid = run.get("id")
        if not rid:
            msg = str((run or {}).get("message") or run)
            if "stage configuration" in msg.lower():
                raise RuntimeError(
                    "Fabric: the Data Agent has no published stage — open it in Fabric and "
                    "Publish it (consumption uses the published stage), then retry."
                )
            raise RuntimeError(f"Fabric: could not start run: {msg[:300]}")
        status = run.get("status")
        for _ in range(90):  # ~3 min @ 2s
            run = (await c.get(f"/threads/{tid}/runs/{rid}")).json()
            status = run.get("status")
            if status in ("completed", "failed", "cancelled", "expired", "requires_action"):
                break
            await asyncio.sleep(2)
        msgs = (await c.get(f"/threads/{tid}/messages")).json()
        answer = _first_assistant_text(msgs)
        steps: List[str] = []
        try:
            steps = _extract_steps((await c.get(f"/threads/{tid}/runs/{rid}/steps")).json())
        except Exception:  # noqa: BLE001 — steps are best-effort
            pass
    if status != "completed" and not answer:
        err = (run.get("last_error") or {}) if isinstance(run, dict) else {}
        detail = err.get("message") or err.get("code") or run.get("incomplete_details") or ""
        raise RuntimeError(
            f"Fabric run ended with status={status!r}"
            + (f": {detail}" if detail else " and no answer (check the agent has data sources and your login has access).")
        )
    return {"answer": answer or "(no answer returned)", "steps": steps, "status": status}


# ── registry CRUD ──────────────────────────────────────────────────────────────
def _to_read(a: FabricDataAgent) -> FabricDataAgentRead:
    return FabricDataAgentRead(
        id=a.id, name=a.name, display_name=a.display_name, description=a.description,
        workspace_id=a.workspace_id, data_agent_id=a.data_agent_id, api_version=a.api_version,
        auth_method=a.auth_method, tenant_id=a.tenant_id, client_id=a.client_id,
        enabled=bool(a.enabled),
        has_secret=bool(a.client_secret_encrypted or a.bearer_token_encrypted),
    )


class FabricDataAgentService:
    def __init__(self, db: Session):
        self.db = db

    def list(self) -> List[FabricDataAgentRead]:
        return [_to_read(a) for a in self.db.query(FabricDataAgent).order_by(FabricDataAgent.id).all()]

    def list_enabled(self) -> List[FabricDataAgent]:
        return self.db.query(FabricDataAgent).filter(FabricDataAgent.enabled == True).all()  # noqa: E712

    def get(self, agent_id: int) -> Optional[FabricDataAgent]:
        return self.db.query(FabricDataAgent).filter(FabricDataAgent.id == agent_id).first()

    def get_by_name(self, name: str) -> Optional[FabricDataAgent]:
        return self.db.query(FabricDataAgent).filter(FabricDataAgent.name == name).first()

    def create(self, data: FabricDataAgentCreate) -> FabricDataAgentRead:
        if (data.auth_method or "") not in FABRIC_AUTH_METHODS:
            raise ValueError(f"auth_method must be one of {FABRIC_AUTH_METHODS}")
        obj = FabricDataAgent(
            name=data.name, display_name=data.display_name, description=data.description,
            workspace_id=data.workspace_id, data_agent_id=data.data_agent_id,
            api_version=data.api_version, auth_method=data.auth_method,
            tenant_id=data.tenant_id, client_id=data.client_id, enabled=data.enabled,
            client_secret_encrypted=encrypt_value(data.client_secret) if data.client_secret else None,
            bearer_token_encrypted=encrypt_value(data.bearer_token) if data.bearer_token else None,
        )
        self.db.add(obj)
        self.db.commit()
        self.db.refresh(obj)
        return _to_read(obj)

    def update(self, agent_id: int, data: FabricDataAgentUpdate) -> Optional[FabricDataAgentRead]:
        obj = self.get(agent_id)
        if obj is None:
            return None
        fields = data.model_dump(exclude_unset=True)
        if "client_secret" in fields:
            cs = fields.pop("client_secret")
            obj.client_secret_encrypted = encrypt_value(cs) if cs else None
        if "bearer_token" in fields:
            bt = fields.pop("bearer_token")
            obj.bearer_token_encrypted = encrypt_value(bt) if bt else None
        for k, v in fields.items():
            setattr(obj, k, v)
        self.db.commit()
        self.db.refresh(obj)
        return _to_read(obj)

    def delete(self, agent_id: int) -> bool:
        obj = self.get(agent_id)
        if obj is None:
            return False
        self.db.delete(obj)
        self.db.commit()
        return True
