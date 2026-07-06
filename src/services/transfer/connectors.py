"""Transfer connectors: a pluggable read/write abstraction over storage backends
(the Python equivalent of Camel components). Register a connector for a protocol;
the engine resolves it per endpoint at run time.

Common contract: test_connection / read / write (+ optional list). This is also
the seam the future LLM-driven/self-extending connectors will plug into.
"""
from __future__ import annotations

import io
import posixpath
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional, Type

# protocol -> connector class
CONNECTORS: Dict[str, Type["Connector"]] = {}


def register(protocol: str):
    def deco(cls: Type["Connector"]):
        cls.protocol = protocol
        CONNECTORS[protocol] = cls
        return cls
    return deco


class Connector(ABC):
    protocol: str = ""
    #: schema fields this connector needs (drives forms/LLM later)
    fields: List[str] = ["path"]
    credential_type: Optional[str] = None

    def __init__(self, *, hostname: Optional[str] = None, port: Optional[int] = None,
                 path: Optional[str] = None, params: Optional[dict] = None,
                 secrets: Optional[dict] = None):
        self.hostname = hostname
        self.port = port
        self.path = path
        self.params = params or {}
        self.secrets = secrets or {}

    @abstractmethod
    def test_connection(self) -> None:
        """Raise on failure; return None on success."""

    @abstractmethod
    def read(self, path: str) -> bytes: ...

    @abstractmethod
    def write(self, path: str, data: bytes) -> None: ...

    def list(self, path: str) -> List[str]:  # optional
        raise NotImplementedError(f"{self.protocol} connector does not support list()")

    def exists(self, path: str) -> bool:  # optional (used by the poller)
        raise NotImplementedError(f"{self.protocol} connector does not support exists()")

    def delete(self, path: str) -> None:  # optional (move-after-transfer)
        raise NotImplementedError(f"{self.protocol} connector does not support delete()")

    def is_directory(self, path: str) -> bool:  # optional (multi-file transfers)
        return False

    def iter_files(self, path: str) -> List[str]:
        """Files to transfer for a source path: a single file → [path]; a directory
        → each file under it. Default treats the path as a single file."""
        return [path]

    def browse(self, path: str) -> List[dict]:
        """List a directory for the UI path browser: [{name, path, is_dir}]. Default
        is unsupported (the user types the path)."""
        raise NotImplementedError(f"{self.protocol} connector does not support browse()")

    def close(self) -> None:
        pass


@register("file")
class LocalFileConnector(Connector):
    """Local filesystem (also used as the simplest test target)."""
    fields = ["path"]

    def test_connection(self) -> None:
        base = Path(self.path or ".").expanduser()
        d = base if base.is_dir() else base.parent
        if not d.exists():
            raise RuntimeError(f"Path not found: {d}")

    def read(self, path: str) -> bytes:
        return Path(path).expanduser().read_bytes()

    def write(self, path: str, data: bytes) -> None:
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def list(self, path: str) -> List[str]:
        return [str(p) for p in Path(path).expanduser().iterdir()]

    def exists(self, path: str) -> bool:
        return Path(path).expanduser().exists()

    def delete(self, path: str) -> None:
        Path(path).expanduser().unlink(missing_ok=True)

    def is_directory(self, path: str) -> bool:
        return Path(path).expanduser().is_dir()

    def iter_files(self, path: str) -> List[str]:
        p = Path(path).expanduser()
        if p.is_dir():
            return [str(x) for x in sorted(p.iterdir()) if x.is_file()]
        return [str(p)] if p.exists() else []

    def browse(self, path: str) -> List[dict]:
        base = Path(path or ".").expanduser()
        if not base.is_dir():
            base = base.parent if base.parent.exists() else Path.home()
        return [{"name": p.name, "path": str(p), "is_dir": p.is_dir()} for p in sorted(base.iterdir())]


