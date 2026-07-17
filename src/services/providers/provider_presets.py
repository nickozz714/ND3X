"""Known cloud provider presets for the guided "add provider" flow.

Static UI metadata (base URL, where to get an API key, what it's good for) so
users pick a provider from a list instead of hand-typing a provider_type + URL.
Includes cloud-hosted Llama options (Ollama Cloud / Groq / Together) which run
open models on credit/cheap-per-token plans.
"""
from __future__ import annotations

PRESETS = [
    {
        "key": "openai", "provider_type": "openai", "label": "OpenAI",
        "description": "GPT-4o & o-series chat, embeddings, Whisper, TTS, realtime.",
        "base_url": "https://api.openai.com/v1",
        "api_key_url": "https://platform.openai.com/api-keys",
        "needs_base_url": False, "is_local": False,
        "capabilities": ["chat", "embeddings", "transcription", "tts", "realtime"],
    },
    {
        "key": "anthropic", "provider_type": "anthropic", "label": "Anthropic",
        "description": "Claude models — strong, reliable general agents.",
        "base_url": "", "api_key_url": "https://console.anthropic.com/settings/keys",
        "needs_base_url": False, "is_local": False, "capabilities": ["chat"],
    },
    {
        # NOT is_local: only the CLI binary runs locally — the models are cloud
        # (subscription). is_local would hide it from the FE cloud-preset list
        # and trigger local-model treatment (light prompts) it doesn't need.
        "key": "claude_code", "provider_type": "claude_code", "label": "Claude Code (CLI)",
        "description": "Runs the local Claude Code CLI on your Claude subscription — no per-token "
                       "cost. Paste the token from `claude setup-token` as the API key, or leave "
                       "it empty to use this machine's `claude` login.",
        "base_url": "", "api_key_url": "",
        # Token is optional: without it the CLI uses the host's own login.
        "needs_base_url": False, "needs_api_key": False, "is_local": False,
        "capabilities": ["chat"],
    },
    {
        "key": "gemini", "provider_type": "gemini", "label": "Google Gemini",
        "description": "Gemini 1.5/2.0 — huge context windows, multimodal, cheap Flash tier.",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key_url": "https://aistudio.google.com/apikey",
        "needs_base_url": False, "is_local": False, "capabilities": ["chat", "embeddings"],
    },
    {
        "key": "voyage", "provider_type": "voyage", "label": "Voyage AI",
        "description": "High-quality retrieval embeddings.",
        "base_url": "https://api.voyageai.com/v1",
        "api_key_url": "https://dashboard.voyageai.com/",
        "needs_base_url": False, "is_local": False, "capabilities": ["embeddings"],
    },
    {
        "key": "azure_foundry", "provider_type": "azure_foundry", "label": "Azure AI Foundry",
        "description": "Azure-hosted models via the v1 OpenAI-compatible API — Azure OpenAI "
                       "(GPT-4o/4.1/o-series) plus DeepSeek, Grok, Llama, Phi and Mistral "
                       "deployments. Base URL = your resource endpoint "
                       "(https://<resource>.openai.azure.com — /openai/v1 is added "
                       "automatically). Model id = your DEPLOYMENT name, not the model name.",
        "base_url": "", "api_key_url": "https://ai.azure.com",
        "needs_base_url": True, "is_local": False, "capabilities": ["chat", "embeddings"],
    },
    {
        "key": "ollama_cloud", "provider_type": "openai_compatible", "label": "Ollama Cloud (Llama)",
        "description": "Cloud-hosted Llama & other open models on a monthly credit plan — "
                       "often cheaper than per-token APIs.",
        "base_url": "https://ollama.com/v1",
        "api_key_url": "https://ollama.com/settings/keys",
        "needs_base_url": False, "is_local": False, "capabilities": ["chat", "embeddings"],
    },
    {
        "key": "groq", "provider_type": "openai_compatible", "label": "Groq (Llama, fast)",
        "description": "Very fast Llama/Mixtral inference at low cost.",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key_url": "https://console.groq.com/keys",
        "needs_base_url": False, "is_local": False, "capabilities": ["chat"],
    },
    {
        "key": "together", "provider_type": "openai_compatible", "label": "Together AI (Llama)",
        "description": "Open models incl. Llama, cheap per-token.",
        "base_url": "https://api.together.xyz/v1",
        "api_key_url": "https://api.together.ai/settings/api-keys",
        "needs_base_url": False, "is_local": False, "capabilities": ["chat", "embeddings"],
    },
    {
        "key": "openai_compatible", "provider_type": "openai_compatible", "label": "Other OpenAI-compatible",
        "description": "Any OpenAI-compatible endpoint — set the base URL yourself.",
        "base_url": "", "api_key_url": "",
        "needs_base_url": True, "is_local": False, "capabilities": ["chat", "embeddings"],
    },
    {
        "key": "ollama", "provider_type": "ollama", "label": "Ollama (local)",
        "description": "Run open models locally on this machine — no API key, no per-token cost.",
        # Placeholder — get_presets() fills the effective host (OLLAMA_HOST env,
        # e.g. the Docker sidecar, else localhost) at read time.
        "base_url": "", "api_key_url": "",
        "needs_base_url": False, "is_local": True, "capabilities": ["chat", "embeddings"],
    },
]


def get_presets() -> list[dict]:
    """PRESETS with the Ollama base_url resolved to the effective default host —
    in a Docker deploy that is the compose sidecar (OLLAMA_HOST env), not the
    container's own localhost."""
    from services.local_models.ollama_client import DEFAULT_HOST
    out: list[dict] = []
    for p in PRESETS:
        if p.get("key") == "ollama" and not p.get("base_url"):
            p = {**p, "base_url": f"{DEFAULT_HOST}/v1"}
        out.append(p)
    return out
