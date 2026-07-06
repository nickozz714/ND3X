from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, Dict, List, Optional

import httpx
from fastapi.encoders import jsonable_encoder
from fastmcp import Client

from component.logging import get_logger


log = get_logger(__name__)

class MCPClientError(RuntimeError):
    pass


class MCPUnavailableError(MCPClientError):
    pass


class MCPInvalidResponseError(MCPClientError):
    pass


class MCPToolExecutionError(MCPClientError):
    def __init__(
        self,
        message: str,
        *,
        reason: str = "mcp_tool_execution_failed",
        upstream_status_code: int | None = None,
        upstream_unavailable: bool = False,
    ):
        super().__init__(message)
        self.reason = reason
        self.upstream_status_code = upstream_status_code
        self.upstream_unavailable = upstream_unavailable

def _extract_http_status_from_message(message: str) -> int | None:
    import re

    match = re.search(r"HTTP\s+(\d{3})", message or "")
    if not match:
        return None

    try:
        return int(match.group(1))
    except Exception:
        return None


def _looks_like_upstream_unavailable(message: str) -> bool:
    text = (message or "").lower()
    markers = (
        "request failed:",
        "connection refused",
        "connect call failed",
        "connection reset",
        "network is unreachable",
        "name or service not known",
        "nodename nor servname provided",
        "temporarily unavailable",
        "timed out",
        "timeout",
    )
    return any(marker in text for marker in markers)
class BearerAuth(httpx.Auth):
    def __init__(self, token: str):
        log.debugx(
            "BearerAuth initialiseren",
            has_token=bool(token),
            token_length=len(token or ""),
        )
        self.token = token

    def auth_flow(self, request: httpx.Request):
        log.debugx(
            "Bearer authorization header toevoegen aan request",
            method=request.method,
            url=str(request.url),
            has_token=bool(self.token),
        )
        request.headers["Authorization"] = f"Bearer {self.token}"
        yield request


def _is_empty(x: Any) -> bool:
    log.debugx(
        "Controleren of waarde leeg is gestart",
        value_type=type(x).__name__,
    )
    result = x is None or x == "" or x == [] or x == {} or x == ()
    log.debugx(
        "Controleren of waarde leeg is afgerond",
        value_type=type(x).__name__,
        is_empty=result,
    )
    return result


def _dump(x: Any) -> Any:
    log.debugx(
        "Object dumpen gestart",
        object_type=type(x).__name__,
        has_model_dump=hasattr(x, "model_dump"),
        has_dict=hasattr(x, "dict"),
    )
    if hasattr(x, "model_dump"):
        result = x.model_dump()
        log.debugx(
            "Object gedumpt via model_dump",
            object_type=type(x).__name__,
            result_type=type(result).__name__,
        )
        return result
    if hasattr(x, "dict"):
        result = x.dict()
        log.debugx(
            "Object gedumpt via dict",
            object_type=type(x).__name__,
            result_type=type(result).__name__,
        )
        return result
    log.debugx(
        "Object dump fallback gebruikt",
        object_type=type(x).__name__,
    )
    return x


def _try_parse_json(s: str) -> Any:
    log.debugx(
        "JSON string proberen te parsen gestart",
        input_length=len(s or ""),
    )
    t = (s or "").strip()
    if not t:
        log.debugx("JSON parse overgeslagen: lege string")
        return t
    if (t.startswith("{") and t.endswith("}")) or (t.startswith("[") and t.endswith("]")):
        try:
            result = json.loads(t)
            log.debugx(
                "JSON string succesvol geparsed",
                result_type=type(result).__name__,
            )
            return result
        except Exception:
            log.warningx(
                "JSON string parsen mislukt, originele tekst wordt gebruikt",
                input_length=len(t),
            )
            return t
    log.debugx(
        "JSON parse overgeslagen: string lijkt geen JSON object of array",
        input_length=len(t),
    )
    return t


