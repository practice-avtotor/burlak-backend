"""Synchronous ML service adapter for structure analysis and translation.

Uses ``httpx.Client`` (blocking) because Celery workers run in a synchronous
context. Calling async ``httpx.AsyncClient`` from a Celery task would require
``asyncio.run()`` per task — an anti-pattern.

Includes a simple circuit breaker and a ``ManualResponseNeeded`` exception
for handling the ml-mock manual mode (503 responses).

Typical usage inside a Celery task::

    from app.services.structure_adapter import StructureAdapter

    with StructureAdapter(settings.ml_service_url) as adapter:
        mapping = adapter.analyze_structure(snapshot_json)
        translations = adapter.translate_batch(unique_strings)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 120.0
_TRANSLATE_BATCH_SIZE = 200

_CB_FAILURE_THRESHOLD = 5
_CB_COOLDOWN_SECONDS = 60.0


class _CircuitBreaker:
    """Simple thread-safe circuit breaker for ML service calls."""

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
        if self._consecutive_failures < self._failure_threshold:
            return False
        elapsed = time.monotonic() - self._opened_at
        if elapsed >= self._cooldown_seconds:
            return False  # half-open: allow a probe
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
        if self.is_open:
            raise httpx.ConnectError(
                f"Circuit breaker open: ML service unreachable "
                f"(cooldown {self._cooldown_seconds:.0f}s, "
                f"failures={self._consecutive_failures})"
            )


class ManualResponseNeededError(Exception):
    """Raised when ML service is in manual mode and no response file is prepared."""

    def __init__(self, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


class StructureAdapter:
    """Synchronous HTTP client for the ML structure & translation service."""

    _circuit_breaker = _CircuitBreaker()

    def __init__(self, base_url: str, timeout: float = _DEFAULT_TIMEOUT) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
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
        """Send an XLSX snapshot to the ML service and receive a mapping config."""
        self._circuit_breaker.check()

        url = f"{self._base_url}/api/v1/analyze-structure"
        logger.info("Calling ML analyze-structure at %s", url)

        try:
            response = self._client.post(url, json=snapshot)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 503:
                # Manual mode: no response prepared — not retryable
                try:
                    raw = exc.response.json()
                except Exception:
                    raw = {}
                # FastAPI wraps in {"detail": {...}} — unwrap
                detail = raw.get("detail", raw) if isinstance(raw, dict) else {}
                raise ManualResponseNeededError(
                    detail.get("message", "ML manual response not prepared yet."),
                    detail=detail,
                ) from exc
            self._circuit_breaker.record_failure()
            raise

        self._circuit_breaker.record_success()
        result: dict[str, Any] = response.json()

        if result.get("status") == "error":
            error_info = result.get("error", {})
            raise ValueError(
                f"ML service error: {error_info.get('code', 'UNKNOWN')} — "
                f"{error_info.get('message', '')}"
            )

        mapping_config: dict[str, Any] | None = result.get("mapping_config")
        if mapping_config is None:
            raise ValueError("ML service response is missing 'mapping_config' field")
        logger.info("Received mapping_config with %d keys", len(mapping_config))
        return mapping_config

    def translate_batch(
        self,
        texts: list[str],
        source_lang: str = "zh",
        target_lang: str = "en",
    ) -> dict[str, str]:
        """Batch-translate unique strings via the ML translation service."""
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

                for src, translated in zip(chunk, batch_translations):
                    translations[src] = translated

            self._circuit_breaker.record_success()
        except Exception:
            self._circuit_breaker.record_failure()
            raise

        logger.info("Translated %d unique strings", len(translations))
        return translations
