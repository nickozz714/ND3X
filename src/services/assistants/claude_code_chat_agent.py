"""
services/assistants/claude_code_chat_agent.py

Chat integration for Claude Code as a FULL AGENT (option A), not the ND3X planner
brain. When claude_code is on the chat.planner slot, the pipeline hands the turn
to this agent instead of running the ND3X ReAct loop: Claude Code drives its own
agent loop with its own tools PLUS ND3X's tools, MCP servers (Fabric, …) and
skill tools via the mcp__nd3x gateway, and returns a natural-language answer that
ND3X shows as the chat reply.

Why: Claude Code is an autonomous agent — forcing it into the "produce a JSON
plan, ND3X executes" role makes it grab its own tools and stall on tool tasks
(error_max_turns). Here it does what it's built for, while ND3X stays the source
of truth for tools/skills/MCP via the gateway.
"""
from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator, Dict, List, Optional

from sqlalchemy.orm import Session

from component.logging import get_logger

log = get_logger(__name__)

def _agent_instruction() -> str:
    """Chat-turn instruction: the shared ND3X world-context + the chat tail.

    Built from ND3X_AGENT_PREAMBLE so the world-context (what ND3X is, that
    capabilities live under mcp__nd3x, the host-boundary, reply-language) can
    never drift from the workflow runner's — only the trailing role rule differs.
    """
    from services.providers.claude_code_provider import ND3X_AGENT_PREAMBLE
    return (
        ND3X_AGENT_PREAMBLE + "\n\n"
        "You are answering a chat turn. When you have done the work, give the "
        "user a clear, direct answer in natural language — that final message is "
        "what ND3X shows in the chat."
    )