def _normalize_call_tool_result(res: Any) -> Any:
    """
    Normalize tool result for UI using ONLY res.content (ignore res.data).
    Handles:
      1) Output wrapper model: fooOutput(result=[Root(),...]) or .results
      2) MCP text blocks: [{"type":"text","text":"{...json...}"}, ...]
      3) Fallback: dump the content/model as plain JSON-encodable data
    """
    log.debugx(
        "MCP tool resultaat normaliseren gestart",
        result_type=type(res).__name__,
        has_content=hasattr(res, "content"),
    )

    content = getattr(res, "content", None)
    if _is_empty(content):
        log.debugx(
            "MCP tool resultaat content is leeg, volledige response wordt gedumpt",
            result_type=type(res).__name__,
        )
        return jsonable_encoder(_dump(res))

    # ------------------------------------------------------------
    # Case 1: content is a list (blocks / models)
    # ------------------------------------------------------------
    if isinstance(content, list) and content:
        log.debugx(
            "MCP tool resultaat bevat content lijst",
            content_count=len(content),
            first_type=type(content[0]).__name__,
        )
        first = content[0]

        # 1A) Wrapper models: content[0].result / .results
        if hasattr(first, "result"):
            log.debugx("MCP tool resultaat genormaliseerd via content[0].result")
            return jsonable_encoder(_dump(getattr(first, "result")))
        if hasattr(first, "results"):
            log.debugx("MCP tool resultaat genormaliseerd via content[0].results")
            return jsonable_encoder(_dump(getattr(first, "results")))

        # 1B) Treat as generic blocks
        blocks = [_dump(b) for b in content]
        log.debugx(
            "MCP content blocks gedumpt",
            block_count=len(blocks),
        )

        # If ALL blocks look like MCP text blocks, parse ALL their .text
        if all(
            isinstance(b, dict)
            and b.get("type") == "text"
            and isinstance(b.get("text"), str)
            for b in blocks
        ):
            log.debugx(
                "Alle MCP blocks zijn text blocks, text wordt geparsed",
                block_count=len(blocks),
            )
            parsed = [_try_parse_json(b["text"]) for b in blocks]

            # If every parsed element is a list, flatten them
            if all(isinstance(p, list) for p in parsed):
                log.debugx(
                    "Alle geparsede MCP text blocks zijn lijsten, resultaat wordt geflattened",
                    parsed_count=len(parsed),
                )
                flat: list[Any] = []
                for p in parsed:
                    flat.extend(p)
                return jsonable_encoder(flat)

            # If there is exactly one parsed value, just return it
            if len(parsed) == 1:
                log.debugx(
                    "Één geparsed MCP text block gevonden, enkel resultaat wordt teruggegeven",
                    parsed_type=type(parsed[0]).__name__,
                )
                return jsonable_encoder(parsed[0])

            # Otherwise return the list of parsed values
            log.debugx(
                "Meerdere geparsede MCP text blocks gevonden, lijst wordt teruggegeven",
                parsed_count=len(parsed),
            )
            return jsonable_encoder(parsed)

        # 1C) Fallback: return the list of dumped blocks as-is
        log.debugx(
            "MCP content blocks fallback: gedumpte blocks worden teruggegeven",
            block_count=len(blocks),
        )
        return jsonable_encoder(blocks)

    # ------------------------------------------------------------
    # Case 2: single object in res.content
    # ------------------------------------------------------------
    obj = content
    log.debugx(
        "MCP tool resultaat bevat single content object",
        object_type=type(obj).__name__,
    )

    # Wrapper with .result / .results
    if hasattr(obj, "result"):
        log.debugx("MCP single content object genormaliseerd via .result")
        return jsonable_encoder(_dump(getattr(obj, "result")))
    if hasattr(obj, "results"):
        log.debugx("MCP single content object genormaliseerd via .results")
        return jsonable_encoder(_dump(getattr(obj, "results")))

    d = _dump(obj)

    # Single MCP text block: {"type":"text","text":"{...json...}"}
    if isinstance(d, dict) and d.get("type") == "text" and isinstance(d.get("text"), str):
        log.debugx("MCP single text block gevonden, text wordt geparsed")
        return jsonable_encoder(_try_parse_json(d["text"]))

    # Final fallback: just dump whatever data we have
    log.debugx(
        "MCP tool resultaat fallback normalisatie gebruikt",
        dumped_type=type(d).__name__,
    )
    return jsonable_encoder(d)


