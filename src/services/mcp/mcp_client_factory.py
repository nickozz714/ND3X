from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, TYPE_CHECKING

from component.logging import get_logger
from services.mcp.mcp_client import MCPClient

if TYPE_CHECKING:
    from services.mcp.stdio_process_manager import StdioProcessManager
    from services.mcp.builtin_mcp_client import BuiltinMCPClient

log = get_logger(__name__)


class MCPAuthError(ValueError):
    """Raised when MCP auth configuration is invalid."""


@dataclass
class ParsedAuth:
    auth_type: str = "none"
    bearer: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    query_params: Dict[str, str] = field(default_factory=dict)
    cookies: Dict[str, str] = field(default_factory=dict)
    basic_username: str = ""
    basic_password: str = ""
    oauth: Dict[str, Any] = field(default_factory=dict)
    ssh: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        log.debugx(
            "ParsedAuth aangemaakt",
            auth_type=self.auth_type,
            has_bearer=bool(self.bearer),
            header_keys=list(self.headers.keys()),
            query_param_keys=list(self.query_params.keys()),
            cookie_keys=list(self.cookies.keys()),
        )

    def as_dict(self) -> Dict[str, Any]:
        log.debugx("ParsedAuth converteren naar dict gestart", auth_type=self.auth_type)
        return {
            "auth_type": self.auth_type,
            "bearer": self.bearer,
            "headers": self.headers,
            "query_params": self.query_params,
            "cookies": self.cookies,
            "basic_username": self.basic_username,
            "basic_password": self.basic_password,
            "oauth": self.oauth,
            "ssh": self.ssh,
            "metadata": self.metadata,
        }


