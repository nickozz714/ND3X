"""Unit tests for the Claude Code CLI chat adapter.

Exercised with a fake subprocess — no CLI, no network. The adapter's job is:
correct headless flags per mode (plain-chat vs agentic), subscription-safe env
(strip ANTHROPIC_API_KEY, inject CLAUDE_CODE_OAUTH_TOKEN), and parsing of the
`--output-format json` / `stream-json` envelopes into ChatResult/text deltas.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import services.providers.claude_code_provider as ccp
from services.providers.claude_code_provider import (
    NON_AGENTIC_DISALLOWED_TOOLS,
    NON_AGENTIC_INSTRUCTION,
    NON_AGENTIC_MAX_TURNS,
    ClaudeCodeChatProvider,
    _to_prompt,
)


def _result_envelope(**overrides) -> dict:
    base = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "hoi",
        "session_id": "sess-123",
        "num_turns": 1,
        "total_cost_usd": 0.0123,
        "usage": {"input_tokens": 100, "output_tokens": 10},
    }
    base.update(overrides)
    return base


class _FakeProc:
    """Stand-in for asyncio.subprocess.Process covering both call shapes."""

    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0,
                 hang: bool = False):
        self._stdout_bytes = stdout
        self._stderr_bytes = stderr
        self._final_rc = returncode
        self._hang = hang
        self.returncode: int | None = None
        self.killed = False
        self.stdin_data = b""
        # stream-mode handles
        self.stdin = self
        self.stdout = self
        self.stderr = self
        self._lines = stdout.splitlines(keepends=True)
        self._line_idx = 0

    # --- chat() path
    async def communicate(self, input: bytes = b""):
        if self._hang:
            await asyncio.sleep(3600)
        self.stdin_data = input
        self.returncode = self._final_rc
        return self._stdout_bytes, self._stderr_bytes

    # --- stream path (self doubles as stdin/stdout/stderr handle)
    def write(self, data: bytes) -> None:
        self.stdin_data += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def readline(self) -> bytes:
        if self._hang:
            await asyncio.sleep(3600)
        if self._line_idx >= len(self._lines):
            return b""
        line = self._lines[self._line_idx]
        self._line_idx += 1
        return line

    async def read(self) -> bytes:
        return self._stderr_bytes

    async def wait(self) -> int:
        self.returncode = self._final_rc
        return self._final_rc

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def _patch_spawn(monkeypatch, proc: _FakeProc, captured: dict):
    async def fake_exec(*cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = kwargs.get("env")
        captured["cwd"] = kwargs.get("cwd")
        return proc

    monkeypatch.setattr(ccp.asyncio, "create_subprocess_exec", fake_exec)


# ---------------------------------------------------------------- prompt/cmd


def test_to_prompt_passthrough_and_transcript():
    assert _to_prompt("hallo") == "hallo"
    prompt = _to_prompt([
        {"role": "system", "content": "wees kort"},
        {"role": "user", "content": "vraag"},
        {"role": "assistant", "content": "antwoord"},
        {"role": "user", "content": [
            {"type": "text", "text": "vervolg"},
            {"type": "input_image", "image_url": "data:image/png;base64,xx"},
        ]},
    ])
    assert "[system]\nwees kort" in prompt
    assert "User:\nvraag" in prompt
    assert "Assistant:\nantwoord" in prompt
    assert "vervolg" in prompt
    assert "base64" not in prompt  # images are skipped


def test_build_cmd_plain_chat_default_is_tool_less():
    # Default plain-chat is the ND3X planner brain: tool-less. Whitelist is the
    # no-tools sentinel (blocks every real/future CLI tool), no disallow list.
    from services.providers.claude_code_provider import _NO_TOOLS_SENTINEL
    p = ClaudeCodeChatProvider(default_model="sonnet")
    cmd = p._build_cmd("sonnet", "instructies")
    assert cmd[:2] == ["claude", "-p"]
    assert ["--model", "sonnet"] == cmd[2:4]
    system = cmd[cmd.index("--append-system-prompt") + 1]
    assert system.startswith("instructies") and "planning brain" in system
    assert ["--max-turns", str(NON_AGENTIC_MAX_TURNS)] == \
        cmd[cmd.index("--max-turns"):cmd.index("--max-turns") + 2]
    assert cmd[cmd.index("--allowedTools") + 1] == _NO_TOOLS_SENTINEL
    assert "--disallowedTools" not in cmd  # whitelist, not blocklist
    assert "--strict-mcp-config" in cmd
    assert "--permission-mode" not in cmd


def test_build_cmd_plain_chat_native_web_whitelists_only_web():
    # web_search_service opts in: WebSearch/WebFetch allowed, nothing else.
    p = ClaudeCodeChatProvider(default_model="sonnet", native_web=True)
    cmd = p._build_cmd("sonnet", None)
    allowed = cmd[cmd.index("--allowedTools") + 1]
    assert "WebSearch" in allowed and "WebFetch" in allowed
    assert "Bash" not in allowed and "Read" not in allowed
    assert "--disallowedTools" not in cmd


def test_build_cmd_plain_chat_native_groups():
    p = ClaudeCodeChatProvider(default_model="sonnet",
                               native_files=True, native_bash=False)
    cmd = p._build_cmd("sonnet", None)
    allowed = cmd[cmd.index("--allowedTools") + 1]
    assert "Read" in allowed and "Glob" in allowed and "Grep" in allowed
    assert "WebSearch" not in allowed and "Bash" not in allowed


def test_build_cmd_agentic_mode():
    p = ClaudeCodeChatProvider(
        default_model="opus", agentic=True, max_turns=8,
        allowed_tools="Bash(git:*) Read", cli_path="/opt/claude",
        extra_args=["--add-dir", "/tmp/ws"],
    )
    cmd = p._build_cmd("opus", None)
    assert cmd[0] == "/opt/claude"
    assert ["--permission-mode", "bypassPermissions"] == \
        cmd[cmd.index("--permission-mode"):cmd.index("--permission-mode") + 2]
    assert ["--allowedTools", "Bash(git:*) Read"] == \
        cmd[cmd.index("--allowedTools"):cmd.index("--allowedTools") + 2]
    assert ["--max-turns", "8"] == cmd[cmd.index("--max-turns"):cmd.index("--max-turns") + 2]
    assert NON_AGENTIC_DISALLOWED_TOOLS not in cmd
    assert cmd[-2:] == ["--add-dir", "/tmp/ws"]


def test_build_env_strips_api_key_and_injects_token(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leak")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "leak2")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = ClaudeCodeChatProvider(oauth_token="oat-abc")._build_env()
    # Both API-key vars must be gone or the CLI bills the API, not the subscription.
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oat-abc"
    assert env["PATH"] == "/usr/bin"
    # No token stored -> no var; host login (~/.claude) takes over.
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in ClaudeCodeChatProvider()._build_env()


def test_oauth_token_strips_embedded_whitespace():
    # A setup-token pasted from a wrapped terminal can carry an embedded newline;
    # the CLI rejects that as invalid. The provider strips ALL whitespace.
    p = ClaudeCodeChatProvider(oauth_token="sk-ant-oat01-AB\nCD 12\t34")
    assert p._oauth_token == "sk-ant-oat01-ABCD1234"
    assert ClaudeCodeChatProvider(oauth_token="  \n ")._oauth_token is None


def test_build_env_sets_is_sandbox_as_root(monkeypatch):
    # As root, the CLI refuses --permission-mode bypassPermissions unless IS_SANDBOX
    # is set. The provider marks it so the containerized (root) deploy works.
    monkeypatch.setattr("os.geteuid", lambda: 0, raising=False)
    assert ClaudeCodeChatProvider()._build_env().get("IS_SANDBOX") == "1"
    # Non-root: don't touch it (bypassPermissions is allowed for non-root).
    monkeypatch.setattr("os.geteuid", lambda: 1000, raising=False)
    assert "IS_SANDBOX" not in ClaudeCodeChatProvider()._build_env()
    # An explicit IS_SANDBOX is respected (root, but preset).
    monkeypatch.setattr("os.geteuid", lambda: 0, raising=False)
    monkeypatch.setenv("IS_SANDBOX", "0")
    assert ClaudeCodeChatProvider()._build_env()["IS_SANDBOX"] == "0"


def test_build_env_strips_nested_claude_code_session(monkeypatch):
    # ND3X launched from inside a Claude Code session (dev): the nested CLI
    # must not inherit that session's harness (it injects extra tools the
    # model then tries to call — the audit_e52491f1 failure).
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("CLAUDE_CODE_SSE_PORT", "12345")
    env = ClaudeCodeChatProvider(oauth_token="oat-abc")._build_env()
    assert "CLAUDECODE" not in env
    assert "CLAUDE_CODE_ENTRYPOINT" not in env
    assert "CLAUDE_CODE_SSE_PORT" not in env
    # Our own token var survives the prefix strip (set after filtering).
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oat-abc"


# --------------------------------------------------------------------- chat


def test_chat_parses_result_envelope(monkeypatch):
    proc = _FakeProc(stdout=json.dumps(_result_envelope()).encode())
    captured: dict = {}
    _patch_spawn(monkeypatch, proc, captured)
    p = ClaudeCodeChatProvider(default_model="sonnet", oauth_token="oat")

    result = asyncio.run(p.chat("hallo", instructions="wees kort"))

    assert result.text == "hoi"
    assert result.response_id == "sess-123"
    assert result.provider == "claude_code"
    assert result.model == "sonnet"
    assert result.usage["input_tokens"] == 100
    assert result.usage["total_cost_usd"] == 0.0123
    assert proc.stdin_data == b"hallo"
    assert ["--output-format", "json"] == \
        captured["cmd"][captured["cmd"].index("--output-format"):captured["cmd"].index("--output-format") + 2]
    assert captured["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "oat"


def test_chat_tolerates_noise_around_json(monkeypatch):
    noisy = b"npm warn something\n" + json.dumps(_result_envelope(result="ok")).encode() + b"\n"
    proc = _FakeProc(stdout=noisy)
    _patch_spawn(monkeypatch, proc, {})
    result = asyncio.run(ClaudeCodeChatProvider(default_model="sonnet").chat("x"))
    assert result.text == "ok"


def test_chat_error_envelope_raises(monkeypatch):
    envelope = _result_envelope(is_error=True, subtype="error_during_execution", result="boem")
    proc = _FakeProc(stdout=json.dumps(envelope).encode())
    _patch_spawn(monkeypatch, proc, {})
    with pytest.raises(RuntimeError, match="error_during_execution"):
        asyncio.run(ClaudeCodeChatProvider(default_model="sonnet").chat("x"))


def test_chat_nonzero_exit_raises_with_stderr(monkeypatch):
    proc = _FakeProc(stdout=b"", stderr=b"invalid api key", returncode=1)
    _patch_spawn(monkeypatch, proc, {})
    with pytest.raises(RuntimeError, match="invalid api key"):
        asyncio.run(ClaudeCodeChatProvider(default_model="sonnet").chat("x"))


def test_chat_timeout_kills_process(monkeypatch):
    proc = _FakeProc(hang=True)
    _patch_spawn(monkeypatch, proc, {})
    p = ClaudeCodeChatProvider(default_model="sonnet", timeout=0.05)
    with pytest.raises(RuntimeError, match="timeout"):
        asyncio.run(p.chat("x"))
    assert proc.killed


def test_chat_missing_cli_gives_helpful_error(monkeypatch):
    async def raise_not_found(*a, **k):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(ccp.asyncio, "create_subprocess_exec", raise_not_found)
    with pytest.raises(RuntimeError, match="niet gevonden"):
        asyncio.run(ClaudeCodeChatProvider(default_model="sonnet").chat("x"))


# ------------------------------------------------------------------- stream


def _stream_lines(*objs) -> bytes:
    return b"".join(json.dumps(o).encode() + b"\n" for o in objs)


async def _collect(p: ClaudeCodeChatProvider, prompt: str) -> list[str]:
    return [chunk async for chunk in p.chat_stream(prompt)]


def test_chat_stream_yields_partial_deltas(monkeypatch):
    lines = _stream_lines(
        {"type": "system", "subtype": "init"},
        {"type": "stream_event", "event": {"type": "content_block_delta",
                                           "delta": {"type": "text_delta", "text": "ho"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta",
                                           "delta": {"type": "text_delta", "text": "i"}}},
        # Buffered assistant message duplicates the deltas — must NOT be re-yielded.
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hoi"}]}},
        _result_envelope(),
    )
    proc = _FakeProc(stdout=lines)
    captured: dict = {}
    _patch_spawn(monkeypatch, proc, captured)
    chunks = asyncio.run(_collect(ClaudeCodeChatProvider(default_model="sonnet"), "hallo"))
    assert chunks == ["ho", "i"]
    assert "--include-partial-messages" in captured["cmd"]
    assert proc.stdin_data == b"hallo"


def test_chat_stream_falls_back_to_buffered_text(monkeypatch):
    # Older CLI without partial messages: only whole assistant messages arrive.
    lines = _stream_lines(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hoi"}]}},
        _result_envelope(),
    )
    _patch_spawn(monkeypatch, _FakeProc(stdout=lines), {})
    chunks = asyncio.run(_collect(ClaudeCodeChatProvider(default_model="sonnet"), "hallo"))
    assert chunks == ["hoi"]


def test_chat_stream_error_result_raises(monkeypatch):
    lines = _stream_lines(_result_envelope(is_error=True, subtype="error_max_turns", result=""))
    _patch_spawn(monkeypatch, _FakeProc(stdout=lines), {})
    with pytest.raises(RuntimeError, match="error_max_turns"):
        asyncio.run(_collect(ClaudeCodeChatProvider(default_model="sonnet"), "x"))


# ------------------------------------------------- factory/discovery/health


def test_factory_builds_claude_code_provider_from_config_json():
    import models.provider as pv
    from services.providers.provider_factory import _build_chat_provider

    p = pv.Provider(
        name="CC", provider_type="claude_code",
        config_json=json.dumps({"agentic": True, "cli_path": "/opt/claude",
                                "max_turns": 5, "timeout": 120}),
    )
    provider = _build_chat_provider(p, "oat-token", "opus", None)
    assert isinstance(provider, ClaudeCodeChatProvider)
    assert provider._agentic is True
    assert provider._cli_path == "/opt/claude"
    assert provider._max_turns == 5
    assert provider._timeout == 120
    assert provider._oauth_token == "oat-token"
    # No token + broken config_json -> still builds, with defaults
    p2 = pv.Provider(name="CC2", provider_type="claude_code", config_json="{niet json")
    provider2 = _build_chat_provider(p2, None, "sonnet", None)
    assert isinstance(provider2, ClaudeCodeChatProvider)
    assert provider2._agentic is False


def test_model_discovery_returns_static_aliases():
    from services.providers.model_discovery import discover_models

    out = discover_models(provider_type="claude_code", base_url=None, api_key=None)
    ids = [m["model_id"] for m in out["models"]]
    assert sorted(ids) == ["haiku", "opus", "sonnet"]  # _shape sorts by (capability, id)
    assert all(m["capability"] == "chat" for m in out["models"])


def test_health_check_reports_cli_presence(monkeypatch):
    import shutil
    from services.providers.health_service import check_provider

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/claude")
    ok = asyncio.run(check_provider(provider_type="claude_code", base_url=None, has_api_key=True))
    assert ok["status"] == "ok"

    monkeypatch.setattr(shutil, "which", lambda _: None)
    missing = asyncio.run(check_provider(provider_type="claude_code", base_url=None, has_api_key=True))
    assert missing["status"] == "unconfigured"


def test_factory_builds_tool_less_planner():
    # The factory-built chat provider is the ND3X planner brain: always
    # tool-less, regardless of config_json.native_* (those govern only the
    # separate web_search_service flow). Enabling a native tool in the planner
    # would make Claude Code run its own tool instead of producing the plan.
    import models.provider as pv
    from services.providers.provider_factory import _build_chat_provider

    p = pv.Provider(
        name="CCN", provider_type="claude_code",
        config_json=json.dumps({"native_web": True, "native_files": True}),
    )
    provider = _build_chat_provider(p, None, "sonnet", None)
    assert provider._native == {"web": False, "files": False, "bash": False}
    p2 = pv.Provider(name="CCN2", provider_type="claude_code")
    assert _build_chat_provider(p2, None, "sonnet", None)._native == \
        {"web": False, "files": False, "bash": False}


def test_web_search_capability_claude_code_default_on():
    from services.providers.web_search_capability import effective_web_search

    assert effective_web_search("claude_code", "opus", None) is True
    # Per-model UI override ("web: off") still wins.
    assert effective_web_search("claude_code", "opus", False) is False


def test_web_search_service_honors_native_web_choice(monkeypatch):
    import models.provider as pv
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from services.web_search_service import _claude_code as ws_claude_code
    from services.providers.base import ChatResult

    engine = create_engine("sqlite:///:memory:")
    pv.Provider.__table__.create(bind=engine)
    db = sessionmaker(bind=engine)()

    # native_web=false -> explicit choice: orchestrator owns web search.
    p_off = pv.Provider(name="CC-off", provider_type="claude_code",
                        config_json=json.dumps({"native_web": False}))
    db.add(p_off); db.commit()
    out = ws_claude_code(db, p_off.id, None, "opus", "weer Urmond", 5)
    assert out["ok"] is False and "native_web" in out["error"]

    # native_web on (default) -> searches via the CLI provider.
    p_on = pv.Provider(name="CC-on", provider_type="claude_code")
    db.add(p_on); db.commit()

    async def fake_chat(self, user_input, **kwargs):
        assert "WebSearch" in (kwargs.get("instructions") or "")
        return ChatResult(text="Morgen 22°C in Urmond (bron: knmi.nl)",
                          provider="claude_code", model="opus")

    monkeypatch.setattr(ccp.ClaudeCodeChatProvider, "chat", fake_chat)
    out = ws_claude_code(db, p_on.id, None, "opus", "weer Urmond", 5)
    assert out["ok"] is True and "Urmond" in out["answer"]
    db.close()


def test_claude_code_model_coercion():
    from services.providers.claude_code_provider import claude_code_model
    # Claude aliases + full ids pass through unchanged.
    assert claude_code_model("opus") == "opus"
    assert claude_code_model("sonnet") == "sonnet"
    assert claude_code_model("claude-opus-4-8") == "claude-opus-4-8"
    # Non-Claude models (a GPT/local pin from another slot) fall back.
    assert claude_code_model("gpt-5.4-mini") == "opus"
    assert claude_code_model("qwen2.5:14b") == "opus"
    assert claude_code_model(None) == "opus"
    assert claude_code_model("", default="sonnet") == "sonnet"