def _normalize_tool(tool: Any) -> Dict[str, Any]:
    """
    Normalize one tool object returned by client.list_tools().

    Expected FastMCP/MCP fields include at least:
      - name
      - description
      - inputSchema
      - meta (optional; FastMCP tags often live under meta["_fastmcp"]["tags"])
    """
    log.debugx(
        "MCP tool normaliseren gestart",
        tool_type=type(tool).__name__,
    )
    raw = _dump(tool)
    if not isinstance(raw, dict):
        log.debugx(
            "MCP tool dump is geen dict, jsonable_encoder wordt gebruikt",
            raw_type=type(raw).__name__,
        )
        raw = jsonable_encoder(raw)

    meta = raw.get("meta") or {}
    fastmcp_meta = meta.get("_fastmcp") or {}

    annotations = raw.get("annotations") or {}

    result = {
        "name": raw.get("name"),
        "title": raw.get("title"),
        "description": raw.get("description"),
        "inputSchema": raw.get("inputSchema") or {},
        "outputSchema": raw.get("outputSchema"),
        "annotations": annotations,
        "meta": meta,
        "tags": fastmcp_meta.get("tags", []),
        "fastmcp": fastmcp_meta,
        "raw": raw,  # handig voor debugging / UI-inspectie
    }
    log.debugx(
        "MCP tool normaliseren afgerond",
        tool_name=result.get("name"),
        title=result.get("title"),
        has_description=bool(result.get("description")),
        tag_count=len(result.get("tags") or []),
        has_input_schema=bool(result.get("inputSchema")),
        has_output_schema=bool(result.get("outputSchema")),
    )
    return result


def _normalize_tools_listing(
    tools: List[Any],
    *,
    name_contains: Optional[str] = None,
    tag: Optional[str] = None,
) -> Dict[str, Any]:
    log.debugx(
        "MCP tools listing normaliseren gestart",
        tool_count=len(tools or []),
        name_contains=name_contains,
        tag=tag,
    )
    items = [_normalize_tool(t) for t in tools]

    if name_contains:
        needle = name_contains.lower().strip()
        before_count = len(items)
        items = [t for t in items if needle in (t.get("name") or "").lower()]
        log.debugx(
            "MCP tools gefilterd op naam",
            name_contains=name_contains,
            before_count=before_count,
            after_count=len(items),
        )

    if tag:
        before_count = len(items)
        items = [t for t in items if tag in (t.get("tags") or [])]
        log.debugx(
            "MCP tools gefilterd op tag",
            tag=tag,
            before_count=before_count,
            after_count=len(items),
        )

    result = {
        "count": len(items),
        "tools": items,
        "tool_names": [t["name"] for t in items if t.get("name")],
        "by_name": {t["name"]: t for t in items if t.get("name")},
    }
    log.debugx(
        "MCP tools listing normaliseren afgerond",
        count=result["count"],
        tool_names=result["tool_names"],
    )
    return result


