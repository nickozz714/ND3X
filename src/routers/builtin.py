"""
routers/builtin.py

Endpoints voor az login flow en losse shell commando's.
Registreer in routers/__init__.py:

    from routers.builtin import router as builtin_router
    all_routers = [..., builtin_router]
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from services.assistants.ask_job_callbacks import az_login_service
from services.shell.shell_exec_service import exec_command

router = APIRouter(prefix="/builtin", tags=["Builtin"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class ShellExecRequest(BaseModel):
    command: str = Field(..., description="Het bash commando om uit te voeren")
    timeout: Optional[float] = Field(60.0, description="Timeout in seconden")


class AzLoginServicePrincipalRequest(BaseModel):
    tenant_id: str = Field(..., description="Azure tenant ID (Directory ID)")
    client_id: str = Field(..., description="Application (client) ID van de app registration")
    client_secret: str = Field(..., description="Client secret van de app registration")


class AzLoginTokenImportRequest(BaseModel):
    msal_token_cache: Any = Field(
        ...,
        description="Inhoud van ~/.azure/msal_token_cache.json van je lokale machine",
    )
    azure_profile: Any = Field(
        ...,
        description="Inhoud van ~/.azure/azureProfile.json van je lokale machine",
    )
    service_principal_entries: Optional[Any] = Field(
        default=None,
        description="Optioneel: inhoud van ~/.azure/service_principal_entries.json",
    )


# ── Az login — device code ────────────────────────────────────────────────────

@router.post("/az-login", summary="Start Azure device code login")
async def start_az_login() -> Dict[str, Any]:
    """
    Start az login --use-device-code.
    Retourneert de URL en code zodra az CLI ze heeft gegenereerd.
    Poll GET /builtin/az-login/status totdat status == authenticated of failed.
    """
    result = await az_login_service.start_device_code()
    return {
        **result,
        "method": "device_code",
        "instructions": (
            "Open de URL op een willekeurig apparaat, voer de code in en log in. "
            "Poll GET /builtin/az-login/status om te controleren of de login geslaagd is."
        ),
    }


# ── Az login — service principal ──────────────────────────────────────────────

@router.post("/az-login/service-principal", summary="Login met Azure service principal")
async def start_az_login_service_principal(
    body: AzLoginServicePrincipalRequest,
) -> Dict[str, Any]:
    """
    Login met een service principal via client secret.
    Retourneert direct — geen polling nodig.

    App registration aanmaken:
      az ad sp create-for-rbac --name "mijn-app" --role Contributor --scopes /subscriptions/<id>
    """
    result = await az_login_service.start_service_principal(
        tenant_id=body.tenant_id,
        client_id=body.client_id,
        client_secret=body.client_secret,
    )
    return result


# ── Az login — token import ───────────────────────────────────────────────────

@router.post("/az-login/import", summary="Importeer lokale az login token bestanden")
def import_az_login_tokens(body: AzLoginTokenImportRequest) -> Dict[str, Any]:
    """
    Importeert de ~/.azure bestanden van je lokale machine naar de container.
    Gebruik dit als device code login geblokkeerd is door conditional access.

    Werkwijze op je lokale machine:
      1. az login
      2. Kopieer de inhoud van de onderstaande bestanden naar dit endpoint:
           ~/.azure/msal_token_cache.json
           ~/.azure/azureProfile.json

    Met een klein hulpscript:
      curl -X POST https://jouw-server/api/builtin/az-login/import
        -H "Content-Type: application/json"
        -d "{
          \\"msal_token_cache\\": $(cat ~/.azure/msal_token_cache.json),
          \\"azure_profile\\": $(cat ~/.azure/azureProfile.json)
        }"
    """
    try:
        result = az_login_service.import_token_files(
            msal_token_cache=body.msal_token_cache,
            azure_profile=body.azure_profile,
            service_principal_entries=body.service_principal_entries,
        )
        return result
    except Exception as err:
        raise HTTPException(status_code=500, detail=str(err))


# ── Az login — status & reset ─────────────────────────────────────────────────

@router.get("/az-login/status", summary="Poll Azure login status")
def get_az_login_status() -> Dict[str, Any]:
    """
    Retourneert de huidige status van de az login flow.
    status: idle | pending | authenticated | failed
    method: device_code | service_principal | token_import | null
    """
    return az_login_service.get_status()


@router.delete("/az-login", summary="Reset Azure login sessie")
def clear_az_login() -> Dict[str, Any]:
    """Verwijdert de huidige login sessie zodat een nieuwe gestart kan worden."""
    az_login_service.clear()
    return {"cleared": True}


# ── Shell exec ────────────────────────────────────────────────────────────────

@router.post("/shell/exec", summary="Voer een los bash commando uit")
async def shell_exec(body: ShellExecRequest) -> Dict[str, Any]:
    """
    Voert een enkel bash commando uit in de container.
    Retourneert stdout, stderr en exit_code.
    """
    result = await exec_command(body.command, timeout=body.timeout or 60.0)
    return result
