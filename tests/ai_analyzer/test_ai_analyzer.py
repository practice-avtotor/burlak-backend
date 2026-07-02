"""Unit tests for ai_analyzer schemas, services, and pipeline.

Tests use mocked ``AsyncOpenAI`` to avoid real LLM calls.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import openai
import pydantic
import pytest

from ai_analyzer.schemas import (
    AnalyzeStructureRequest,
    BomAnalysisResult,
    BomSheet,
    CardAnalysisResult,
    CardFormatMapping,
    CardSheetMapping,
    ColumnMapping,
    FieldMapping,
    MappingResult,
)
from ai_analyzer.services import (
    BomAnalyzer,
    CardsAnalyzer,
    LLMAnalysisError,
    MappingBuilder,
    safe_parse,
)

# ═══════════════════════════════════════════════════════════════════════
#  Schema tests
# ═══════════════════════════════════════════════════════════════════════


class TestColumnMapping:
    def test_valid(self):
        m = ColumnMapping(col_index=2, confidence=0.95)
        assert m.col_index == 2
        assert m.confidence == 0.95

    def test_confidence_clamped(self):
        with pytest.raises(pydantic.ValidationError):
            ColumnMapping(col_index=1, confidence=1.5)
        with pytest.raises(pydantic.ValidationError):
            ColumnMapping(col_index=1, confidence=-0.1)

    def test_col_index_negative(self):
        with pytest.raises(pydantic.ValidationError):
            ColumnMapping(col_index=-1, confidence=0.5)


class TestFieldMapping:
    def test_valid(self):
        m = FieldMapping(
            bom_column="part_no",
            card_column="part_no",
            match_type="exact",
            confidence=0.95,
        )
        assert m.confidence == 0.95

    def test_confidence_out_of_range(self):
        with pytest.raises(pydantic.ValidationError):
            FieldMapping(
                bom_column="part_no",
                card_column="part_no",
                match_type="exact",
                confidence=1.1,
            )


class TestBomSheet:
    def test_data_start_row_ge_1(self):
        with pytest.raises(pydantic.ValidationError):
            BomSheet(
                sheet_name="Sheet1",
                sheet_type="bom_data",
                header_rows=[1],
                data_start_row=0,
                total_data_rows_estimate=100,
                columns={
                    "part_no": {"col_index": 1, "confidence": 0.9},
                    "qty": {"col_index": 2, "confidence": 0.9},
                    "name_cn": {"col_index": 3, "confidence": 0.9},
                },
                layout={"type": "single_table", "description": "test"},
            )


class TestCardSheetMapping:
    def test_data_start_row_ge_1(self):
        with pytest.raises(pydantic.ValidationError):
            CardSheetMapping(
                sheet_name=None,
                sheet_type="card_data",
                header_rows=[1],
                data_start_row=0,
                columns={
                    "part_no": {"col_index": 1, "confidence": 0.9},
                    "name_cn": {"col_index": 2, "confidence": 0.9},
                    "qty": {"col_index": 3, "confidence": 0.9},
                },
                table_boundaries={"type": "empty_rows", "empty_rows_threshold": 3},
            )


class TestCardFormatMapping:
    def test_card_number_confidence_out_of_range(self):
        with pytest.raises(pydantic.ValidationError):
            CardFormatMapping(
                structure_type="standard_table",
                description="test",
                card_number_source="filename",
                card_number_pattern="test",
                card_number_confidence=1.5,
                sheets=[],
            )


class TestAnalyzeStructureRequest:
    def test_valid(self):
        req = AnalyzeStructureRequest(
            bom=[{"filename": "test.xlsx", "sheets": []}],
            sample_cards=[],
        )
        assert len(req.bom) == 1
        assert req.sample_cards == []

    def test_options_optional(self):
        req = AnalyzeStructureRequest(bom=[], sample_cards=[], options={"key": "value"})
        assert req.options == {"key": "value"}


# ═══════════════════════════════════════════════════════════════════════
#  safe_parse tests
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_openai_client():
    """Patch get_client() to return a mock AsyncOpenAI client."""
    with patch("ai_analyzer.services.get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_get_client.return_value = mock_client
        yield mock_client


class TestSafeParse:
    async def test_success(self, mock_openai_client):
        """Valid JSON response is parsed and validated."""
        mock_message = MagicMock()
        mock_message.content = json.dumps(
            {
                "sheets": [
                    {
                        "sheet_name": "Sheet1",
                        "sheet_type": "bom_data",
                        "header_rows": [1],
                        "data_start_row": 2,
                        "total_data_rows_estimate": 100,
                        "columns": {
                            "part_no": {"col_index": 1, "confidence": 0.9},
                            "qty": {"col_index": 2, "confidence": 0.9},
                            "name_cn": {"col_index": 3, "confidence": 0.9},
                        },
                        "layout": {
                            "type": "single_table",
                            "description": "Standard BOM table",
                        },
                    }
                ]
            }
        )

        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_choice.finish_reason = "stop"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        mock_openai_client.chat.completions.create = AsyncMock(
            return_value=mock_response
        )

        result = await safe_parse(
            "qwen2.5:7B",
            [{"role": "user", "content": "test"}],
            BomAnalysisResult,
        )
        assert isinstance(result, BomAnalysisResult)
        assert len(result.sheets) == 1
        assert result.sheets[0].sheet_name == "Sheet1"

    async def test_empty_choices(self, mock_openai_client):
        """Empty choices list raises EMPTY_RESPONSE error."""
        mock_response = MagicMock()
        mock_response.choices = []

        mock_openai_client.chat.completions.create = AsyncMock(
            return_value=mock_response
        )

        with pytest.raises(LLMAnalysisError) as exc_info:
            await safe_parse(
                "qwen2.5:7B",
                [{"role": "user", "content": "test"}],
                BomAnalysisResult,
            )
        assert exc_info.value.code == "EMPTY_RESPONSE"

    async def test_none_content_with_length_finish(self, mock_openai_client):
        """None content with finish_reason='length' raises INCOMPLETE_OUTPUT."""
        mock_message = MagicMock()
        mock_message.content = None

        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_choice.finish_reason = "length"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        mock_openai_client.chat.completions.create = AsyncMock(
            return_value=mock_response
        )

        with pytest.raises(LLMAnalysisError) as exc_info:
            await safe_parse(
                "qwen2.5:7B",
                [{"role": "user", "content": "test"}],
                BomAnalysisResult,
            )
        assert exc_info.value.code == "INCOMPLETE_OUTPUT"

    async def test_invalid_json(self, mock_openai_client):
        """Non-JSON content raises INVALID_JSON error."""
        mock_message = MagicMock()
        mock_message.content = "not valid json"

        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_choice.finish_reason = "stop"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        mock_openai_client.chat.completions.create = AsyncMock(
            return_value=mock_response
        )

        with pytest.raises(LLMAnalysisError) as exc_info:
            await safe_parse(
                "qwen2.5:7B",
                [{"role": "user", "content": "test"}],
                BomAnalysisResult,
            )
        assert exc_info.value.code == "INVALID_JSON"

    async def test_validation_error(self, mock_openai_client):
        """JSON that doesn't match schema raises INVALID_MODEL_OUTPUT."""
        mock_message = MagicMock()
        mock_message.content = json.dumps({"invalid": "data"})

        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_choice.finish_reason = "stop"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        mock_openai_client.chat.completions.create = AsyncMock(
            return_value=mock_response
        )

        with pytest.raises(LLMAnalysisError) as exc_info:
            await safe_parse(
                "qwen2.5:7B",
                [{"role": "user", "content": "test"}],
                BomAnalysisResult,
            )
        assert exc_info.value.code == "INVALID_MODEL_OUTPUT"

    async def test_bad_request_length_error(self, mock_openai_client):
        """BadRequestError with code='length' raises INCOMPLETE_OUTPUT."""
        mock_openai_client.chat.completions.create = AsyncMock(
            side_effect=openai.BadRequestError(
                message="max_tokens exceeded",
                response=MagicMock(),
                body=None,
            )
        )
        # Set code attribute
        mock_openai_client.chat.completions.create.side_effect.code = "length"

        with pytest.raises(LLMAnalysisError) as exc_info:
            await safe_parse(
                "qwen2.5:7B",
                [{"role": "user", "content": "test"}],
                BomAnalysisResult,
            )
        assert exc_info.value.code == "INCOMPLETE_OUTPUT"

    async def test_api_error(self, mock_openai_client):
        """APIError raises LLM_API_ERROR."""
        mock_openai_client.chat.completions.create = AsyncMock(
            side_effect=openai.APIError(
                message="connection failed",
                request=MagicMock(),
                body=None,
            )
        )

        with pytest.raises(LLMAnalysisError) as exc_info:
            await safe_parse(
                "qwen2.5:7B",
                [{"role": "user", "content": "test"}],
                BomAnalysisResult,
            )
        assert exc_info.value.code == "LLM_API_ERROR"