class MCPClient:
    """
    MCP client with HARD timeouts + safe retries.

    We intentionally add a top-level asyncio timeout guard because different
    fastmcp versions may not expose httpx timeout knobs. This prevents
    "endless wait" even if the underlying stream stalls.
    """

    def __init__(
        self,
        *,
        mcp_url: str,
        bearer: str = "",
        # hard deadline for a single call end-to-end
        call_timeout_s: float = 120.0,
        # connect/retry tuning
        retries: int = 1,
        retry_backoff_s: float = 0.35,
    ):
        log.infox(
            "MCPClient initialiseren",
            mcp_url=mcp_url,
            has_bearer=bool(bearer),
            call_timeout_s=call_timeout_s,
            retries=retries,
            retry_backoff_s=retry_backoff_s,
        )
        self.mcp_url = mcp_url
        self.bearer = bearer
        self.call_timeout_s = float(call_timeout_s)
        self.retries = max(0, int(retries))
        self.retry_backoff_s = float(retry_backoff_s)
        log.debugx(
            "MCPClient geïnitialiseerd",
            mcp_url=self.mcp_url,
            has_bearer=bool(self.bearer),
            call_timeout_s=self.call_timeout_s,
            retries=self.retries,
            retry_backoff_s=self.retry_backoff_s,
        )

    async def _with_client(self):
        log.debugx(
            "FastMCP client aanmaken",
            mcp_url=self.mcp_url,
            has_bearer=bool(self.bearer),
        )
        auth: Optional[httpx.Auth] = BearerAuth(self.bearer) if self.bearer else None
        return Client(self.mcp_url, auth=auth)

    async def _call_once(self, tool: str, args: Dict[str, Any]) -> Any:
        log.infox(
            "MCP tool call uitvoeren gestart",
            mcp_url=self.mcp_url,
            tool=tool,
            arg_keys=list(args.keys()) if isinstance(args, dict) else None,
            has_bearer=bool(self.bearer),
        )
        auth: Optional[httpx.Auth] = BearerAuth(self.bearer) if self.bearer else None
        async with Client(self.mcp_url, auth=auth) as client:
            res = await client.call_tool(tool, args, raise_on_error=True)
            log.debugx(
                "MCP raw tool response ontvangen",
                tool=tool,
                response_type=type(res).__name__,
                has_content=hasattr(res, "content"),
            )
            result = _normalize_call_tool_result(res)
            log.infox(
                "MCP tool call uitvoeren afgerond",
                tool=tool,
                result_type=type(result).__name__,
            )
            return result

    async def call(self, tool: str, args: Dict[str, Any]) -> Any:
        log.infox(
            "MCP call gestart",
            tool=tool,
            arg_keys=list(args.keys()) if isinstance(args, dict) else None,
            retries=self.retries,
            timeout_s=self.call_timeout_s,
        )
        last_err: Optional[Exception] = None

        for attempt in range(self.retries + 1):
            log.debugx(
                "MCP call poging gestart",
                tool=tool,
                attempt=attempt + 1,
                max_attempts=self.retries + 1,
            )
            try:
                # Absolute guardrail against hangs:
                result = await asyncio.wait_for(
                    self._call_once(tool, args),
                    timeout=self.call_timeout_s,
                )
                log.infox(
                    "MCP call succesvol afgerond",
                    tool=tool,
                    attempt=attempt + 1,
                    result_type=type(result).__name__,
                )
                return result
            except asyncio.TimeoutError as e:
                last_err = e
                log.warningx(
                    "MCP call timeout",
                    tool=tool,
                    attempt=attempt + 1,
                    timeout_s=self.call_timeout_s,
                )
            except (httpx.TimeoutException, httpx.TransportError) as e:
                # In case fastmcp surfaces httpx errors directly
                last_err = e
                log.warningx(
                    "MCP call transport/timeout fout",
                    tool=tool,
                    attempt=attempt + 1,
                    error=str(e),
                    error_type=type(e).__name__,
                )

            except Exception as e:
                message = str(e)
                upstream_status = _extract_http_status_from_message(message)
                upstream_unavailable = _looks_like_upstream_unavailable(message)

                log.errorx(
                    "MCP call mislukt met tool/runtime fout",
                    tool=tool,
                    attempt=attempt + 1,
                    error=message,
                    error_type=type(e).__name__,
                    upstream_status=upstream_status,
                    upstream_unavailable=upstream_unavailable,
                )

                raise MCPToolExecutionError(
                    f"MCP tool failed: tool={tool}: {message}",
                    reason="upstream_unavailable" if upstream_unavailable else "mcp_tool_execution_failed",
                    upstream_status_code=upstream_status,
                    upstream_unavailable=upstream_unavailable,
                ) from e

            # retry (transport/timeout only)
            if attempt < self.retries:
                sleep_s = self.retry_backoff_s * (attempt + 1)
                log.debugx(
                    "MCP call retry voorbereiden",
                    tool=tool,
                    attempt=attempt + 1,
                    sleep_s=sleep_s,
                )
                await asyncio.sleep(sleep_s)

        log.errorx(
            "MCP call definitief mislukt na retries",
            tool=tool,
            retries=self.retries,
            last_error=str(last_err),
            last_error_type=type(last_err).__name__ if last_err else None,
        )
        raise MCPUnavailableError(
            f"MCP server unavailable or timed out: tool={tool} err={last_err}"
        )

    async def _list_tools_once(self) -> List[Any]:
        log.infox(
            "MCP list_tools uitvoeren gestart",
            mcp_url=self.mcp_url,
            has_bearer=bool(self.bearer),
        )
        auth: Optional[httpx.Auth] = BearerAuth(self.bearer) if self.bearer else None
        async with Client(self.mcp_url, auth=auth) as client:
            tools = await client.list_tools()
            result = list(tools or [])
            log.infox(
                "MCP list_tools uitvoeren afgerond",
                tool_count=len(result),
            )
            return result

    async def list_tools(self) -> List[Dict[str, Any]]:
        """
        Raw-ish normalized list of tools from the MCP server.
        Good if je gewoon alles wilt zien zoals de server het aanbiedt.
        """
        log.infox(
            "MCP list_tools gestart",
            retries=self.retries,
            timeout_s=self.call_timeout_s,
        )
        last_err: Optional[Exception] = None

        for attempt in range(self.retries + 1):
            log.debugx(
                "MCP list_tools poging gestart",
                attempt=attempt + 1,
                max_attempts=self.retries + 1,
            )
            try:
                tools = await asyncio.wait_for(
                    self._list_tools_once(),
                    timeout=self.call_timeout_s,
                )
                result = jsonable_encoder([_dump(t) for t in tools])
                log.infox(
                    "MCP list_tools succesvol afgerond",
                    attempt=attempt + 1,
                    tool_count=len(result) if isinstance(result, list) else None,
                )
                return result
            except asyncio.TimeoutError as e:
                last_err = e
                log.warningx(
                    "MCP list_tools timeout",
                    attempt=attempt + 1,
                    timeout_s=self.call_timeout_s,
                )
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = e
                log.warningx(
                    "MCP list_tools transport/timeout fout",
                    attempt=attempt + 1,
                    error=str(e),
                    error_type=type(e).__name__,
                )
            except Exception:
                log.errorx(
                    "MCP list_tools mislukt met niet-retrybare fout",
                    attempt=attempt + 1,
                )
                raise

            if attempt < self.retries:
                sleep_s = self.retry_backoff_s * (attempt + 1)
                log.debugx(
                    "MCP list_tools retry voorbereiden",
                    attempt=attempt + 1,
                    sleep_s=sleep_s,
                )
                await asyncio.sleep(sleep_s)

        log.errorx(
            "MCP list_tools definitief mislukt na retries",
            retries=self.retries,
            last_error=str(last_err),
            last_error_type=type(last_err).__name__ if last_err else None,
        )
        raise RuntimeError(f"MCP list_tools timed out/failed: err={last_err}")

    async def list_tools_listing(
        self,
        *,
        name_contains: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Uitgebreide tool listing voor UI/API.

        Retourneert o.a.:
          - count
          - tools[]
          - tool_names[]
          - by_name{...}

        Per tool:
          - name
          - title
          - description
          - inputSchema
          - outputSchema
          - annotations
          - meta
          - tags
          - fastmcp
          - raw
        """
        log.infox(
            "MCP list_tools_listing gestart",
            name_contains=name_contains,
            tag=tag,
            retries=self.retries,
            timeout_s=self.call_timeout_s,
        )
        last_err: Optional[Exception] = None

        for attempt in range(self.retries + 1):
            log.debugx(
                "MCP list_tools_listing poging gestart",
                attempt=attempt + 1,
                max_attempts=self.retries + 1,
                name_contains=name_contains,
                tag=tag,
            )
            try:
                tools = await asyncio.wait_for(
                    self._list_tools_once(),
                    timeout=self.call_timeout_s,
                )
                result = jsonable_encoder(
                    _normalize_tools_listing(
                        tools,
                        name_contains=name_contains,
                        tag=tag,
                    )
                )
                log.infox(
                    "MCP list_tools_listing succesvol afgerond",
                    attempt=attempt + 1,
                    count=result.get("count") if isinstance(result, dict) else None,
                    name_contains=name_contains,
                    tag=tag,
                )
                return result
            except asyncio.TimeoutError as e:
                last_err = e
                log.warningx(
                    "MCP list_tools_listing timeout",
                    attempt=attempt + 1,
                    timeout_s=self.call_timeout_s,
                    name_contains=name_contains,
                    tag=tag,
                )
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = e
                log.warningx(
                    "MCP list_tools_listing transport/timeout fout",
                    attempt=attempt + 1,
                    error=str(e),
                    error_type=type(e).__name__,
                    name_contains=name_contains,
                    tag=tag,
                )
            except Exception:
                log.errorx(
                    "MCP list_tools_listing mislukt met niet-retrybare fout",
                    attempt=attempt + 1,
                    name_contains=name_contains,
                    tag=tag,
                )
                raise

            if attempt < self.retries:
                sleep_s = self.retry_backoff_s * (attempt + 1)
                log.debugx(
                    "MCP list_tools_listing retry voorbereiden",
                    attempt=attempt + 1,
                    sleep_s=sleep_s,
                )
                await asyncio.sleep(sleep_s)

        log.errorx(
            "MCP list_tools_listing definitief mislukt na retries",
            retries=self.retries,
            name_contains=name_contains,
            tag=tag,
            last_error=str(last_err),
            last_error_type=type(last_err).__name__ if last_err else None,
        )
        raise RuntimeError(f"MCP list_tools_listing timed out/failed: err={last_err}")

    async def get_tool(self, name: str) -> Optional[Dict[str, Any]]:
        """
        Handig hulpfunctietje om één tool op naam op te halen uit de listing.
        """
        log.infox(
            "MCP tool ophalen op naam gestart",
            name=name,
        )
        listing = await self.list_tools_listing()
        result = listing.get("by_name", {}).get(name)
        log.infox(
            "MCP tool ophalen op naam afgerond",
            name=name,
            found=result is not None,
        )
        return result

    async def _read_resource_once(self, uri: str) -> bytes:
        log.infox(
            "MCP resource lezen gestart",
            uri=uri,
            mcp_url=self.mcp_url,
            has_bearer=bool(self.bearer),
        )
        auth: Optional[httpx.Auth] = BearerAuth(self.bearer) if self.bearer else None
        async with Client(self.mcp_url, auth=auth) as client:
            content = await client.read_resource(uri)
            log.debugx(
                "MCP resource content ontvangen",
                uri=uri,
                has_content=bool(content),
                content_count=len(content) if content else 0,
            )

            # FastMCP returns a list of content blocks; for PDFs expect blob
            if not content:
                log.warningx(
                    "MCP resource heeft geen content",
                    uri=uri,
                )
                raise FileNotFoundError(f"No content for resource: {uri}")

            block = content[0]
            log.debugx(
                "MCP resource eerste content block verwerken",
                uri=uri,
                block_type=type(block).__name__,
                has_blob=bool(getattr(block, "blob", None)),
                has_data=hasattr(block, "data"),
                has_text=hasattr(block, "text"),
            )

            # Typical for binary: block.blob is base64
            blob = getattr(block, "blob", None)
            if blob:
                result = base64.b64decode(blob)
                log.infox(
                    "MCP resource blob succesvol gedecodeerd",
                    uri=uri,
                    bytes_count=len(result),
                )
                return result

            # Sometimes libraries expose bytes directly
            data = getattr(block, "data", None)
            if isinstance(data, (bytes, bytearray)):
                result = bytes(data)
                log.infox(
                    "MCP resource data bytes succesvol gelezen",
                    uri=uri,
                    bytes_count=len(result),
                )
                return result

            # Not binary / unexpected
            text = getattr(block, "text", None)
            log.warningx(
                "MCP resource is geen binary resource",
                uri=uri,
                text_preview=repr(text)[:250],
            )
            raise ValueError(f"Resource is not binary. Got text={text!r}")

    async def read_resource(self, uri: str) -> bytes:
        log.infox(
            "MCP read_resource gestart",
            uri=uri,
            retries=self.retries,
            timeout_s=self.call_timeout_s,
        )
        last_err: Optional[Exception] = None

        for attempt in range(self.retries + 1):
            log.debugx(
                "MCP read_resource poging gestart",
                uri=uri,
                attempt=attempt + 1,
                max_attempts=self.retries + 1,
            )
            try:
                result = await asyncio.wait_for(
                    self._read_resource_once(uri),
                    timeout=self.call_timeout_s,
                )
                log.infox(
                    "MCP read_resource succesvol afgerond",
                    uri=uri,
                    attempt=attempt + 1,
                    bytes_count=len(result),
                )
                return result
            except asyncio.TimeoutError as e:
                last_err = e
                log.warningx(
                    "MCP read_resource timeout",
                    uri=uri,
                    attempt=attempt + 1,
                    timeout_s=self.call_timeout_s,
                )
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = e
                log.warningx(
                    "MCP read_resource transport/timeout fout",
                    uri=uri,
                    attempt=attempt + 1,
                    error=str(e),
                    error_type=type(e).__name__,
                )
            except Exception:
                log.errorx(
                    "MCP read_resource mislukt met niet-retrybare fout",
                    uri=uri,
                    attempt=attempt + 1,
                )
                raise

            if attempt < self.retries:
                sleep_s = self.retry_backoff_s * (attempt + 1)
                log.debugx(
                    "MCP read_resource retry voorbereiden",
                    uri=uri,
                    attempt=attempt + 1,
                    sleep_s=sleep_s,
                )
                await asyncio.sleep(sleep_s)

        log.errorx(
            "MCP read_resource definitief mislukt na retries",
            uri=uri,
            retries=self.retries,
            last_error=str(last_err),
            last_error_type=type(last_err).__name__ if last_err else None,
        )
        raise RuntimeError(f"MCP read_resource timed out/failed: uri={uri} err={last_err}")