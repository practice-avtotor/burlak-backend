from __future__ import annotations

import json
import logging
from typing import Any, cast

import openai
import pydantic
from openai.types.chat import ChatCompletionMessageParam

from ai_analyzer.llm.client import get_client
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

logger = logging.getLogger(__name__)


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
    """
    Отправляет запрос в LLM через обычный chat.completions.create(),
    затем вручную парсит JSON из текстового ответа в Pydantic-схему.

    Это необходимо, потому что Ollama не поддерживает
    ``client.beta.chat.completions.parse()`` (Structured Outputs).
    """
    client = get_client()
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            response_format={"type": "json_object"},
        )

        # Проверка на пустой ответ
        if not response.choices:
            raise LLMAnalysisError(
                code="EMPTY_RESPONSE",
                message="LLM вернула пустой список choices",
                details={"model": model},
            )

        message = response.choices[0].message
        content = message.content

        # Проверка на отсутствие контента
        if content is None:
            # Если у сообщения есть finish_reason "length" — не хватило токенов
            finish_reason = response.choices[0].finish_reason
            if finish_reason == "length":
                raise LLMAnalysisError(
                    code="INCOMPLETE_OUTPUT",
                    message="LLM не хватило токенов для завершения структуры",
                    details={"model": model, "finish_reason": finish_reason},
                )
            raise LLMAnalysisError(
                code="EMPTY_CONTENT",
                message="LLM вернула пустое сообщение",
                details={"model": model, "finish_reason": finish_reason},
            )

        # Парсим JSON из текстового ответа
        try:
            parsed_data = json.loads(content)
        except json.JSONDecodeError as e:
            raise LLMAnalysisError(
                code="INVALID_JSON",
                message="LLM вернула невалидный JSON",
                details={"error": str(e), "model": model},
            )

        # Валидируем через Pydantic
        try:
            return response_format.model_validate(parsed_data)
        except pydantic.ValidationError as e:
            raise LLMAnalysisError(
                code="INVALID_MODEL_OUTPUT",
                message="LLM вернула данные, не соответствующие Pydantic-схеме",
                details={"validation_errors": e.errors(), "model": model},
            )

    except openai.BadRequestError as e:
        # Обработка ошибки "length" (превышение max_tokens)
        if e.code == "length":
            raise LLMAnalysisError(
                code="INCOMPLETE_OUTPUT",
                message="LLM не хватило токенов для завершения структуры",
                details={"model": model, "error": str(e)},
            )
        raise LLMAnalysisError(
            code="INVALID_REQUEST",
            message="Неверный запрос к LLM",
            details={"error": str(e), "model": model},
        )
    except openai.APIError as e:
        # Проблемы с доступом к Ollama (упал сервис, таймаут)
        raise LLMAnalysisError(
            code="LLM_API_ERROR",
            message="Ошибка соединения с LLM",
            details={"error": str(e), "model": model},
        )
    except LLMAnalysisError:
        # Пробрасываем уже наши ошибки дальше
        raise
    except Exception as e:
        # Любые другие непредвиденные сбои
        logger.exception("Unexpected error during LLM analysis")
        raise LLMAnalysisError(
            code="INTERNAL_ERROR",
            message="Неизвестная ошибка при анализе",
            details={"error": str(e), "model": model},
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
