from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from component.logging import get_logger

log = get_logger(__name__)

_PARSE_TIMEOUT = 20.0
_SP_TIMEOUT = 30.0
_AZ_DIR = Path(os.environ.get("AZURE_CONFIG_DIR", Path.home() / ".azure"))


# ── Session state ─────────────────────────────────────────────────────────────

class AzLoginSession:
    def __init__(self):
        self.status: str = "idle"          # idle | pending | authenticated | failed
        self.method: Optional[str] = None  # device_code | service_principal | token_import
        self.url: Optional[str] = None
        self.code: Optional[str] = None
        self.message: str = ""
        self.started_at: Optional[str] = None
        self._proc: Optional[asyncio.subprocess.Process] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "method": self.method,
            "url": self.url,
            "code": self.code,
            "message": self.message,
            "started_at": self.started_at,
        }

    def clear(self) -> None:
        if self._proc:
            try:
                self._proc.kill()
            except Exception:
                pass
        self.__init__()


# ── Service ───────────────────────────────────────────────────────────────────

class AzLoginService:
    def __init__(self):
        self._session = AzLoginSession()

    def get_status(self) -> Dict[str, Any]:
        return self._session.as_dict()

    def clear(self) -> None:
        self._session.clear()

    # ── Device code flow ──────────────────────────────────────────────────────

    async def start_device_code(self) -> Dict[str, Any]:
        """
        Start az login --use-device-code.
        Retourneert URL en code zodra az CLI ze heeft gegenereerd.
        Poll get_status() totdat status == "authenticated" of "failed".
        """
        if self._session.status == "pending":
            log.debugx("Az login al in progress, bestaande sessie teruggeven")
            return {
                "url": self._session.url,
                "code": self._session.code,
                "message": "Login al in progress",
            }

        self._session.clear()
        self._session.started_at = datetime.now(timezone.utc).isoformat()
        self._session.status = "pending"
        self._session.method = "device_code"
        self._session.message = "Starten..."

        log.infox("Az login device code flow starten")

        proc = await asyncio.create_subprocess_shell(
            "az login --use-device-code",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._session._proc = proc

        url_code_event = asyncio.Event()
        asyncio.ensure_future(self._watch_device_output(proc, url_code_event))
        asyncio.ensure_future(self._watch_exit(proc))

        try:
            await asyncio.wait_for(url_code_event.wait(), timeout=_PARSE_TIMEOUT)
        except asyncio.TimeoutError:
            self._session.status = "failed"
            self._session.message = "Timeout: az CLI gaf geen device code terug"
            raise RuntimeError(self._session.message)

        return {"url": self._session.url, "code": self._session.code}

    # ── Service principal flow ────────────────────────────────────────────────

    async def start_service_principal(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
    ) -> Dict[str, Any]:
        """
        Login met een service principal via client secret.
        Retourneert direct zodra de login geslaagd of mislukt is — geen polling nodig.
        """
        if self._session.status == "pending":
            raise RuntimeError("Er is al een login sessie actief. Reset eerst via clear().")

        if not tenant_id or not client_id or not client_secret:
            raise ValueError("tenant_id, client_id en client_secret zijn allemaal verplicht")

        self._session.clear()
        self._session.started_at = datetime.now(timezone.utc).isoformat()
        self._session.status = "pending"
        self._session.method = "service_principal"
        self._session.message = "Service principal login starten..."

        log.infox("Az login service principal starten", tenant_id=tenant_id, client_id=client_id)

        command = (
            f"az login --service-principal "
            f"--tenant {tenant_id} "
            f"--username {client_id} "
            f"--password {client_secret}"
        )

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._session._proc = proc

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=_SP_TIMEOUT,
            )

            stdout = stdout_bytes.decode(errors="replace").strip()
            stderr = stderr_bytes.decode(errors="replace").strip()

            if proc.returncode == 0:
                self._session.status = "authenticated"
                self._session.message = "Service principal succesvol ingelogd"
                log.infox("Az service principal login geslaagd", tenant_id=tenant_id)
                return {"status": "authenticated", "message": self._session.message}
            else:
                error_msg = stderr or stdout or f"Exit code {proc.returncode}"
                self._session.status = "failed"
                self._session.message = f"Service principal login mislukt: {error_msg}"
                log.warningx("Az service principal login mislukt", tenant_id=tenant_id, error=error_msg)
                raise RuntimeError(self._session.message)

        except asyncio.TimeoutError:
            self._session.status = "failed"
            self._session.message = f"Service principal login timed out na {_SP_TIMEOUT}s"
            raise RuntimeError(self._session.message)

    # ── Token import flow ─────────────────────────────────────────────────────

    def import_token_files(
        self,
        msal_token_cache: Any,
        azure_profile: Any,
        service_principal_entries: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Importeert ~/.azure bestanden die lokaal zijn gegenereerd via 'az login'.

        Werkwijze op de lokale machine:
            az login
            cat ~/.azure/msal_token_cache.json
            cat ~/.azure/azureProfile.json
            cat ~/.azure/service_principal_entries.json  # optioneel

        Stuur die inhoud naar dit endpoint — de container is daarna ingelogd
        met hetzelfde account als lokaal.

        De refresh token in msal_token_cache zorgt ervoor dat az automatisch
        nieuwe access tokens ophaalt zolang de sessie geldig is.
        """
        _AZ_DIR.mkdir(parents=True, exist_ok=True)

        written = []

        def _write(filename: str, content: Any) -> None:
            path = _AZ_DIR / filename
            if isinstance(content, str):
                path.write_text(content, encoding="utf-8")
            else:
                path.write_text(json.dumps(content, indent=2), encoding="utf-8")
            written.append(filename)
            log.infox("Az token bestand geschreven", filename=filename, path=str(path))

        _write("msal_token_cache.json", msal_token_cache)
        _write("azureProfile.json", azure_profile)

        if service_principal_entries is not None:
            _write("service_principal_entries.json", service_principal_entries)

        # Sessie markeren als authenticated
        self._session.clear()
        self._session.started_at = datetime.now(timezone.utc).isoformat()
        self._session.status = "authenticated"
        self._session.method = "token_import"
        self._session.message = f"Token bestanden geïmporteerd: {', '.join(written)}"

        log.infox("Az login token import geslaagd", written=written)

        return {
            "status": "authenticated",
            "method": "token_import",
            "files_written": written,
            "message": self._session.message,
        }

    # ── Backwards compat ──────────────────────────────────────────────────────

    async def start(self) -> Dict[str, Any]:
        """Backwards compatibel — roept device code flow aan."""
        return await self.start_device_code()

    # ── Interne helpers ───────────────────────────────────────────────────────

    async def _watch_device_output(self, proc, event: asyncio.Event) -> None:
        url_re = re.compile(r"https://[^\s]*devicelogin[^\s]*", re.IGNORECASE)
        code_re = re.compile(r"enter the code ([A-Z0-9]{6,10})", re.IGNORECASE)

        async def scan(stream):
            if stream is None:
                return
            async for raw in stream:
                line = raw.decode(errors="replace")
                log.debugx("Az login output", line=line.rstrip())
                if not self._session.url:
                    m = url_re.search(line)
                    if m:
                        self._session.url = m.group(0).rstrip(".")
                if not self._session.code:
                    m = code_re.search(line)
                    if m:
                        self._session.code = m.group(1)
                if self._session.url and self._session.code and not event.is_set():
                    self._session.message = "Wachten op gebruiker authenticatie..."
                    log.infox("Az login device code beschikbaar", url=self._session.url, code=self._session.code)
                    event.set()

        await asyncio.gather(scan(proc.stdout), scan(proc.stderr))

    async def _watch_exit(self, proc) -> None:
        code = await proc.wait()
        if code == 0:
            self._session.status = "authenticated"
            self._session.message = "Succesvol ingelogd"
            log.infox("Az login succesvol afgerond")
        elif self._session.status == "pending":
            self._session.status = "failed"
            self._session.message = f"Az login mislukt met exit code {code}"
            log.warningx("Az login mislukt", exit_code=code)
