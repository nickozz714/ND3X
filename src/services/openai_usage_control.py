# services/openai_usage_control.py

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from component.config import settings


@dataclass
class UsageEstimate:
    input_tokens: int
    reserved_output_tokens: int
    total_reserved_tokens: int
    max_input_tokens: int
    input_budget_ratio: float
    near_context_limit: bool



class OpenAIRateLimitRetry(Exception):
    pass


# Base64 image payloads in data URLs. Image tokens scale with pixel DIMENSIONS
# (roughly 1–2K tokens for a 1024px image), not with the base64 byte count — a
# 1 MB PNG must not be counted as ~350K text tokens (that false estimate made
# every vision call with a real photo fail the pre-flight size guard).
_DATA_URL_RE = re.compile(r"data:[a-zA-Z0-9.+/-]+;base64,[A-Za-z0-9+/=\\]{256,}")
_IMAGE_TOKENS_FLAT = 1600  # conservative per-image cost


def rough_token_count(value: Any) -> int:
    """
    Cheap approximation. Good enough for budget warnings and throttling.
    Replace later with tiktoken if wanted. Base64 data-URLs (images) are
    counted as a flat per-image cost instead of by character length.
    """
    try:
        raw = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        raw = str(value or "")

    raw, image_count = _DATA_URL_RE.subn("[image]", raw)
    return max(1, len(raw) // 4) + image_count * _IMAGE_TOKENS_FLAT


def estimate_openai_request_usage(
    *,
    req: Dict[str, Any],
    max_input_tokens: int,
    default_reserved_output_tokens: int,
) -> UsageEstimate:
    input_tokens = rough_token_count({
        "input": req.get("input"),
        "instructions": req.get("instructions"),
        "tools": req.get("tools"),
        "tool_resources": req.get("tool_resources"),
        "response_format": req.get("response_format"),
    })

    reserved_output_tokens = int(
        req.get("max_output_tokens")
        or default_reserved_output_tokens
    )

    ratio = input_tokens / max(max_input_tokens, 1)

    return UsageEstimate(
        input_tokens=input_tokens,
        reserved_output_tokens=reserved_output_tokens,
        total_reserved_tokens=input_tokens + reserved_output_tokens,
        max_input_tokens=max_input_tokens,
        input_budget_ratio=ratio,
        near_context_limit=ratio >= 0.85,
    )


class OpenAIRateLimiter:
    """
    Simple in-process RPM/TPM limiter.

    Important:
    - This does not know real OpenAI remaining headers.
    - It protects your own app from bursts.
    - It is per-process. If you run multiple workers, each worker has its own limiter.
    """

    def __init__(
        self,
        *,
        default_requests_per_minute: int,
        default_tokens_per_minute: int,
    ):
        self.default_requests_per_minute = int(default_requests_per_minute)
        self.default_tokens_per_minute = int(default_tokens_per_minute)
        self._events_by_model: Dict[str, List[dict]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def acquire(
        self,
        *,
        model: str,
        estimated_tokens: int,
        requests_per_minute: Optional[int] = None,
        tokens_per_minute: Optional[int] = None,
    ) -> None:
        rpm = int(requests_per_minute or self.default_requests_per_minute)
        tpm = int(tokens_per_minute or self.default_tokens_per_minute)
        model_key = model or "default"

        while True:
            async with self._lock:
                now = time.monotonic()

                events = [
                    e for e in self._events_by_model[model_key]
                    if now - float(e["ts"]) < 60.0
                ]
                self._events_by_model[model_key] = events

                used_requests = len(events)
                used_tokens = sum(int(e["tokens"]) for e in events)

                if used_requests + 1 <= rpm and used_tokens + estimated_tokens <= tpm:
                    events.append({
                        "ts": now,
                        "tokens": int(estimated_tokens),
                    })
                    return

                oldest = min((float(e["ts"]) for e in events), default=now)
                wait_s = max(0.25, 60.0 - (now - oldest))

            await asyncio.sleep(min(wait_s, 5.0))


def is_rate_limit_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    code = getattr(exc, "code", None)
    message = str(exc).lower()

    return (
        status_code == 429
        or code == "rate_limit_exceeded"
        or "rate limit" in message
        or "rate_limit" in message
        or "tokens per min" in message
        or "requests per min" in message
        or "tpm" in message
        or "rpm" in message
    )


def is_context_length_error(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    message = str(exc).lower()

    return (
        code == "context_length_exceeded"
        or "context_length_exceeded" in message
        or "maximum context length" in message
        or "context window" in message
        or "too many tokens" in message
    )


def retry_after_from_error(exc: Exception) -> Optional[float]:
    """
    Best effort. Some SDK errors expose headers; some only expose message text.
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)

    if headers:
        retry_after = headers.get("retry-after") or headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except Exception:
                pass

    message = str(exc)

    # Examples sometimes contain "try again in 8.64s"
    match = re.search(r"try again in ([0-9.]+)s", message, flags=re.IGNORECASE)
    if match:
        try:
            return float(match.group(1))
        except Exception:
            pass

    return None


async def sleep_for_rate_limit_retry(
    *,
    exc: Exception,
    attempt: int,
    default_wait_s: float,
    max_wait_s: float,
) -> float:
    retry_after = retry_after_from_error(exc)

    if retry_after is None:
        retry_after = min(
            max_wait_s,
            default_wait_s * (2 ** max(0, attempt - 1)),
        )

    # small jitter avoids multiple concurrent jobs waking at the exact same moment
    retry_after = min(max_wait_s, retry_after + random.uniform(0.0, 0.5))

    await asyncio.sleep(retry_after)
    return retry_after

GLOBAL_OPENAI_RATE_LIMITER = OpenAIRateLimiter(
    default_requests_per_minute=int(getattr(settings, "OPENAI_RPM", 120)),
    default_tokens_per_minute=int(getattr(settings, "OPENAI_TPM", 200000)),
)