class MCPClientFactory:
    """
    Bouwt de juiste MCP client op basis van server_type:

      http / sse  → MCPClient        (HTTP transport via fastmcp)
      stdio       → StdioMCPClient   (child process, JSON-RPC via stdin/stdout)
      builtin     → BuiltinMCPClient (ingebouwd: shell scripting + az-login)
    """

    SUPPORTED_AUTH_TYPES = {
        "none", "bearer", "basic", "api_key",
        "oauth_client_credentials", "oauth_authorization_code",
        "oauth_device_code", "ssh_key",
    }

    def __init__(
        self,
        stdio_process_manager: Optional["StdioProcessManager"] = None,
        builtin_mcp_client: Optional["BuiltinMCPClient"] = None,
    ):
        self._stdio_pm = stdio_process_manager
        self._builtin = builtin_mcp_client
        log.debugx(
            "MCPClientFactory aangemaakt",
            has_stdio_process_manager=stdio_process_manager is not None,
            has_builtin_mcp_client=builtin_mcp_client is not None,
        )

    def set_stdio_process_manager(self, manager: "StdioProcessManager") -> None:
        self._stdio_pm = manager
        log.debugx("StdioProcessManager gekoppeld aan MCPClientFactory")

    def set_builtin_mcp_client(self, client: "BuiltinMCPClient") -> None:
        self._builtin = client
        log.debugx("BuiltinMCPClient gekoppeld aan MCPClientFactory")

    # ── Hoofdmethode ──────────────────────────────────────────────────────────

    def build(self, server, auth=None):
        server_type = (getattr(server, "server_type", None) or "http").strip().lower()
        log.infox(
            "MCPClient bouwen gestart",
            server_name=getattr(server, "name", None),
            server_type=server_type,
            has_auth=auth is not None,
        )

        if server_type == "builtin":
            return self._build_builtin(server)
        if server_type == "stdio":
            return self._build_stdio(server)
        return self._build_http(server, auth)

    # ── Builders ──────────────────────────────────────────────────────────────

    def _build_http(self, server, auth=None) -> "MCPClient":
        parsed = self.parse_auth(auth)
        log.debugx(
            "HTTP MCPClient bouwen",
            base_url=getattr(server, "base_url", None),
            auth_type=parsed.auth_type,
            has_bearer=bool(parsed.bearer),
        )
        client = MCPClient(mcp_url=server.base_url, bearer=parsed.bearer)
        log.infox("HTTP MCPClient gebouwd", base_url=getattr(server, "base_url", None))
        return client

    def _build_stdio(self, server):
        from services.mcp.stdio_mcp_client import StdioMCPClient
        if self._stdio_pm is None:
            raise RuntimeError(
                "StdioProcessManager is niet geconfigureerd in MCPClientFactory. "
                "Zorg dat stdio_process_manager is meegegeven bij constructie."
            )
        slug = getattr(server, "slug", None) or getattr(server, "name", None)
        log.infox("Stdio MCPClient bouwen", slug=slug)
        return StdioMCPClient(server_slug=slug, process_manager=self._stdio_pm)

    def _build_builtin(self, server):
        if self._builtin is None:
            raise RuntimeError(
                "BuiltinMCPClient is niet geconfigureerd in MCPClientFactory. "
                "Zorg dat builtin_mcp_client is meegegeven bij constructie."
            )
        log.infox("Builtin MCPClient bouwen", server_name=getattr(server, "name", None))
        return self._builtin

    # ── Auth parsing (volledig ongewijzigd t.o.v. origineel) ──────────────────

    def parse_auth(self, auth) -> ParsedAuth:
        log.debugx(
            "MCP auth parsen gestart",
            has_auth=auth is not None,
            auth_type=getattr(auth, "auth_type", None) if auth is not None else None,
            is_active=getattr(auth, "is_active", None) if auth is not None else None,
        )
        if auth is None:
            log.debugx("MCP auth ontbreekt, auth_type none wordt gebruikt")
            return ParsedAuth(auth_type="none")
        if getattr(auth, "is_active", True) is False:
            log.debugx("MCP auth is niet actief, auth_type none wordt gebruikt")
            return ParsedAuth(auth_type="none")

        auth_type = (getattr(auth, "auth_type", None) or "none").strip().lower()
        config = self._normalize_config(getattr(auth, "config", None))

        log.debugx("MCP auth type en config genormaliseerd", auth_type=auth_type, config_keys=list(config.keys()))

        if auth_type not in self.SUPPORTED_AUTH_TYPES:
            log.errorx("Niet-ondersteund MCP auth_type", auth_type=auth_type, supported_auth_types=sorted(self.SUPPORTED_AUTH_TYPES))
            raise MCPAuthError(
                f"Unsupported auth_type={auth_type!r}. "
                f"Supported values: {', '.join(sorted(self.SUPPORTED_AUTH_TYPES))}"
            )

        result = getattr(self, f"_parse_{auth_type}")(auth=auth, config=config)
        log.infox(
            "MCP auth parsen afgerond",
            auth_type=result.auth_type,
            has_bearer=bool(result.bearer),
            header_keys=list(result.headers.keys()),
        )
        return result

    def _parse_none(self, auth, config: Dict[str, Any]) -> ParsedAuth:
        return ParsedAuth(auth_type="none")

    def _parse_bearer(self, auth, config: Dict[str, Any]) -> ParsedAuth:
        token = self._first_non_empty(
            getattr(auth, "token", None),
            getattr(auth, "bearer_token", None),
            getattr(auth, "access_token", None),
            config.get("token"), config.get("bearer"),
            config.get("bearer_token"), config.get("access_token"),
        )
        if not token:
            log.errorx("MCP bearer auth token ontbreekt")
            raise MCPAuthError("bearer auth vereist een token in config.token, config.bearer, of config.access_token")
        return ParsedAuth(auth_type="bearer", bearer=token)

    def _parse_basic(self, auth, config: Dict[str, Any]) -> ParsedAuth:
        username = self._required(config, "username")
        password = self._required(config, "password")
        encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
        return ParsedAuth(
            auth_type="basic",
            bearer="",
            headers={"Authorization": f"Basic {encoded}"},
            basic_username=username,
            basic_password=password,
        )

    def _parse_api_key(self, auth, config: Dict[str, Any]) -> ParsedAuth:
        key = self._required(config, "api_key")
        header_name = self._first_non_empty(config.get("header_name")) or "X-Api-Key"
        in_ = (config.get("in") or "header").strip().lower()
        if in_ == "query":
            param_name = self._first_non_empty(config.get("param_name")) or "api_key"
            return ParsedAuth(auth_type="api_key", query_params={param_name: key})
        if in_ == "cookie":
            cookie_name = self._first_non_empty(config.get("cookie_name")) or "api_key"
            return ParsedAuth(auth_type="api_key", cookies={cookie_name: key})
        return ParsedAuth(auth_type="api_key", headers={header_name: key})

    def _parse_oauth_client_credentials(self, auth, config: Dict[str, Any]) -> ParsedAuth:
        return ParsedAuth(
            auth_type="oauth_client_credentials",
            oauth={
                "client_id": self._required(config, "client_id"),
                "client_secret": self._required(config, "client_secret"),
                "token_url": self._required(config, "token_url"),
                "scope": self._normalize_scope(config.get("scope")),
                "audience": self._first_non_empty(config.get("audience")),
            },
            metadata={"flow": "client_credentials"},
        )

    def _parse_oauth_authorization_code(self, auth, config: Dict[str, Any]) -> ParsedAuth:
        return ParsedAuth(
            auth_type="oauth_authorization_code",
            bearer=self._first_non_empty(config.get("access_token")) or "",
            oauth={
                "client_id": self._required(config, "client_id"),
                "client_secret": self._first_non_empty(config.get("client_secret")),
                "authorization_url": self._required(config, "authorization_url"),
                "token_url": self._required(config, "token_url"),
                "redirect_uri": self._first_non_empty(config.get("redirect_uri")),
                "scope": self._normalize_scope(config.get("scope")),
                "access_token": self._first_non_empty(config.get("access_token")),
                "refresh_token": self._first_non_empty(config.get("refresh_token")),
            },
            metadata={"flow": "authorization_code"},
        )

    def _parse_oauth_device_code(self, auth, config: Dict[str, Any]) -> ParsedAuth:
        return ParsedAuth(
            auth_type="oauth_device_code",
            bearer=self._first_non_empty(config.get("access_token")) or "",
            oauth={
                "client_id": self._required(config, "client_id"),
                "device_authorization_url": self._required(config, "device_authorization_url"),
                "token_url": self._required(config, "token_url"),
                "scope": self._normalize_scope(config.get("scope")),
                "access_token": self._first_non_empty(config.get("access_token")),
            },
            metadata={"flow": "device_code"},
        )

    def _parse_ssh_key(self, auth, config: Dict[str, Any]) -> ParsedAuth:
        private_key = self._first_non_empty(config.get("private_key"))
        private_key_path = self._first_non_empty(config.get("private_key_path"))
        if not private_key and not private_key_path:
            raise MCPAuthError("ssh_key auth requires either config.private_key or config.private_key_path")
        ssh = {
            "username": self._first_non_empty(config.get("username"), "git"),
            "private_key": private_key,
            "private_key_path": private_key_path,
            "public_key": self._first_non_empty(config.get("public_key")),
            "public_key_path": self._first_non_empty(config.get("public_key_path")),
            "passphrase": self._first_non_empty(config.get("passphrase")),
            "known_hosts": self._first_non_empty(config.get("known_hosts")),
            "known_hosts_path": self._first_non_empty(config.get("known_hosts_path")),
            "host": self._first_non_empty(config.get("host")),
            "port": config.get("port"),
            "strict_host_key_checking": bool(config.get("strict_host_key_checking", True)),
        }
        return ParsedAuth(auth_type="ssh_key", ssh=ssh, metadata={"transport": "ssh"})

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _normalize_config(self, config: Any) -> Dict[str, Any]:
        if config is None:
            return {}
        if isinstance(config, dict):
            return config
        if isinstance(config, str):
            stripped = config.strip()
            if not stripped:
                return {}
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise MCPAuthError("auth.config is a string but not valid JSON") from exc
            if not isinstance(parsed, dict):
                raise MCPAuthError("auth.config JSON must decode to an object/dict")
            return parsed
        raise MCPAuthError(f"auth.config must be a dict, JSON string, or None; got {type(config)!r}")

    def _first_non_empty(self, *values: Any) -> Optional[str]:
        for value in values:
            if value is None:
                continue
            if isinstance(value, str):
                stripped = value.strip()
                if stripped:
                    return stripped
                continue
            return str(value)
        return None

    def _required(self, config: Dict[str, Any], key: str) -> str:
        value = self._first_non_empty(config.get(key))
        if not value:
            raise MCPAuthError(f"Missing required auth config field: {key}")
        return value

    def _safe_dict(self, value: Any) -> Dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        raise MCPAuthError(f"Expected dict, got {type(value)!r}")

    def _normalize_scope(self, scope: Any) -> Optional[str]:
        if scope is None:
            return None
        if isinstance(scope, str):
            return scope.strip() or None
        if isinstance(scope, list):
            normalized = [str(item).strip() for item in scope if str(item).strip()]
            return " ".join(normalized) or None
        raise MCPAuthError("scope must be a string, list, or None")

    @staticmethod
    def mask_secret(value: Optional[str], visible: int = 4) -> str:
        if not value:
            return ""
        if len(value) <= visible:
            return "*" * len(value)
        return "*" * (len(value) - visible) + value[-visible:]
