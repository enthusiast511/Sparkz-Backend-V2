"""
OpenAI Client
=============
Returns an async OpenAI client configured from settings.
"""

from typing import Optional

from openai import AsyncOpenAI

from app.config import settings

_client: Optional[AsyncOpenAI] = None


def get_openai_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY, timeout=120.0)
    return _client
