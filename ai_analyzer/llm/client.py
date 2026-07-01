"""AsyncOpenAI client factory for Ollama.

Provides a lazily-initialised, reconfigurable ``AsyncOpenAI`` instance
that can be closed gracefully on shutdown.
"""

from __future__ import annotations

import logging
import os
from typing import AsyncIterator

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/v1")
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "ollama")

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    """Return the singleton ``AsyncOpenAI`` client, creating it if needed.

    The client is configured from environment variables so it can be
    overridden for tests (e.g. via ``OLLAMA_URL``).
    """
    global _client
    if _client is None:
        logger.info(
            "Creating AsyncOpenAI client: base_url=%s", OLLAMA_URL
        )
        _client = AsyncOpenAI(
            base_url=OLLAMA_URL,
            api_key=OLLAMA_API_KEY,
        )
    return _client


async def close_client() -> None:
    """Close the underlying HTTP client session, if it was created."""
    global _client
    if _client is not None:
        logger.info("Closing AsyncOpenAI client")
        await _client.close()
        _client = None


async def get_client_context() -> AsyncIterator[AsyncOpenAI]:
    """Async context manager that yields the client and closes it on exit.

    Usage::

        async with get_client_context() as client:
            ...
    """
    try:
        yield get_client()
    finally:
        await close_client()