# ═══════════════════════════════════════════════════════════════════════
#  Analyzer tests
# ═══════════════════════════════════════════════════════════════════════


class TestBomAnalyzer:
    async def test_analyze(self, mock_openai_client):
        """BomAnalyzer.analyze returns BomAnalysisResult."""
        mock_message = MagicMock()
        mock_message.content = json.dumps(
            {
                "sheets": [
                    {
                        "sheet_name": "BOM",
                        "sheet_type": "bom_data",
                        "header_rows": [1],
                        "data_start_row": 2,
                        "total_data_rows_estimate": 50,
                        "columns": {
                            "part_no": {"col_index": 1, "confidence": 0.95},
                            "qty": {"col_index": 3, "confidence": 0.9},
                            "name_cn": {"col_index": 2, "confidence": 0.9},
                        },
                        "layout": {
                            "type": "single_table",
                            "description": "Standard BOM",
                        },
                    }
                ]
            }
        )
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_choice.finish_reason = "stop"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        mock_openai_client.chat.completions.create = AsyncMock(
            return_value=mock_response
        )

        analyzer = BomAnalyzer()
        result = await analyzer.analyze([{"filename": "test.xlsx", "sheets": []}])
        assert isinstance(result, BomAnalysisResult)


class TestCardsAnalyzer:
    async def test_analyze(self, mock_openai_client):
        """CardsAnalyzer.analyze returns CardAnalysisResult."""
        mock_message = MagicMock()
        mock_message.content = json.dumps(
            {
                "formats": {
                    "format_A": {
                        "structure_type": "standard_table",
                        "description": "Standard card format",
                        "card_number_source": "filename",
                        "card_number_pattern": "CARD-\\d+",
                        "card_number_confidence": 0.9,
                        "sheets": [
                            {
                                "sheet_name": None,
                                "sheet_type": "card_data",
                                "header_rows": [1],
                                "data_start_row": 2,
                                "columns": {
                                    "part_no": {"col_index": 1, "confidence": 0.9},
                                    "name_cn": {"col_index": 2, "confidence": 0.9},
                                    "qty": {"col_index": 3, "confidence": 0.9},
                                },
                                "table_boundaries": {
                                    "type": "empty_rows",
                                    "empty_rows_threshold": 3,
                                },
                            }
                        ],
                    }
                },
                "file_classification_rules": {
                    "operational_card_patterns": [],
                    "service_file_patterns": [],
                },
            }
        )
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_choice.finish_reason = "stop"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        mock_openai_client.chat.completions.create = AsyncMock(
            return_value=mock_response
        )

        analyzer = CardsAnalyzer()
        result = await analyzer.analyze([{"filename": "card.xlsx", "sheets": []}])
        assert isinstance(result, CardAnalysisResult)


