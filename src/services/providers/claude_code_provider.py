"""
services/providers/claude_code_provider.py

Chat adapter that runs the Claude Code CLI in headless mode (`claude -p`).

Why a provider and not a tool: this makes Claude Code selectable on the normal
routing slots (chat, cognition, ...) like any other model. Each chat() call
spawns one non-interactive CLI run; ND3X keeps carrying the conversation, so
the adapter is stateless (no --resume) and fits the ChatProvider contract.

Auth is SUBSCRIPTION-based, not API-key-based: the provider's stored "API key"
is the long-lived OAuth token from `claude setup-token`, injected into the
subprocess as CLAUDE_CODE_OAUTH_TOKEN. ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN
are stripped from the subprocess env — if either is present the CLI silently
bills the API instead of the subscription. With no token stored, the CLI falls
back to the host login (~/.claude), which is the desktop-app case.

Two modes (per-provider `config_json`):
- default (agentic=false): behaves like a plain chat model — one turn
  (--max-turns 1) and the built-in tools disallowed.
- agentic=true: Claude Code keeps its own tools and agent loop and runs with
  --permission-mode bypassPermissions. NOTE: that executes shell commands in
  THIS process's environment/container — only enable it on isolated deploys.

config_json keys (all optional):
  {"agentic": bool, "cli_path": "claude", "timeout": 600, "max_turns": int,
   "workdir": "/path", "allowed_tools": "Bash(git:*) Read", "extra_args": [...]}
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, AsyncIterator, Dict, List, Optional

from component.logging import get_logger
from services.providers.base import ChatInput, ChatProvider, ChatResult

log = get_logger(__name__)

# Built-in tools disallowed in plain-chat mode so the CLI acts as a pure LLM.
NON_AGENTIC_DISALLOWED_TOOLS = (
    "Bash,Edit,Write,NotebookEdit,Read,Glob,Grep,WebFetch,WebSearch,Task,TodoWrite"
)

# Env vars stripped from the subprocess: if ANTHROPIC_API_KEY (or AUTH_TOKEN)
# leaks in, the CLI bills the API instead of the subscription token.
_STRIPPED_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def _default_timeout() -> float:
    try:
        from component.config import settings
        return float(getattr(settings, "CLAUDE_CODE_TIMEOUT", 600) or 600)
    except Exception:  # noqa: BLE001
        return 600.0


def _to_prompt(user_input: ChatInput) -> str:
    """Flatten ChatInput to one prompt string for `claude -p` (stdin).

    A plain string passes through. A message list becomes a labeled transcript;
    provider-neutral content blocks contribute their text (images are not
    supported over the CLI and are skipped with a warning).
    """
    if isinstance(user_input, str):
        return user_input
    lines: List[str] = []
    for m in user_input or []:
        role = (m.get("role") or "user").strip().lower()
        content = m.get("content")
        if isinstance(content, list):
            texts: List[str] = []
            for block in content:
                btype = block.get("type")
                if btype in {"text", "input_text"}:
                    texts.append(block.get("text") or "")
                elif btype in {"image", "input_image"}:
                    log.warningx("Claude Code CLI ondersteunt geen images in de prompt — blok overgeslagen")
            text = "\n".join(t for t in texts if t)
        else:
            text = content or ""
        if not text:
            continue
        if role == "system":
            lines.append(f"[system]\n{text}")
        elif role == "assistant":
            lines.append(f"Assistant:\n{text}")
        else:
            lines.append(f"User:\n{text}")
    return "\n\n".join(lines)


class ClaudeCodeChatProvider(ChatProvider):
    provider_type = "claude_code"
    # The CLI has no JSON-schema enforcement; the router falls back to the
    # non-schema path for providers with this off.
    supports_structured_output = False
    supports_streaming = True

    def __init__(
        self,
        *,
        default_model: str = "",
        oauth_token: Optional[str] = None,
        cli_path: str = "claude",
        agentic: bool = False,
        max_turns: Optional[int] = None,
        timeout: Optional[float] = None,
        workdir: Optional[str] = None,
        allowed_tools: Optional[str] = None,
        extra_args: Optional[List[str]] = None,
    ):
        self._default_model = default_model
        self._oauth_token = (oauth_token or "").strip() or None
        self._cli_path = (cli_path or "claude").strip() or "claude"
        self._agentic = bool(agentic)
        self._max_turns = max_turns
        self._timeout = float(timeout) if timeout else _default_timeout()
        self._workdir = workdir or None
        self._allowed_tools = allowed_tools or None
        self._extra_args = list(extra_args or [])
        log.debugx(
            "ClaudeCodeChatProvider aangemaakt",
            cli_path=self._cli_path, agentic=self._agentic,
            has_token=bool(self._oauth_token), timeout=self._timeout,
        )

    # ------------------------------------------------------------------ build

    def _build_env(self) -> Dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in _STRIPPED_ENV_VARS}
        if self._oauth_token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = self._oauth_token
        return env

    def _build_cmd(self, model_id: str, instructions: Optional[str]) -> List[str]:
        cmd = [self._cli_path, "-p", "--model", model_id]
        if instructions:
            cmd += ["--append-system-prompt", instructions]
        if self._agentic:
            cmd += ["--permission-mode", "bypassPermissions"]
            if self._allowed_tools:
                cmd += ["--allowedTools", self._allowed_tools]
            if self._max_turns:
                cmd += ["--max-turns", str(int(self._max_turns))]
        else:
            cmd += ["--max-turns", str(int(self._max_turns or 1))]
            cmd += ["--disallowedTools", NON_AGENTIC_DISALLOWED_TOOLS]
        cmd += self._extra_args
        return cmd

    async def _spawn(self, cmd: List[str]) -> asyncio.subprocess.Process:
        try:
            return await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._build_env(),
                cwd=self._workdir,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Claude Code CLI niet gevonden ('{self._cli_path}') — installeer "
                "@anthropic-ai/claude-code of zet cli_path in de provider-config."
            ) from exc

    @staticmethod
    async def _kill(proc: asyncio.subprocess.Process) -> None:
        try:
            proc.kill()
            await proc.wait()
        except Exception:  # noqa: BLE001
            pass

    def _record_usage(self, model_id: str, usage: Dict[str, Any]) -> None:
        try:
            from services.providers.usage_accumulator import add as _usage_add
            _usage_add(
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                model=model_id,
                provider_type=self.provider_type,
            )
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------- chat

    async def chat(
        self,
        user_input: ChatInput,
        *,
        model: Optional[str] = None,
        instructions: Optional[str] = None,
        response_format: Optional[Dict[str, Any]] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_output_tokens: Optional[int] = None,
        metadata: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> ChatResult:
        """One headless CLI run: prompt in via stdin, JSON result envelope out.

        temperature/top_p/max_output_tokens have no CLI equivalent and are
        ignored; response_format is ignored (supports_structured_output=False,
        the router never sends it here).
        """
        model_id = model or self._default_model
        prompt = _to_prompt(user_input)
        cmd = self._build_cmd(model_id, instructions) + ["--output-format", "json"]

        proc = await self._spawn(cmd)
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")), timeout=self._timeout
            )
        except asyncio.TimeoutError:
            await self._kill(proc)
            raise RuntimeError(
                f"Claude Code run overschreed de timeout ({self._timeout:.0f}s) voor '{model_id}'"
            )

        if proc.returncode != 0:
            err = (stderr or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"Claude Code CLI faalde (exit {proc.returncode}) voor '{model_id}': "
                f"{err[-800:] or 'geen stderr'}"
            )

        data = self._parse_result(stdout)
        if data.get("is_error"):
            raise RuntimeError(
                f"Claude Code gaf een fout terug ({data.get('subtype')}): "
                f"{str(data.get('result') or '')[:400]}"
            )

        usage = data.get("usage") or {}
        if data.get("total_cost_usd") is not None:
            # Indicative only — subscription runs are not billed per token.
            usage = {**usage, "total_cost_usd": data.get("total_cost_usd")}
        self._record_usage(model_id, usage)
        return ChatResult(
            text=str(data.get("result") or ""),
            response_id=str(data.get("session_id") or ""),
            raw=data,
            provider=self.provider_type,
            model=model_id,
            usage=usage,
        )

    @staticmethod
    def _parse_result(stdout: bytes) -> Dict[str, Any]:
        """`--output-format json` prints one JSON object; be tolerant of stray
        lines around it (npm/node warnings) by scanning for the result event."""
        text = (stdout or b"").decode("utf-8", errors="replace").strip()
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except Exception:  # noqa: BLE001
            pass
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(obj, dict) and obj.get("type") == "result":
                return obj
        raise RuntimeError(f"Claude Code output niet parsebaar als JSON: {text[:400]!r}")

    # ------------------------------------------------------------------ stream

    async def chat_stream(
        self,
        user_input: ChatInput,
        *,
        model: Optional[str] = None,
        instructions: Optional[str] = None,
        response_format: Optional[Dict[str, Any]] = None,
        max_output_tokens: Optional[int] = None,
        metadata: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Yield text deltas from `--output-format stream-json`.

        Token-level deltas come from --include-partial-messages (stream_event /
        content_block_delta). If the installed CLI doesn't emit those, fall back
        to the buffered assistant-message text collected along the way.
        """
        model_id = model or self._default_model
        prompt = _to_prompt(user_input)
        cmd = self._build_cmd(model_id, instructions) + [
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
        ]

        proc = await self._spawn(cmd)
        yielded_deltas = False
        buffered: List[str] = []
        usage: Dict[str, Any] = {}
        try:
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

            loop = asyncio.get_running_loop()
            deadline = loop.time() + self._timeout
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise RuntimeError(
                        f"Claude Code stream overschreed de timeout ({self._timeout:.0f}s)"
                    )
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                otype = obj.get("type")
                if otype == "stream_event":
                    event = obj.get("event") or {}
                    delta = event.get("delta") or {}
                    if event.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                        text = delta.get("text") or ""
                        if text:
                            yielded_deltas = True
                            yield text
                elif otype == "assistant":
                    msg = obj.get("message") or {}
                    for block in msg.get("content") or []:
                        if isinstance(block, dict) and block.get("type") == "text":
                            buffered.append(block.get("text") or "")
                elif otype == "result":
                    if obj.get("is_error"):
                        raise RuntimeError(
                            f"Claude Code gaf een fout terug ({obj.get('subtype')}): "
                            f"{str(obj.get('result') or '')[:400]}"
                        )
                    usage = obj.get("usage") or {}

            rc = await proc.wait()
            if rc != 0:
                err = (await proc.stderr.read()).decode("utf-8", errors="replace") if proc.stderr else ""
                raise RuntimeError(
                    f"Claude Code CLI faalde (exit {rc}) voor '{model_id}': {err.strip()[-800:]}"
                )
            if not yielded_deltas:
                text = "".join(buffered)
                if text:
                    yield text
            self._record_usage(model_id, usage)
        finally:
            if proc.returncode is None:
                await self._kill(proc)
