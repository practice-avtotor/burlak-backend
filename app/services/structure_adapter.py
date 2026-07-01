"""Synchronous ML service adapter for structure analysis and translation.

Uses ``httpx.Client`` (blocking) because Celery workers run in a synchronous
context. Calling async ``httpx.AsyncClient`` from a Celery task would require
``asyncio.run()`` per task — an anti-pattern that creates a new event loop
for each of the 1000 cards.

Includes a simple circuit breaker: after N consecutive failures the adapter
fails fast for a cooldown period, preventing cascading timeouts when the ML
service is unreachable.

Typical usage inside a Celery task::

    from app.core.config import get_settings
    from app.services.structure_adapter import StructureAdapter

    settings = get_settings()
    with StructureAdapter(settings.ml_service_url) as adapter:
        mapping = adapter.analyze_structure(snapshot_json)
        translations = adapter.translate_batch(unique_strings)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, cast

import httpx

logger = logging.getLogger(__name__)

# Default timeout for ML service calls (seconds).
# Structure analysis may be slow on first cold-start.
_DEFAULT_TIMEOUT = 120.0

# Maximum number of texts per translation batch.
_TRANSLATE_BATCH_SIZE = 200

# Circuit breaker settings
_CB_FAILURE_THRESHOLD = 5
_CB_COOLDOWN_SECONDS = 60.0


class _CircuitBreaker:
    """Simple circuit breaker for ML service calls.

    After ``failure_threshold`` consecutive failures the circuit opens and
    all calls fail fast for ``cooldown_seconds``.  After the cooldown the
    circuit half-opens, allowing a single probe request; on success it
    closes again.

    Thread-safe via ``threading.Lock``.
    """

    def __init__(
        self,
        failure_threshold: int = _CB_FAILURE_THRESHOLD,
        cooldown_seconds: float = _CB_COOLDOWN_SECONDS,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._consecutive_failures = 0
        self._opened_at: float = 0.0
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        """Return True if the circuit is open (failing fast)."""
        if self._consecutive_failures < self._failure_threshold:
            return False
        elapsed = time.monotonic() - self._opened_at
        if elapsed >= self._cooldown_seconds:
            # Half-open: allow a probe
            return False
        return True

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._failure_threshold:
                self._opened_at = time.monotonic()
                logger.warning(
                    "Circuit breaker OPEN after %d consecutive failures. "
                    "Failing fast for %.0f seconds.",
                    self._consecutive_failures,
                    self._cooldown_seconds,
                )

    def check(self) -> None:
        """Raise if the circuit is open."""
        if self.is_open:
            raise httpx.ConnectError(
                f"Circuit breaker open: ML service unreachable "
                f"(cooldown {self._cooldown_seconds:.0f}s, "
                f"failures={self._consecutive_failures})"
            )


class StructureAdapter:
    """Synchronous HTTP client for the ML structure & translation service.

    Args:
        base_url: Base URL of the ML service (e.g. ``http://ml-service:8000``).
        timeout: Request timeout in seconds.
    """

    # Module-level circuit breaker shared across all instances
    _circuit_breaker = _CircuitBreaker()

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
            httpx.ConnectError: If the ML service is unreachable or circuit is open.
        """
        self._circuit_breaker.check()

        url = f"{self._base_url}/api/v1/analyze-structure"
        logger.info("Calling ML analyze-structure at %s", url)

        try:
            response = self._client.post(url, json=snapshot)
            response.raise_for_status()
        except Exception:
            self._circuit_breaker.record_failure()
            raise

        self._circuit_breaker.record_success()
        result: dict[str, Any] = response.json()
        # Извлекаем mapping_config из ответа ML-сервиса
        mapping_config: dict[str, Any] = cast(
            dict[str, Any], result.get("mapping_config", result)
        )
        logger.info("Received mapping_config with %d keys", len(mapping_config))
        return mapping_config

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
            httpx.ConnectError: If the ML service is unreachable or circuit is open.
        """
        if not texts:
            return {}

        self._circuit_breaker.check()

        url = f"{self._base_url}/api/v1/translate"
        translations: dict[str, str] = {}

        try:
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

            # All chunks succeeded — record one success for the entire batch
            self._circuit_breaker.record_success()
        except Exception:
            self._circuit_breaker.record_failure()
            raise

        logger.info("Translated %d unique strings", len(translations))
        return translations
