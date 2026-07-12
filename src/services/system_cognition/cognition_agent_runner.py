"""
services/system_cognition/cognition_agent_runner.py

Cognition as a BLACKBOX call to a CLI-agent provider (Fase 3 of the agent-mode
framework). When the `chat.cognition` slot resolves to a CLI agent, instead of
running ND3X's multi-step structured pipeline (several LLM calls with a JSON
schema — which a CLI agent can't enforce), we hand the finished turn to the agent
ONCE and let a capable model decide + extract what's worth remembering, returning
a single JSON envelope that the orchestrator persists.

Runs in plain-chat mode (agentic=False): the provider is told to emit the JSON
envelope as text (no tools, no gateway) and we parse it tolerantly via the shared
CliAgentRunner.last_json_object.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from component.logging import get_logger
from services.providers.cli_agent_runner import CliAgentRunner

log = get_logger(__name__)

COGNITION_INSTRUCTION = (
    "You are ND3X's cognition module. You are given ONE finished conversation turn "
    "(the user's message and the assistant's answer). Decide what — if anything — is "
    "worth REMEMBERING for future turns.\n\n"
    "Remember DURABLE things: user preferences, project rules, architecture/technical "
    "decisions, constraints, corrections, and reusable facts. Do NOT remember volatile "
    "one-off lookups, transient status, or trivia with no future value. If nothing is "
    "worth remembering, return empty arrays — that is the common case.\n\n"
    "Reply with ONE JSON object and NOTHING else:\n"
    "{\n"
    '  "decision": "<one short line on what you kept and why>",\n'
    '  "memories": [ {"content": "<one self-contained sentence>", '
    '"type": "user_preference|project_context|architecture_decision|constraint|note", '
    '"scope": "thread|project|global", "importance": 0.0} ],\n'
    '  "beliefs": [ {"topic": "...", "summary": "...", "content": "...", "domain": "...", "confidence": 0.0} ],\n'
    '  "curiosity": [ {"topic": "...", "reason": "..."} ]\n'
    "}\n"
    "Prefer 0-3 memories; empty is fine. beliefs and curiosity are optional (usually empty)."
)


class CognitionAgentRunner(CliAgentRunner):
    """One blackbox extraction call to a CLI-agent provider."""

    def _build_provider(self, model: Optional[str]):
        from services.providers.registry_service import ProviderRegistryService
        from services.providers.claude_code_provider import ClaudeCodeChatProvider, claude_code_model

        p = self._resolve_cli_provider_row()
        if p is None:
            raise RuntimeError("No enabled CLI-agent provider is registered for cognition.")
        key = ProviderRegistryService(self.db).get_api_key(p.id)
        cfg: Dict[str, Any] = {}
        try:
            cfg = json.loads(p.config_json or "{}") or {}
        except Exception:  # noqa: BLE001
            pass
        return ClaudeCodeChatProvider(
            default_model=claude_code_model(model or cfg.get("default_model")),
            oauth_token=key,
            cli_path=str(cfg.get("cli_path") or "claude"),
            agentic=False,  # plain-chat: emit the JSON envelope, no tools/gateway
            timeout=cfg.get("timeout"),
        )

    async def extract(self, *, question: str, answer: str, model: Optional[str] = None) -> Dict[str, Any]:
        """Return {decision, memories[], beliefs[], curiosity[]}; tolerant of prose
        and of a non-JSON reply (falls back to empty extraction)."""
        provider = self._build_provider(model)
        prompt = f"USER MESSAGE:\n{question}\n\nASSISTANT ANSWER:\n{answer}"
        result = await provider.chat(prompt, instructions=COGNITION_INSTRUCTION)
        env = self.last_json_object(result.text or "")
        if not isinstance(env, dict):
            log.warningx("Agent-cognition: geen JSON-envelope in antwoord", chars=len(result.text or ""))
            return {"decision": "no-parse", "memories": [], "beliefs": [], "curiosity": []}

        def _list(key: str):
            v = env.get(key)
            return v if isinstance(v, list) else []

        return {
            "decision": str(env.get("decision") or ""),
            "memories": _list("memories"),
            "beliefs": _list("beliefs"),
            "curiosity": _list("curiosity"),
        }