@register("sftp")
class SftpConnector(Connector):
    """SFTP via paramiko. Auth: username+password or username+private_key(+key_phrase)."""
    fields = ["path"]
    credential_type = "SFTP"

    def _connect(self):
        import paramiko  # lazy: keeps the module importable without paramiko
        port = int(self.port or 22)
        if not self.hostname:
            raise RuntimeError("SFTP endpoint needs a host")
        pkey = None
        pk = self.secrets.get("private_key")
        if pk:
            pkey = paramiko.RSAKey.from_private_key(io.StringIO(pk), password=self.secrets.get("key_phrase") or None)
        transport = paramiko.Transport((self.hostname, port))
        transport.connect(username=self.secrets.get("username"),
                          password=self.secrets.get("password"), pkey=pkey)
        return transport, paramiko.SFTPClient.from_transport(transport)

    def test_connection(self) -> None:
        transport, sftp = self._connect()
        try:
            sftp.listdir(posixpath.dirname(self.path or "") or ".")
        finally:
            transport.close()

    def read(self, path: str) -> bytes:
        transport, sftp = self._connect()
        try:
            with sftp.open(path, "rb") as f:
                return f.read()
        finally:
            transport.close()

    def write(self, path: str, data: bytes) -> None:
        transport, sftp = self._connect()
        try:
            self._mkdirs(sftp, posixpath.dirname(path))
            with sftp.open(path, "wb") as f:
                f.write(data)
        finally:
            transport.close()

    def list(self, path: str) -> List[str]:
        transport, sftp = self._connect()
        try:
            return sftp.listdir(path or ".")
        finally:
            transport.close()

    def exists(self, path: str) -> bool:
        transport, sftp = self._connect()
        try:
            sftp.stat(path)
            return True
        except IOError:
            return False
        finally:
            transport.close()

    def delete(self, path: str) -> None:
        transport, sftp = self._connect()
        try:
            sftp.remove(path)
        finally:
            transport.close()

    def is_directory(self, path: str) -> bool:
        import stat as _stat
        transport, sftp = self._connect()
        try:
            return _stat.S_ISDIR(sftp.stat(path).st_mode)
        except IOError:
            return False
        finally:
            transport.close()

    def iter_files(self, path: str) -> List[str]:
        import stat as _stat
        transport, sftp = self._connect()
        try:
            try:
                attrs = sftp.listdir_attr(path)
            except IOError:
                return [path]  # not a directory → single file
            return [posixpath.join(path, a.filename) for a in attrs if not _stat.S_ISDIR(a.st_mode)]
        finally:
            transport.close()

    def browse(self, path: str) -> List[dict]:
        import stat as _stat
        transport, sftp = self._connect()
        try:
            base = (path or ".").rstrip("/")
            attrs = sftp.listdir_attr(base or ".")
            return [{"name": a.filename,
                     "path": posixpath.join(base, a.filename) if base and base != "." else a.filename,
                     "is_dir": _stat.S_ISDIR(a.st_mode)} for a in attrs]
        finally:
            transport.close()

    @staticmethod
    def _mkdirs(sftp, directory: str) -> None:
        if not directory:
            return
        parts, cur = directory.strip("/").split("/"), ""
        for part in parts:
            cur = f"{cur}/{part}" if cur else f"/{part}"
            try:
                sftp.stat(cur)
            except IOError:
                try:
                    sftp.mkdir(cur)
                except IOError:
                    pass


def _azure_account(hostname: Optional[str], secrets: dict) -> str:
    account = (hostname or secrets.get("username") or "").strip()
    if not account:
        raise RuntimeError("Azure storage needs the account name (set it as the host hostname).")
    return account.split(".")[0]  # accept either bare name or a full domain


@register("azure-storage-blob")
class AzureBlobConnector(Connector):
    """Azure Blob. path = '<container>/<blob/path>'. Cred: ACCESS_KEY (account key in
    password) or SAS_TOKEN (token)."""
    fields = ["path"]  # path = container/blob
    credential_type = "ACCESS_KEY"

    def _service(self):
        from azure.storage.blob import BlobServiceClient
        account = _azure_account(self.hostname, self.secrets)
        url = f"https://{account}.blob.core.windows.net"
        credential = self.secrets.get("token") or self.secrets.get("password")
        if not credential:
            raise RuntimeError("Azure Blob needs an account key (ACCESS_KEY) or SAS token (SAS_TOKEN).")
        return BlobServiceClient(account_url=url, credential=credential)

    @staticmethod
    def _split(path: str):
        p = (path or "").lstrip("/")
        container, _, blob = p.partition("/")
        if not container or not blob:
            raise RuntimeError("Azure Blob path must be '<container>/<blob/path>'.")
        return container, blob

    def _blob(self, path: str):
        c, b = self._split(path)
        return self._service().get_blob_client(container=c, blob=b)

    def test_connection(self) -> None:
        c, _ = self._split(self.path or "")
        self._service().get_container_client(c).get_container_properties()

    def read(self, path: str) -> bytes:
        return self._blob(path).download_blob().readall()

    def write(self, path: str, data: bytes) -> None:
        self._blob(path).upload_blob(data, overwrite=True)


