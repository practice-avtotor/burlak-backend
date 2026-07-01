import time
from ai_analyzer.services import BomAnalyzer, CardsAnalyzer, MappingBuilder
from ai_analyzer.schemas import LLM_MODEL


class StructurePipeline:
    """
    Оркестратор
    """

    # Создаем объекты всех трех сервисов анализа
    def __init__(self):
        self.bom_analyzer = BomAnalyzer()
        self.cards_analyzer = CardsAnalyzer()
        self.mapping_builder = MappingBuilder()

    async def run(self, bom: list[dict], sample_cards: list[dict], options: dict | None = None):
        started = time.time()

        # Запускаем все сервисы анализа
        bom_result = await self.bom_analyzer.analyze(bom)
        card_result = await self.cards_analyzer.analyze(sample_cards)
        mapping_result = await self.mapping_builder.build(bom_result, card_result)

        # Собираем итоговый ответ
        return {
            "status": "success",
            "mapping_config": {
                "bom": bom_result.model_dump(),
                "cards": card_result.model_dump(),
                "mapping": mapping_result.model_dump(),
                "metadata": {
                    "analyzer_version": "1.0.0",
                    "processing_time_ms": int((time.time() - started) * 1000),
                    "model": LLM_MODEL,
                    "warnings": []
                }
            }
        }