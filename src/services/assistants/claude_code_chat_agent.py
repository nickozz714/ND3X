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
from services.providers.cli_agent_runner import CliAgentRunner

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


class ClaudeCodeChatAgent(CliAgentRunner):
    """Runs one chat turn as an autonomous Claude Code agent with ND3X tools."""

    def available(self) -> bool:
        return self.cli_agent_available()

    def _build_provider(self, model: Optional[str], mcp_config_path: Optional[str]):
        from services.providers.registry_service import ProviderRegistryService
        from services.providers.claude_code_provider import ClaudeCodeChatProvider

        p = self._resolve_cli_provider_row()
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
            # Skill-driven runs are long: many tool hops (each = a turn). 250 is a
            # runaway guard, not a work budget — the provider's agent timeout (2h
            # default) is the real cap. Override via config_json chat_max_turns.
            max_turns=cfg.get("chat_max_turns") or cfg.get("max_turns") or 250,
            timeout=cfg.get("timeout"),
            extra_args=extra_args,
        )

    @staticmethod
    def _to_prompt(user_input: Any) -> str:
        """Build the chat-turn prompt from the pipeline's plan_input.

        A plain string is the turn verbatim. For a multi-turn conversation we
        fence the earlier turns as ALREADY-HANDLED context and mark only the last
        user message as the current request. Without that split the autonomous
        agent re-runs actions from earlier turns — e.g. asked only to change the
        TV volume, it also re-issued a past "turn the living-room lights off"
        because that instruction was still sitting in the flattened transcript.
        """
        from services.providers.claude_code_provider import _to_prompt as _flatten
        if isinstance(user_input, str):
            return user_input
        msgs = list(user_input or [])
        if not msgs:
            return ""
        # The current request = the last user message; everything before it is
        # already-handled history.
        last_idx = None
        for i in range(len(msgs) - 1, -1, -1):
            if (msgs[i].get("role") or "user").strip().lower() == "user":
                last_idx = i
                break
        if last_idx is None:
            return _flatten(msgs)  # no user turn found — keep the old behaviour
        current = _flatten([msgs[last_idx]])
        if current.startswith("User:\n"):
            current = current[len("User:\n"):]  # we label it ourselves below
        history = msgs[:last_idx]
        if not history:
            return current
        return (
            "## Conversation so far — context only. These turns are ALREADY "
            "handled; do NOT repeat or re-run any of their actions.\n\n"
            f"{_flatten(history)}\n\n"
            "## Current request — the ONLY thing to act on now:\n\n"
            f"{current}"
        )

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
        # Skill-scoped gateway: skill-linked tools are only exposed when their
        # skill was selected for this turn (unlinked tools stay always-on).
        mcp_config_path = self.write_gateway_config(
            "nd3x-mcp-chat-", skill_names=[str(n) for n in (skill_names or [])])
        provider = self._build_provider(cc_model, mcp_config_path)
        instructions = _agent_instruction()
        # Dynamic ND3X inventory (connected MCP servers, skill catalog, selected
        # skills' file roots) — keeps the static preamble current from the DB.
        from services.providers.nd3x_agent_context import build_nd3x_context_block
        nd3x_ctx = build_nd3x_context_block(self.db, selected_skill_names=skill_names)
        if nd3x_ctx:
            instructions = f"{instructions}\n\n{nd3x_ctx}"
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
        resume_session_id: Optional[str] = None,
    ) -> str:
        """Run the turn and return the agent's natural-language answer."""
        provider, instructions, mcp_config_path, cc_model = self._prepare(model, extra_instructions, skill_names)
        prompt = self._to_prompt(user_input)
        log.infox("Claude Code chat-agent run gestart",
                  has_nd3x_tools=mcp_config_path is not None, prompt_chars=len(prompt or ""),
                  skills=skill_names or [], model=cc_model, resume=bool(resume_session_id))
        try:
            result = await provider.chat(prompt, instructions=instructions, model=cc_model,
                                         resume_session_id=resume_session_id)
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
        resume_session_id: Optional[str] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Stream typed agent events: 'session' (the CLI session id to persist),
        'thinking'/'tool' (the agent working — for the steps view) and 'answer'
        (the final reply — for the chat)."""
        provider, instructions, mcp_config_path, cc_model = self._prepare(model, extra_instructions, skill_names)
        prompt = self._to_prompt(user_input)
        log.infox("Claude Code chat-agent event-stream gestart",
                  has_nd3x_tools=mcp_config_path is not None, skills=skill_names or [],
                  model=cc_model, resume=bool(resume_session_id))
        try:
            async for ev in provider.chat_stream_events(prompt, instructions=instructions,
                                                        model=cc_model,
                                                        resume_session_id=resume_session_id):
                yield ev
        finally:
            if mcp_config_path:
                try:
                    os.unlink(mcp_config_path)
                except Exception:  # noqa: BLE001
                    pass
