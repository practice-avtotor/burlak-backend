import asyncio
import time

from ai_analyzer.schemas import LLM_MODEL
from ai_analyzer.services import BomAnalyzer, CardsAnalyzer, MappingBuilder


class StructurePipeline:
    """
    Orchestrator that runs BOM analysis, card analysis (in parallel),
    then mapping building.
    """

    def __init__(self) -> None:
        self.bom_analyzer = BomAnalyzer()
        self.cards_analyzer = CardsAnalyzer()
        self.mapping_builder = MappingBuilder()

    async def run(
        self,
        bom: list[dict[str, object]],
        sample_cards: list[dict[str, object]],
        options: dict[str, object] | None = None,
    ) -> dict[str, object]:
        started = time.monotonic()

        # Run BOM and card analysis in parallel — they are independent
        bom_result, card_result = await asyncio.gather(
            self.bom_analyzer.analyze(bom),
            self.cards_analyzer.analyze(sample_cards),
        )

        mapping_result = await self.mapping_builder.build(bom_result, card_result)

        return {
            "status": "success",
            "mapping_config": {
                "bom": bom_result.model_dump(),
                "cards": card_result.model_dump(),
                "mapping": mapping_result.model_dump(),
                "metadata": {
                    "analyzer_version": "1.0.0",
                    "processing_time_ms": int((time.monotonic() - started) * 1000),
                    "model": LLM_MODEL,
                    "warnings": [],
                },
            },
        }
