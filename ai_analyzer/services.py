import json

from llm.client import client
from llm.prompts import BOM_SYSTEM_PROMPT, CARD_SYSTEM_PROMPT, MAPPING_SYSTEM_PROMPT
from schemas import LLM_MODEL, BomAnalysisResult, CardAnalysisResult, MappingResult


class BomAnalyzer:
    """
    Разбор BOM файла
    """
    MODEL = LLM_MODEL

    async def analyze(self, bom_snapshot: dict) -> BomAnalysisResult:

        # Обращаемся к механизму Structured Outputs
        response = client.beta.chat.completions.parse(
            model=self.MODEL,
            # Контекст беседы с нейросетью
            messages=[
                {
                    "role": "system",
                    "content": BOM_SYSTEM_PROMPT
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        bom_snapshot,
                        ensure_ascii=False
                    )
                }
            ],
            # Передаем Pydantic класс
            response_format=BomAnalysisResult
        )

        return response.choices[0].message.parsed


class CardsAnalyzer:
    """
    Разбор операционной карты
     """
    MODEL = LLM_MODEL

    async def analyze(self, cards: list[dict]) -> CardAnalysisResult:

        # Обращаемся к механизму Structured Outputs
        response = client.beta.chat.completions.parse(
            model=self.MODEL,
            # Контекст беседы с нейросетью
            messages=[
                {
                    "role": "system",
                    "content": CARD_SYSTEM_PROMPT
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        cards,
                        ensure_ascii=False
                    )
                }
            ],
            # Передаем Pydantic класс
            response_format=CardAnalysisResult
        )

        return response.choices[0].message.parsed


class MappingBuilder:
    """
    Маппинг
    """
    MODEL = LLM_MODEL

    # Принимаем результаты предыдущего анализа
    async def build(self, bom_analysis, card_analysis) -> MappingResult:

        # Превращаем Pydantic обратно в словари для json.dumps()
        payload = {
            "bom": bom_analysis.model_dump(),
            "cards": card_analysis.model_dump()
        }

        # Обращаемся к механизму Structured Outputs
        response = client.beta.chat.completions.parse(
            model=self.MODEL,
            # Контекст беседы с нейросетью
            messages=[
                {
                    "role": "system",
                    "content": MAPPING_SYSTEM_PROMPT
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        payload,
                        ensure_ascii=False
                    )
                }
            ],
            # Передаем Pydantic класс
            response_format=MappingResult
        )

        return response.choices[0].message.parsed