@register("azure-files")
class AzureFileShareConnector(Connector):
    """Azure File Share. path = '<share>/<dir>/<file>'. Cred: ACCESS_KEY or SAS_TOKEN."""
    fields = ["path"]
    credential_type = "ACCESS_KEY"

    def _file_client(self, path: str):
        from azure.storage.fileshare import ShareServiceClient
        account = _azure_account(self.hostname, self.secrets)
        url = f"https://{account}.file.core.windows.net"
        credential = self.secrets.get("token") or self.secrets.get("password")
        if not credential:
            raise RuntimeError("Azure Files needs an account key (ACCESS_KEY) or SAS token (SAS_TOKEN).")
        p = (path or "").lstrip("/")
        share, _, rel = p.partition("/")
        if not share or not rel:
            raise RuntimeError("Azure Files path must be '<share>/<dir>/<file>'.")
        svc = ShareServiceClient(account_url=url, credential=credential)
        return svc.get_share_client(share).get_file_client(rel)

    def test_connection(self) -> None:
        from azure.storage.fileshare import ShareServiceClient
        account = _azure_account(self.hostname, self.secrets)
        credential = self.secrets.get("token") or self.secrets.get("password")
        share = (self.path or "").lstrip("/").partition("/")[0]
        if not share:
            raise RuntimeError("Azure Files path must start with a share name.")
        svc = ShareServiceClient(account_url=f"https://{account}.file.core.windows.net", credential=credential)
        svc.get_share_client(share).get_share_properties()

    def read(self, path: str) -> bytes:
        return self._file_client(path).download_file().readall()

    def write(self, path: str, data: bytes) -> None:
        self._file_client(path).upload_file(data)


@register("sharepoint")
class SharePointConnector(Connector):
    """SharePoint via Microsoft Graph (client-credentials). path = 'siteId/driveId/rel/path'.
    Cred: OAUTH (tenant_id + client_id + client_secret)."""
    fields = ["path"]
    credential_type = "OAUTH"
    GRAPH = "https://graph.microsoft.com/v1.0"

    def _token(self) -> str:
        import msal
        tenant = self.secrets.get("tenant_id")
        client_id = self.secrets.get("client_id")
        secret = self.secrets.get("client_secret")
        if not (tenant and client_id and secret):
            raise RuntimeError("SharePoint needs OAUTH credential: tenant_id, client_id, client_secret.")
        app = msal.ConfidentialClientApplication(
            client_id, authority=f"https://login.microsoftonline.com/{tenant}", client_credential=secret)
        result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in result:
            raise RuntimeError(f"SharePoint token failed: {result.get('error_description') or result}")
        return result["access_token"]

    @staticmethod
    def _parse(path: str):
        parts = (path or "").lstrip("/").split("/", 2)
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise RuntimeError("SharePoint path must be 'siteId/driveId[/rel/path]'.")
        return parts[0], parts[1], (parts[2] if len(parts) > 2 else "")

    def _headers(self):
        return {"Authorization": f"Bearer {self._token()}"}

    def test_connection(self) -> None:
        import httpx
        site, drive, _ = self._parse(self.path or "")
        r = httpx.get(f"{self.GRAPH}/drives/{drive}/root", headers=self._headers(), timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"SharePoint drive check failed ({r.status_code}): {r.text[:200]}")

    def read(self, path: str) -> bytes:
        import httpx
        _, drive, rel = self._parse(path)
        r = httpx.get(f"{self.GRAPH}/drives/{drive}/root:/{rel}:/content",
                      headers=self._headers(), timeout=120, follow_redirects=True)
        if r.status_code >= 400:
            raise RuntimeError(f"SharePoint read failed ({r.status_code}): {r.text[:200]}")
        return r.content

    def write(self, path: str, data: bytes) -> None:
        import httpx
        _, drive, rel = self._parse(path)
        # Simple upload (≤ ~4 MB). Large files would need an upload session (TODO).
        r = httpx.put(f"{self.GRAPH}/drives/{drive}/root:/{rel}:/content",
                      headers={**self._headers(), "Content-Type": "application/octet-stream"},
                      content=data, timeout=120)
        if r.status_code >= 400:
            raise RuntimeError(f"SharePoint write failed ({r.status_code}): {r.text[:200]}")