class TestMappingBuilder:
    async def test_build(self, mock_openai_client):
        """MappingBuilder.build returns MappingResult."""
        mock_message = MagicMock()
        mock_message.content = json.dumps(
            {
                "bom_to_card": {
                    "part_no": {
                        "bom_column": "part_no",
                        "card_column": "part_no",
                        "match_type": "exact",
                        "confidence": 0.95,
                    },
                    "name": {
                        "bom_column": "name_cn",
                        "card_column": "name_cn",
                        "match_type": "fuzzy",
                        "confidence": 0.9,
                    },
                    "quantity": {
                        "bom_column": "qty",
                        "card_column": "qty",
                        "match_type": "exact",
                        "confidence": 0.95,
                    },
                }
            }
        )
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_choice.finish_reason = "stop"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        mock_openai_client.chat.completions.create = AsyncMock(
            return_value=mock_response
        )

        bom_result = BomAnalysisResult(
            sheets=[
                BomSheet(
                    sheet_name="BOM",
                    sheet_type="bom_data",
                    header_rows=[1],
                    data_start_row=2,
                    total_data_rows_estimate=50,
                    columns={
                        "part_no": {"col_index": 1, "confidence": 0.95},
                        "qty": {"col_index": 3, "confidence": 0.9},
                        "name_cn": {"col_index": 2, "confidence": 0.9},
                    },
                    layout={"type": "single_table", "description": "Standard BOM"},
                )
            ]
        )
        card_result = CardAnalysisResult(
            formats={},
            file_classification_rules={
                "operational_card_patterns": [],
                "service_file_patterns": [],
            },
        )

        builder = MappingBuilder()
        result = await builder.build(bom_result, card_result)
        assert isinstance(result, MappingResult)
        assert result.bom_to_card.part_no.match_type == "exact"
