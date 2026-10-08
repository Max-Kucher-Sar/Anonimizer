"""Оркестрация пайплайна анонимизации текста."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from app.config import Settings
from app.detectors.context_rules import is_contract_label, is_technical_size, signatures
from app.detectors.ollama_detector import OllamaDetector
from app.detectors.presidio_detector import PresidioDetector
from app.detectors.regex_detector import RegexDetector
from app.redact import redact, resolve_overlaps
from app.schemas import Finding, Strategy

logger = logging.getLogger(__name__)


@dataclass
class AnonymizeResult:
    text: str
    findings: list[Finding]
    elapsed_ms: float = 0.0

    def counts(self) -> dict[str, int]:
        counter: dict[str, int] = {}
        for finding in self.findings:
            counter[finding.entity] = counter.get(finding.entity, 0) + 1
        return counter


@dataclass
class PipelineStats:
    requests: int = 0
    elapsed_ms: float = 0.0
    sources: dict[str, int] = field(default_factory=dict)


class AnonymizerPipeline:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._regex = RegexDetector()
        self._presidio = PresidioDetector(spacy_model=settings.spacy_model)
        self._stats = PipelineStats()
        self._ollama = OllamaDetector(settings) if settings.ollama_enabled else None

    @property
    def supported_entities(self) -> list[str]:
        """Объединение того, что действительно можно найти.

        Объединение regex-детекторов и распознавателей presidio. Раньше сюда
        ещё входили сущности LLM, которых больше нет.
        """
        return sorted(self.regex_entities | self.presidio_entities)

    @property
    def regex_entities(self) -> set[str]:
        return self._regex.entities

    @property
    def presidio_entities(self) -> set[str]:
        return self._presidio.supported_entities

    @property
    def stats(self) -> PipelineStats:
        return self._stats

    def anonymize(
        self,
        text: str,
        strategy: Strategy = Strategy.PLACEHOLDER,
        entities: list[str] | None = None,
    ) -> AnonymizeResult:
        started = time.perf_counter()

        wanted = set(entities) if entities else None

        findings: list[Finding] = []
        if wanted is None or wanted & self._regex.entities:
            findings += self._regex.analyze(text)

        try:
            findings += self._presidio.analyze(text, entities=wanted)
        except Exception:
            # Не должно подниматься: analyze() сам отсекает фильтры без своих
            # распознавателей. Если всплыло — это уже настоящая поломка.
            logger.exception("Presidio analysis failed")

        findings = [f for f in findings if wanted is None or f.entity in wanted]
        findings = [f for f in findings if not (is_technical_size(text, f) or is_contract_label(text, f))]
        if wanted is None or 'PERSON' in wanted:
            findings += signatures(text)
        if self._ollama is not None:
            findings = self._ollama.refine(text, findings, wanted)
            findings = [f for f in findings if not (is_technical_size(text, f) or is_contract_label(text, f))]

        resolved = resolve_overlaps(findings)
        anonymized = redact(text, resolved, strategy)

        result = AnonymizeResult(
            text=anonymized,
            findings=resolved,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )
        self._stats.requests += 1
        self._stats.elapsed_ms += result.elapsed_ms
        for finding in resolved:
            self._stats.sources[finding.source] = self._stats.sources.get(finding.source, 0) + 1

        return result