class FsspecConnector(Connector):
    """Generic connector over any fsspec filesystem — this is how new locations get
    added with NO new code: register this class under an fsspec protocol (s3, gcs,
    abfs, http, …) and it works. storage_options come from the credential + the
    endpoint's defined parameters. path is the backend path (e.g. 's3': 'bucket/key').
    """
    fields = ["path"]
    credential_type = "ACCESS_KEY"

    def _opts(self) -> dict:
        opts = dict(self.params)  # defined parameters first
        s, p = self.secrets, self.protocol
        if p in ("s3", "s3a"):
            if s.get("username"): opts.setdefault("key", s["username"])
            if s.get("password"): opts.setdefault("secret", s["password"])
            if s.get("token"): opts.setdefault("token", s["token"])
        elif p in ("gcs", "gs"):
            if s.get("token"): opts.setdefault("token", s["token"])
        elif p in ("abfs", "az", "adl"):
            if s.get("username"): opts.setdefault("account_name", s["username"])
            if s.get("password"): opts.setdefault("account_key", s["password"])
            if s.get("token"): opts.setdefault("sas_token", s["token"])
        return opts

    def _fs(self):
        import fsspec
        return fsspec.filesystem(self.protocol, **self._opts())

    def test_connection(self) -> None:
        fs = self._fs()
        p = (self.path or "").strip()
        try:
            fs.ls(p)
        except Exception:  # noqa: BLE001 — path may be a file or not exist; existence check is enough
            fs.exists(p)

    def read(self, path: str) -> bytes:
        return self._fs().cat_file(path)

    def write(self, path: str, data: bytes) -> None:
        fs = self._fs()
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        if parent:
            try:
                fs.makedirs(parent, exist_ok=True)
            except Exception:  # noqa: BLE001 — object stores have no real dirs
                pass
        fs.pipe_file(path, data)

    def exists(self, path: str) -> bool:
        return self._fs().exists(path)

    def delete(self, path: str) -> None:
        self._fs().rm(path)

    def is_directory(self, path: str) -> bool:
        try:
            return self._fs().isdir(path)
        except Exception:  # noqa: BLE001
            return False

    def iter_files(self, path: str) -> List[str]:
        fs = self._fs()
        try:
            if fs.isdir(path):
                return [f for f in fs.ls(path, detail=False) if fs.isfile(f)]
        except Exception:  # noqa: BLE001
            pass
        return [path]

    def browse(self, path: str) -> List[dict]:
        fs = self._fs()
        out = []
        for e in fs.ls(path or "", detail=True):
            name = str(e.get("name", "")).rstrip("/").split("/")[-1]
            out.append({"name": name, "path": e.get("name"), "is_dir": e.get("type") == "directory"})
        return out


# Register the fsspec connector for every backend whose package is installed, so
# "new location" = a configured connector instance, no code. s3fs ships with us;
# gcs/abfs/http light up automatically once their backend is pip-installed.
CONNECTORS["s3"] = FsspecConnector
for _proto, _mod in [("gcs", "gcsfs"), ("gs", "gcsfs"), ("abfs", "adlfs"), ("az", "adlfs"),
                     ("http", "aiohttp"), ("https", "aiohttp")]:
    try:
        __import__(_mod)
        CONNECTORS[_proto] = FsspecConnector
    except Exception:  # noqa: BLE001 — backend not installed → don't offer it
        pass


