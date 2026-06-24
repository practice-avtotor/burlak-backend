import time
from services import BomAnalyzer, CardsAnalyzer, MappingBuilder
from schemas import LLM_MODEL


class StructurePipeline:
    """
    Оркестратор
    """

    # Создаем объекты всех трех сервисов анализа
    def __init__(self):
        self.bom = BomAnalyzer()
        self.cards = CardsAnalyzer()
        self.mapping = MappingBuilder()

    async def run(self, bom_snapshot: dict, cards: list[dict]):
        started = time.time()

        # Запускаем все сервисы анализа
        bom_result = await self.bom.analyze(bom_snapshot)
        card_result = await self.cards.analyze(cards)
        mapping_result = await self.mapping.build(bom_result, card_result)

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