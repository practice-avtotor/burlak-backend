"""Synchronous ML service adapter for structure analysis and translation.

Uses ``httpx.Client`` (blocking) because Celery workers run in a synchronous
context. Calling async ``httpx.AsyncClient`` from a Celery task would require
``asyncio.run()`` per task — an anti-pattern that creates a new event loop
for each of the 1000 cards.

Typical usage inside a Celery task::

    from app.core.config import get_settings
    from app.services.structure_adapter import StructureAdapter

    settings = get_settings()
    adapter = StructureAdapter(settings.ml_service_url)
    mapping = adapter.analyze_structure(snapshot_json)
    translations = adapter.translate_batch(unique_strings)
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Default timeout for ML service calls (seconds).
# Structure analysis may be slow on first cold-start.
_DEFAULT_TIMEOUT = 120.0

# Maximum number of texts per translation batch.
_TRANSLATE_BATCH_SIZE = 200


class StructureAdapter:
    """Synchronous HTTP client for the ML structure & translation service.

    Args:
        base_url: Base URL of the ML service (e.g. ``http://ml-service:8000``).
        timeout: Request timeout in seconds.
    """

    def __init__(self, base_url: str, timeout: float = _DEFAULT_TIMEOUT) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        """Close the underlying HTTP client."""
        if hasattr(self._client, "close"):
            self._client.close()

    def __enter__(self) -> StructureAdapter:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze_structure(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Send an XLSX snapshot to the ML service and receive a mapping config.

        Args:
            snapshot: JSON-serialisable snapshot produced by
                :func:`~app.services.snapshot_service.extract_snapshot_from_bytes`.

        Returns:
            A ``mapping_config`` dict describing column coordinates,
            data start rows, file classification rules, etc.

        Raises:
            httpx.HTTPStatusError: If the ML service returns a non-2xx status.
            httpx.ConnectError: If the ML service is unreachable.
        """
        url = f"{self._base_url}/api/v1/analyze-structure"
        logger.info("Calling ML analyze-structure at %s", url)

        response = self._client.post(url, json=snapshot)
        response.raise_for_status()

        result: dict[str, Any] = response.json()
        logger.info("Received mapping_config with %d keys", len(result))
        return result

    def translate_batch(
        self,
        texts: list[str],
        source_lang: str = "zh",
        target_lang: str = "en",
    ) -> dict[str, str]:
        """Batch-translate unique strings via the ML translation service.

        Large lists are automatically chunked to avoid exceeding the ML
        service's request size limits.

        Args:
            texts: Deduplicated list of source-language strings.
            source_lang: ISO 639-1 source language code.
            target_lang: ISO 639-1 target language code.

        Returns:
            A mapping ``{source_text: translated_text}`` for every input string.

        Raises:
            httpx.HTTPStatusError: If the ML service returns a non-2xx status.
            httpx.ConnectError: If the ML service is unreachable.
        """
        if not texts:
            return {}

        url = f"{self._base_url}/api/v1/translate"
        translations: dict[str, str] = {}

        for i in range(0, len(texts), _TRANSLATE_BATCH_SIZE):
            chunk = texts[i : i + _TRANSLATE_BATCH_SIZE]
            logger.info(
                "Translating batch %d/%d (%d texts)",
                i // _TRANSLATE_BATCH_SIZE + 1,
                -(-len(texts) // _TRANSLATE_BATCH_SIZE),
                len(chunk),
            )

            response = self._client.post(
                url,
                json={
                    "texts": chunk,
                    "source_lang": source_lang,
                    "target_lang": target_lang,
                },
            )
            response.raise_for_status()

            data: dict[str, Any] = response.json()
            batch_translations: list[str] = data.get("translations", [])

            # Pair source texts with translations
            for src, translated in zip(chunk, batch_translations):
                translations[src] = translated

        logger.info("Translated %d unique strings", len(translations))
        return translations