@register("onelake")
class OneLakeConnector(Connector):
    """Microsoft Fabric OneLake via the ADLS Gen2 (DFS) REST API.
    path = '<workspace>/<item>.<itemtype>/<dir>/<file>' (everything after the host).
    Auth: AAD token in the STORAGE audience (OneLake only accepts that), resolved as:
      1. a stored bearer token, else
      2. OAUTH service principal (tenant_id+client_id+client_secret, client-credentials), else
      3. NO credential attached → reuse the host's `az login` session
         (`az account get-access-token --resource https://storage.azure.com`).
    OneLake does NOT accept SAS / account keys.
    """
    fields = ["path"]
    credential_type = "OAUTH"
    DEFAULT_HOST = "onelake.dfs.fabric.microsoft.com"
    SCOPE = "https://storage.azure.com/.default"
    API_VERSION = "2023-11-03"

    def _base(self) -> str:
        h = (self.hostname or self.DEFAULT_HOST).strip().rstrip("/")
        return h if h.startswith("http") else "https://" + h

    def _token(self) -> str:
        s = self.secrets
        if s.get("token"):
            return s["token"]
        tenant, client, secret = s.get("tenant_id"), s.get("client_id"), s.get("client_secret")
        if tenant and client and secret:
            import msal
            app = msal.ConfidentialClientApplication(
                client, authority=f"https://login.microsoftonline.com/{tenant}", client_credential=secret)
            r = app.acquire_token_for_client(scopes=[self.SCOPE])
            if "access_token" not in r:
                raise RuntimeError(f"OneLake token failed: {r.get('error_description') or r}")
            return r["access_token"]
        # No service principal / bearer token → reuse the host's Azure CLI login
        # (storage audience). Attach no credential to the endpoint to use this.
        return self._az_token()

    @staticmethod
    def _az_token() -> str:
        import shutil
        import subprocess
        if not shutil.which("az"):
            raise RuntimeError(
                "OneLake auth: no credential attached and the Azure CLI (az) isn't on PATH. "
                "Run 'az login', or attach an OAUTH (service principal) credential or a bearer token.")
        try:
            r = subprocess.run(
                ["az", "account", "get-access-token", "--resource", "https://storage.azure.com",
                 "--query", "accessToken", "-o", "tsv"],
                capture_output=True, text=True, timeout=60)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"OneLake az login token failed: {e}")
        out = (r.stdout or "").strip()
        if r.returncode != 0 or not out:
            raise RuntimeError("OneLake az login token failed — run 'az login' first. " + (r.stderr or "")[:200])
        return out

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token()}", "x-ms-version": self.API_VERSION}

    def _url(self, path: str) -> str:
        return self._base() + "/" + path.lstrip("/")

    def test_connection(self) -> None:
        import httpx
        fs = (self.path or "").lstrip("/").split("/", 1)[0]
        if not fs:
            raise RuntimeError("OneLake path must start with a workspace, e.g. 'workspace/item.Lakehouse/Files/...'.")
        r = httpx.get(f"{self._base()}/{fs}?resource=filesystem&recursive=false",
                      headers=self._headers(), timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"OneLake check failed ({r.status_code}): {r.text[:160]}")

    def read(self, path: str) -> bytes:
        import httpx
        r = httpx.get(self._url(path), headers=self._headers(), timeout=120, follow_redirects=True)
        if r.status_code >= 400:
            raise RuntimeError(f"OneLake read failed ({r.status_code}): {r.text[:160]}")
        return r.content

    def write(self, path: str, data: bytes) -> None:
        import httpx
        url = self._url(path)
        h = self._headers()
        # ADLS Gen2: create → append → flush
        r = httpx.put(url, params={"resource": "file"}, headers=h, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f"OneLake create failed ({r.status_code}): {r.text[:160]}")
        r = httpx.patch(url, params={"action": "append", "position": 0},
                        headers={**h, "Content-Type": "application/octet-stream"}, content=data, timeout=120)
        if r.status_code >= 400:
            raise RuntimeError(f"OneLake append failed ({r.status_code}): {r.text[:160]}")
        r = httpx.patch(url, params={"action": "flush", "position": len(data)}, headers=h, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f"OneLake flush failed ({r.status_code}): {r.text[:160]}")

    def delete(self, path: str) -> None:
        import httpx
        httpx.request("DELETE", self._url(path), headers=self._headers(), timeout=60)

    def is_directory(self, path: str) -> bool:
        import httpx
        r = httpx.head(self._url(path), headers=self._headers(), timeout=30)
        return r.headers.get("x-ms-resource-type") == "directory"

    def iter_files(self, path: str) -> List[str]:
        import httpx
        p = path.lstrip("/")
        fs, _, directory = p.partition("/")
        r = httpx.get(f"{self._base()}/{fs}", headers=self._headers(),
                      params={"resource": "filesystem", "recursive": "false", "directory": directory}, timeout=60)
        if r.status_code >= 400:
            return [path]  # treat as a single file
        out = []
        for entry in (r.json().get("paths") or []):
            if str(entry.get("isDirectory", "false")).lower() != "true":
                out.append(f"{fs}/{entry['name']}")
        return out or [path]

    def browse(self, path: str) -> List[dict]:
        import httpx
        p = (path or "").lstrip("/")
        fs, _, directory = p.partition("/")
        if not fs:
            raise NotImplementedError("OneLake browse needs at least a workspace in the path (e.g. 'workspace/item.Lakehouse/Files').")
        r = httpx.get(f"{self._base()}/{fs}", headers=self._headers(),
                      params={"resource": "filesystem", "recursive": "false", "directory": directory}, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f"OneLake browse failed ({r.status_code}): {r.text[:160]}")
        out = []
        for entry in (r.json().get("paths") or []):
            name = str(entry.get("name", "")).rstrip("/").split("/")[-1]
            out.append({"name": name, "path": f"{fs}/{entry['name']}",
                        "is_dir": str(entry.get("isDirectory", "false")).lower() == "true"})
        return out


