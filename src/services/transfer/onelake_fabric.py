"""Fabric/OneLake helpers: mint AAD tokens (per scope), introspect a token's
identity (so you can confirm WHICH account/tenant it is — e.g. Swinkels vs
Beeminds), and list workspaces + lakehouses for the OneLake path pickers.

Tokens: a stored bearer (scope-specific) → an OAUTH service principal
(client-credentials) → else the host's `az login`. Fabric REST needs the Fabric
scope; OneLake file IO needs the Storage scope.
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
from typing import Any, Dict, List

import httpx

FABRIC_API = "https://api.fabric.microsoft.com/v1"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
FABRIC_RESOURCE = "https://api.fabric.microsoft.com"
STORAGE_SCOPE = "https://storage.azure.com/.default"
STORAGE_RESOURCE = "https://storage.azure.com"


def mint_token(secrets: Dict[str, Any], *, scope: str, resource: str) -> str:
    if secrets.get("token"):
        return secrets["token"]
    tenant, client, sec = secrets.get("tenant_id"), secrets.get("client_id"), secrets.get("client_secret")
    if tenant and client and sec:
        import msal
        app = msal.ConfidentialClientApplication(
            client, authority=f"https://login.microsoftonline.com/{tenant}", client_credential=sec)
        r = app.acquire_token_for_client(scopes=[scope])
        if "access_token" not in r:
            raise RuntimeError(f"token failed: {r.get('error_description') or r}")
        return r["access_token"]
    if not shutil.which("az"):
        raise RuntimeError("No credential attached and the Azure CLI isn't available — run 'az login' or attach an OAUTH credential.")
    p = subprocess.run(["az", "account", "get-access-token", "--resource", resource, "--query", "accessToken", "-o", "tsv"],
                       capture_output=True, text=True, timeout=60)
    out = (p.stdout or "").strip()
    if p.returncode != 0 or not out:
        raise RuntimeError("az token failed — run 'az login'. " + (p.stderr or "")[:160])
    return out


def token_identity(token: str) -> Dict[str, Any]:
    """Decode (not validate) the JWT claims to show whose token it is."""
    try:
        seg = token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        c = json.loads(base64.urlsafe_b64decode(seg))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"could not decode token: {e}"}
    return {
        "ok": True,
        "tenant_id": c.get("tid"),
        "identity": c.get("upn") or c.get("unique_name") or c.get("email") or c.get("appid") or c.get("oid"),
        "name": c.get("name"),
        "app_id": c.get("appid"),
        "audience": c.get("aud"),
        "expires": c.get("exp"),
    }


def identity(secrets: Dict[str, Any]) -> Dict[str, Any]:
    """Identity of the Storage-scope token (what OneLake transfers actually use)."""
    return token_identity(mint_token(secrets, scope=STORAGE_SCOPE, resource=STORAGE_RESOURCE))


def list_workspaces(secrets: Dict[str, Any]) -> List[dict]:
    t = mint_token(secrets, scope=FABRIC_SCOPE, resource=FABRIC_RESOURCE)
    r = httpx.get(f"{FABRIC_API}/workspaces", headers={"Authorization": f"Bearer {t}"}, timeout=30)
    r.raise_for_status()
    return [{"id": w.get("id"), "name": w.get("displayName")} for w in r.json().get("value", [])]


def list_lakehouses(secrets: Dict[str, Any], workspace_id: str) -> List[dict]:
    t = mint_token(secrets, scope=FABRIC_SCOPE, resource=FABRIC_RESOURCE)
    r = httpx.get(f"{FABRIC_API}/workspaces/{workspace_id}/lakehouses", headers={"Authorization": f"Bearer {t}"}, timeout=30)
    r.raise_for_status()
    return [{"id": i.get("id"), "name": i.get("displayName")} for i in r.json().get("value", [])]
