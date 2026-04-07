"""
Retry OpenAI chat completions on rate limits (429 TPM / RPM).

429 means the org hit tokens-per-minute (or requests-per-minute) caps across calls,
not that a single prompt exceeded the model's context window.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

from openai import RateLimitError

DEFAULT_MAX_RETRIES = 10
INITIAL_DELAY_SEC = 1.0
MAX_DELAY_SEC = 90.0


async def chat_completions_create_with_retry(
    client: Any,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    **kwargs: Any,
) -> Any:
    """Same keyword args as client.chat.completions.create; retries on RateLimitError with backoff + jitter."""
    delay = INITIAL_DELAY_SEC
    last_exc: RateLimitError | None = None

    for attempt in range(max_retries):
        try:
            return await client.chat.completions.create(**kwargs)
        except RateLimitError as e:
            last_exc = e
            if attempt >= max_retries - 1:
                raise
            jitter = random.uniform(0, 0.25 * delay)
            await asyncio.sleep(min(delay + jitter, MAX_DELAY_SEC))
            delay = min(delay * 1.8, MAX_DELAY_SEC)

    assert last_exc is not None
    raise last_exc
