from __future__ import annotations

from typing import Any, Dict

from fastapi import HTTPException, status

from services.mcp.mcp_client import (
    MCPUnavailableError,
    MCPToolExecutionError,
    MCPInvalidResponseError,
)


def _detail(
    *,
    ok: bool,
    available: bool,
    service: str,
    tool: str,
    reason: str,
    message: str,
) -> Dict[str, Any]:
    return {
        "ok": ok,
        "available": available,
        "service": service,
        "tool": tool,
        "reason": reason,
        "message": message,
    }


def _mcp_url(mcp: Any) -> str:
    return str(getattr(mcp, "mcp_url", "") or "").strip()


def _is_mcp_configured(mcp: Any) -> bool:
    return bool(_mcp_url(mcp))


def _raise_mcp_not_configured(
    *,
    service: str,
    tool: str,
) -> None:
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=_detail(
            ok=False,
            available=False,
            service=service,
            tool=tool,
            reason="mcp_not_configured",
            message=(
                f"{service} MCP dependency is not configured. "
                "Missing MCP_URL in the Interface environment."
            ),
        ),
    )


async def mcp_proxy_call(
    *,
    mcp: Any,
    service: str,
    tool: str,
    payload: Dict[str, Any] | None = None,
) -> Any:
    if not _is_mcp_configured(mcp):
        _raise_mcp_not_configured(service=service, tool=tool)

    try:
        return await mcp.call(tool, payload or {})

    except MCPUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_detail(
                ok=False,
                available=False,
                service=service,
                tool=tool,
                reason="mcp_unavailable",
                message=str(exc),
            ),
        ) from exc

    except MCPToolExecutionError as exc:
        status_code = status.HTTP_502_BAD_GATEWAY

        if exc.upstream_status_code in {401, 403}:
            status_code = exc.upstream_status_code
        elif exc.upstream_status_code == 404:
            status_code = status.HTTP_404_NOT_FOUND
        elif exc.upstream_status_code == 422:
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
        elif exc.upstream_status_code and 400 <= exc.upstream_status_code < 500:
            status_code = status.HTTP_400_BAD_REQUEST
        elif exc.upstream_unavailable:
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE

        raise HTTPException(
            status_code=status_code,
            detail=_detail(
                ok=False,
                available=status_code != status.HTTP_503_SERVICE_UNAVAILABLE,
                service=service,
                tool=tool,
                reason=exc.reason,
                message=str(exc),
            ),
        ) from exc

    except MCPInvalidResponseError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_detail(
                ok=False,
                available=True,
                service=service,
                tool=tool,
                reason="invalid_mcp_response",
                message=str(exc),
            ),
        ) from exc

    except HTTPException:
        raise

    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_detail(
                ok=False,
                available=True,
                service=service,
                tool=tool,
                reason="unexpected_proxy_error",
                message=f"Unexpected MCP proxy error for tool={tool}",
            ),
        ) from exc


async def mcp_proxy_health(
    *,
    mcp: Any,
    service: str,
    tool: str,
) -> Dict[str, Any]:
    if not _is_mcp_configured(mcp):
        _raise_mcp_not_configured(service=service, tool=tool)

    upstream = await mcp_proxy_call(
        mcp=mcp,
        service=service,
        tool=tool,
        payload={},
    )

    return {
        "ok": True,
        "available": True,
        "service": service,
        "tool": tool,
        "upstream": upstream,
    }