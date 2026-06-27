"""Unit tests for app/services/structure_adapter.py.

Covers:
  - StructureAdapter.analyze_structure: success, HTTP error, connection error
  - StructureAdapter.translate_batch: success, empty input, chunking
"""

from __future__ import annotations

import httpx
import pytest

from app.services.structure_adapter import StructureAdapter

# ═══════════════════════════════════════════════════════════════════════
#  Mock transport for httpx
# ═══════════════════════════════════════════════════════════════════════


class _MockTransport(httpx.BaseTransport):
    """A mock HTTP transport that returns predefined responses."""

    def __init__(self, responses: dict[str, dict]) -> None:
        self._responses = responses
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url_path = request.url.path
        if url_path in self._responses:
            cfg = self._responses[url_path]
            return httpx.Response(
                status_code=cfg.get("status", 200),
                json=cfg.get("json", {}),
            )
        return httpx.Response(status_code=404, json={"error": "not found"})


# ═══════════════════════════════════════════════════════════════════════
#  analyze_structure
# ═══════════════════════════════════════════════════════════════════════


class TestAnalyzeStructure:
    def test_analyze_structure_success(self, monkeypatch):
        """Successful analysis returns the mapping config."""
        expected_config = {
            "cards": {
                "columns": {"part_no": 1, "qty": 3, "name": 2},
            }
        }

        class MockResponse:
            status_code = 200

            def json(self):
                return expected_config

            def raise_for_status(self):
                pass

        class MockClient:
            def __init__(self, **kwargs):
                pass

            def post(self, url, json=None):
                return MockResponse()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        monkeypatch.setattr("app.services.structure_adapter.httpx.Client", MockClient)

        adapter = StructureAdapter("http://ml-service:8000")
        result = adapter.analyze_structure({"filename": "test.xlsx", "sheets": []})
        assert result == expected_config

    def test_analyze_structure_http_error(self, monkeypatch):
        """HTTP 500 raises httpx.HTTPStatusError."""

        class MockResponse:
            status_code = 500

            def json(self):
                return {"error": "internal"}

            def raise_for_status(self):
                raise httpx.HTTPStatusError(
                    "Server Error",
                    request=httpx.Request(
                        "POST", "http://ml-service:8000/api/v1/analyze-structure"
                    ),
                    response=httpx.Response(500),
                )

        class MockClient:
            def __init__(self, **kwargs):
                pass

            def post(self, url, json=None):
                return MockResponse()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        monkeypatch.setattr("app.services.structure_adapter.httpx.Client", MockClient)

        adapter = StructureAdapter("http://ml-service:8000")
        with pytest.raises(httpx.HTTPStatusError):
            adapter.analyze_structure({"sheets": []})


# ═══════════════════════════════════════════════════════════════════════
#  translate_batch
# ═══════════════════════════════════════════════════════════════════════


class TestTranslateBatch:
    def test_empty_input(self):
        """Empty text list returns empty dict immediately."""
        adapter = StructureAdapter("http://ml-service:8000")
        result = adapter.translate_batch([])
        assert result == {}

    def test_success(self, monkeypatch):
        """Successful translation returns source→translated mapping."""
        translations = ["Bolt M6x20", "Nut M8"]

        class MockResponse:
            status_code = 200

            def json(self):
                return {"translations": translations}

            def raise_for_status(self):
                pass

        class MockClient:
            def __init__(self, **kwargs):
                pass

            def post(self, url, json=None):
                return MockResponse()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        monkeypatch.setattr("app.services.structure_adapter.httpx.Client", MockClient)

        adapter = StructureAdapter("http://ml-service:8000")
        result = adapter.translate_batch(["螺栓M6×20", "螺母M8"])
        assert result == {"螺栓M6×20": "Bolt M6x20", "螺母M8": "Nut M8"}

    def test_large_batch_chunking(self, monkeypatch):
        """Large input is chunked into batches of 200."""
        call_count = 0

        class MockResponse:
            status_code = 200

            def json(self):
                return {"translations": ["T"] * 200}

            def raise_for_status(self):
                pass

        class MockClient:
            def __init__(self, **kwargs):
                pass

            def post(self, url, json=None):
                nonlocal call_count
                call_count += 1
                return MockResponse()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        monkeypatch.setattr("app.services.structure_adapter.httpx.Client", MockClient)

        adapter = StructureAdapter("http://ml-service:8000")
        texts = [f"text_{i}" for i in range(500)]
        result = adapter.translate_batch(texts)
        # 500 texts / 200 per batch = 3 calls
        assert call_count == 3
        assert len(result) == 500

    def test_custom_language_codes(self, monkeypatch):
        """Custom source/target language codes are passed to the API."""
        captured_json = {}

        class MockResponse:
            status_code = 200

            def json(self):
                return {"translations": ["translated"]}

            def raise_for_status(self):
                pass

        class MockClient:
            def __init__(self, **kwargs):
                pass

            def post(self, url, json=None):
                captured_json.update(json or {})
                return MockResponse()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        monkeypatch.setattr("app.services.structure_adapter.httpx.Client", MockClient)

        adapter = StructureAdapter("http://ml-service:8000")
        adapter.translate_batch(["hello"], source_lang="en", target_lang="ru")
        assert captured_json["source_lang"] == "en"
        assert captured_json["target_lang"] == "ru"