class ClaudeCodeChatAgent:
    """Runs one chat turn as an autonomous Claude Code agent with ND3X tools."""

    def __init__(self, db: Session):
        self.db = db

    def _resolve_provider_row(self):
        from models.provider import Provider
        return (self.db.query(Provider)
                .filter(Provider.provider_type == "claude_code", Provider.enabled == True)  # noqa: E712
                .order_by(Provider.id.asc())
                .first())

    def available(self) -> bool:
        return self._resolve_provider_row() is not None

    def _build_provider(self, model: Optional[str], mcp_config_path: Optional[str]):
        from services.providers.registry_service import ProviderRegistryService
        from services.providers.claude_code_provider import ClaudeCodeChatProvider

        p = self._resolve_provider_row()
        if p is None:
            raise RuntimeError("No enabled Claude Code provider is registered.")
        key = ProviderRegistryService(self.db).get_api_key(p.id)
        cfg: Dict[str, Any] = {}
        try:
            cfg = json.loads(p.config_json or "{}") or {}
        except Exception:  # noqa: BLE001
            pass
        extra_args = list(cfg.get("extra_args") or [])
        if mcp_config_path:
            extra_args += ["--mcp-config", mcp_config_path]
        # The turn's model may be a non-Claude id (a GPT pin from another slot);
        # the CLI can't run those, so coerce to a Claude model.
        from services.providers.claude_code_provider import claude_code_model
        default_model = claude_code_model(model or cfg.get("default_model"))
        return ClaudeCodeChatProvider(
            default_model=default_model,
            oauth_token=key,
            cli_path=str(cfg.get("cli_path") or "claude"),
            agentic=True,  # full autonomous agent for the chat turn
            max_turns=cfg.get("chat_max_turns") or cfg.get("max_turns") or 40,
            timeout=cfg.get("timeout"),
            extra_args=extra_args,
        )

    @staticmethod
    def _write_gateway_config() -> Optional[str]:
        try:
            import tempfile
            from services.mcp.mcp_gateway import mcp_config_for_cli
            fd, path = tempfile.mkstemp(prefix="nd3x-mcp-chat-", suffix=".json")
            with os.fdopen(fd, "w") as f:
                json.dump(mcp_config_for_cli(), f)
            return path
        except Exception as exc:  # noqa: BLE001 — proceed without ND3X tools
            log.warningx("MCP gateway config voor chat schrijven mislukt", error=str(exc))
            return None

    @staticmethod
    def _to_prompt(user_input: Any) -> str:
        """Flatten the pipeline's plan_input (a conversation) into one prompt."""
        from services.providers.claude_code_provider import _to_prompt
        return _to_prompt(user_input)

    def _skill_instructions_block(self, skill_names: Optional[List[str]]) -> str:
        """Render the how-to instructions of the turn's selected skills so the
        agent knows how to use ND3X's skill tools, not just that they exist."""
        names = [str(n).strip() for n in (skill_names or []) if str(n).strip()]
        if not names:
            return ""
        try:
            from models.skill import Skill
            rows = self.db.query(Skill).filter(Skill.name.in_(names)).all()
        except Exception:  # noqa: BLE001
            return ""
        parts: List[str] = []
        for s in rows:
            instr = (getattr(s, "instructions", "") or "").strip()
            if instr:
                parts.append(f"### Skill: {s.name}\n{instr}")
        if not parts:
            return ""
        return ("Active ND3X skills for this turn — follow their guidance when "
                "using the related mcp__nd3x tools:\n\n" + "\n\n".join(parts))

    def _prepare(self, model: Optional[str], extra_instructions: Optional[str],
                 skill_names: Optional[List[str]]):
        """Shared setup for run/run_stream: gateway config, provider, prompt,
        instructions (agent + selected-skill how-to + any extra), and the
        Claude-coerced model to use (a non-Claude pin can't run in the CLI)."""
        from services.providers.claude_code_provider import claude_code_model
        cc_model = claude_code_model(model)
        mcp_config_path = self._write_gateway_config()
        provider = self._build_provider(cc_model, mcp_config_path)
        instructions = _agent_instruction()
        skills_block = self._skill_instructions_block(skill_names)
        if skills_block:
            instructions = f"{instructions}\n\n{skills_block}"
        if extra_instructions:
            instructions = f"{instructions}\n\n{extra_instructions}"
        return provider, instructions, mcp_config_path, cc_model

    async def run(
        self,
        *,
        user_input: Any,
        model: Optional[str] = None,
        extra_instructions: Optional[str] = None,
        skill_names: Optional[List[str]] = None,
    ) -> str:
        """Run the turn and return the agent's natural-language answer."""
        provider, instructions, mcp_config_path, cc_model = self._prepare(model, extra_instructions, skill_names)
        prompt = self._to_prompt(user_input)
        log.infox("Claude Code chat-agent run gestart",
                  has_nd3x_tools=mcp_config_path is not None, prompt_chars=len(prompt or ""),
                  skills=skill_names or [], model=cc_model)
        try:
            result = await provider.chat(prompt, instructions=instructions, model=cc_model)
        finally:
            if mcp_config_path:
                try:
                    os.unlink(mcp_config_path)
                except Exception:  # noqa: BLE001
                    pass
        log.infox("Claude Code chat-agent run afgerond", answer_chars=len(result.text or ""))
        return result.text or ""

    async def run_stream_events(
        self,
        *,
        user_input: Any,
        model: Optional[str] = None,
        extra_instructions: Optional[str] = None,
        skill_names: Optional[List[str]] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Stream typed agent events: 'thinking'/'tool' (the agent working — for
        the steps view) vs 'answer' (the final reply — for the chat)."""
        provider, instructions, mcp_config_path, cc_model = self._prepare(model, extra_instructions, skill_names)
        prompt = self._to_prompt(user_input)
        log.infox("Claude Code chat-agent event-stream gestart",
                  has_nd3x_tools=mcp_config_path is not None, skills=skill_names or [], model=cc_model)
        try:
            async for ev in provider.chat_stream_events(prompt, instructions=instructions, model=cc_model):
                yield ev
        finally:
            if mcp_config_path:
                try:
                    os.unlink(mcp_config_path)
                except Exception:  # noqa: BLE001
                    pass