# ── Tier-2: runtime-defined connector TYPES (declarative, no code-exec) ─────────
# Config for 'rest'-kind connector defs, keyed by protocol (filled by register_def).
CONNECTOR_DEFS: Dict[str, dict] = {}


def _fmt(template: str, **vals) -> str:
    out = template or ""
    for k, v in vals.items():
        out = out.replace("{" + k + "}", str(v if v is not None else ""))
    return out


class RestDefConnector(Connector):
    """A connector configured purely by data (a ConnectorDef of kind 'rest') — the
    safe form of "the LLM adds a new connector type". config (in CONNECTOR_DEFS):
      base_url, read_path, write_path, write_method(=PUT), list_path, delete_path,
      auth_header(=Authorization), auth_template(e.g. 'Bearer {token}'), headers{}.
    Templates may use {path} and credential fields {username}/{password}/{token}/…
    """
    fields = ["path"]

    @property
    def _cfg(self) -> dict:
        return CONNECTOR_DEFS.get(self.protocol, {})

    def _headers(self) -> dict:
        cfg = self._cfg
        h = dict(cfg.get("headers") or {})
        tmpl = cfg.get("auth_template")
        if tmpl:
            h[cfg.get("auth_header") or "Authorization"] = _fmt(tmpl, **self.secrets)
        return h

    def _url(self, template_key: str, path: str) -> str:
        cfg = self._cfg
        base = (cfg.get("base_url") or "").rstrip("/")
        return base + _fmt(cfg.get(template_key) or "/{path}", path=path, **self.secrets)

    def test_connection(self) -> None:
        import httpx
        cfg = self._cfg
        if not cfg.get("base_url"):
            raise RuntimeError(f"connector '{self.protocol}' has no base_url configured")
        url = self._url("list_path" if cfg.get("list_path") else "read_path", self.path or "")
        r = httpx.get(url, headers=self._headers(), timeout=30, follow_redirects=True)
        if r.status_code >= 400:
            raise RuntimeError(f"{self.protocol} test failed ({r.status_code}): {r.text[:160]}")

    def read(self, path: str) -> bytes:
        import httpx
        r = httpx.get(self._url("read_path", path), headers=self._headers(), timeout=120, follow_redirects=True)
        if r.status_code >= 400:
            raise RuntimeError(f"{self.protocol} read failed ({r.status_code}): {r.text[:160]}")
        return r.content

    def write(self, path: str, data: bytes) -> None:
        import httpx
        method = (self._cfg.get("write_method") or "PUT").upper()
        r = httpx.request(method, self._url("write_path", path),
                          headers={**self._headers(), "Content-Type": "application/octet-stream"},
                          content=data, timeout=120)
        if r.status_code >= 400:
            raise RuntimeError(f"{self.protocol} write failed ({r.status_code}): {r.text[:160]}")

    def delete(self, path: str) -> None:
        import httpx
        if not self._cfg.get("delete_path"):
            raise NotImplementedError(f"{self.protocol} has no delete_path configured")
        httpx.request("DELETE", self._url("delete_path", path), headers=self._headers(), timeout=60)


# fsspec protocols whose backend ships / can be enabled by a def
_FSSPEC_KNOWN = {"s3", "s3a", "gcs", "gs", "abfs", "az", "adl", "http", "https", "ftp", "sftp", "memory", "file"}


def register_def(protocol: str, kind: str, config: Optional[dict]) -> None:
    """Register a runtime-defined connector type into the live registry. Declarative
    only: 'fsspec' binds FsspecConnector to the protocol; 'rest' uses RestDefConnector
    with the given config. Raises if the kind/protocol is unusable."""
    protocol = (protocol or "").strip()
    if not protocol:
        raise ValueError("protocol is required")
    if kind == "fsspec":
        # ensure the fsspec backend is importable for this protocol
        import fsspec
        fsspec.get_filesystem_class(protocol)  # raises if no backend
        CONNECTORS[protocol] = FsspecConnector
    elif kind == "rest":
        if not (config or {}).get("base_url"):
            raise ValueError("rest connector needs config.base_url")
        CONNECTOR_DEFS[protocol] = config or {}
        CONNECTORS[protocol] = RestDefConnector
    else:
        raise ValueError("kind must be 'fsspec' or 'rest'")
