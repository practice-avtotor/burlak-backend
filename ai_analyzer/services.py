from __future__ import annotations

import json
from typing import Any, cast

import openai
import pydantic
from openai.types.chat import ChatCompletionMessageParam

from ai_analyzer.llm.client import client
from ai_analyzer.llm.prompts import (
    BOM_SYSTEM_PROMPT,
    CARD_SYSTEM_PROMPT,
    MAPPING_SYSTEM_PROMPT,
)
from ai_analyzer.schemas import (
    LLM_MODEL,
    BomAnalysisResult,
    CardAnalysisResult,
    MappingResult,
)


class LLMAnalysisError(Exception):
    """
    Обработка ошибок парсинга LLM
    """

    def __init__(
        self, code: str, message: str, details: dict[str, object] | None = None
    ) -> None:
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(self.message)


async def safe_parse(
    model: str, messages: list[ChatCompletionMessageParam], response_format: type[Any]
) -> Any:
    try:
        import anyio

        response = await anyio.to_thread.run_sync(
            lambda: client.beta.chat.completions.parse(
                model=model, messages=messages, response_format=response_format
            )
        )
        return response.choices[0].message.parsed

    except pydantic.ValidationError as e:
        # Модель вернула JSON, но он не соответствует Pydantic-схеме
        raise LLMAnalysisError(
            code="INVALID_MODEL_OUTPUT",
            message="LLM вернула данные, не соответствующие Pydantic-схеме",
            details={"validation_errors": e.errors()},
        )
    except openai.LengthFinishReasonError:
        # Модели не хватило max_tokens, чтобы закрыть JSON
        raise LLMAnalysisError(
            code="INCOMPLETE_OUTPUT",
            message="LLM не хватило токенов для завершения структуры",
        )
    except openai.APIError as e:
        # Проблемы с доступом к Ollama (упал сервис, таймаут)
        raise LLMAnalysisError(
            code="LLM_API_ERROR",
            message="Ошибка соединения с LLM",
            details={"error": str(e)},
        )
    except Exception as e:
        # Любые другие непредвиденные сбои
        raise LLMAnalysisError(
            code="INTERNAL_ERROR",
            message="Неизвестная ошибка при анализе",
            details={"error": str(e)},
        )


class BomAnalyzer:
    """
    Разбор BOM файла
    """

    MODEL = LLM_MODEL

    async def analyze(
        self, bom_snapshots: list[dict[str, object]]
    ) -> BomAnalysisResult:
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": BOM_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(bom_snapshots, ensure_ascii=False)},
        ]
        result = await safe_parse(self.MODEL, messages, BomAnalysisResult)
        return cast(BomAnalysisResult, result)


class CardsAnalyzer:
    """
    Разбор операционной карты
    """

    MODEL = LLM_MODEL

    async def analyze(
        self, cards_snapshots: list[dict[str, object]]
    ) -> CardAnalysisResult:
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": CARD_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(cards_snapshots, ensure_ascii=False),
            },
        ]
        result = await safe_parse(self.MODEL, messages, CardAnalysisResult)
        return cast(CardAnalysisResult, result)


class MappingBuilder:
    """
    Маппинг
    """

    MODEL = LLM_MODEL

    # Принимаем результаты предыдущего анализа
    async def build(
        self, bom_analysis: BomAnalysisResult, card_analysis: CardAnalysisResult
    ) -> MappingResult:
        payload = {
            "bom": bom_analysis.model_dump(),
            "cards": card_analysis.model_dump(),
        }
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": MAPPING_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        result = await safe_parse(self.MODEL, messages, MappingResult)
        return cast(MappingResult, result)
