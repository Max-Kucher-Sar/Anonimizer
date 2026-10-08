"""Общий прогон пайплайна по кускам документа с сохранением структуры.

Зачем склеивать: ФИО в xlsx часто разнесено по трём соседним ячейкам
(«Иванов» | «Иван» | «Иванович»), и по одной ячейке spaCy не находит сущность.
Склеив куски переводом строки, получаем связный текст — spaCy такое ФИО
узнаёт (проверено: «Иванов\\nИван\\nИванович» → одна сущность PERSON).

Почему не маркеры: искусственные метки вида <|SEGaab|> разрывают фразу, и
проверка показала обратное — с метками сущность не находится вообще.

Поэтому склейка идёт обычным переводом строки, а разбор обратно делается по
смещениям находок: их и так возвращает пайплайн, и пересчитывать вывод по
меткам не нужно.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.pipeline import AnonymizerPipeline
from app.redact import redact, resolve_overlaps
from app.schemas import Finding, Strategy

logger = logging.getLogger(__name__)

BATCH_SIZE = 4_000
"""Столько кусков за один проход.

Держит время и память предсказуемыми: spaCy на полумиллионе символов уже
занимает секунды, а на двухсоттысячном документе в один вызов не влезешь.
"""


@dataclass
class Segment:
    """Кусок текста документа и место, откуда он взят."""

    text: str
    location: str = ""
    anonymized: str | None = None

    def __post_init__(self) -> None:
        if self.anonymized is None:
            self.anonymized = self.text


@dataclass
class SegmentFinding:
    """Находка с человекочитаемым адресом: «Лист1!B7», «стр. 3», «абзац 14»."""

    entity: str
    text: str
    score: float
    source: str
    location: str


@dataclass
class SegmentResult:
    texts: list[str]
    findings: list[SegmentFinding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0
    units: int = 0
    """Сколько блоков разобрали: абзацы, ячейки, слайды."""

    def counts(self) -> dict[str, int]:
        counter: dict[str, int] = {}
        for finding in self.findings:
            counter[finding.entity] = counter.get(finding.entity, 0) + 1
        return counter


def _join(segments: list[Segment]) -> tuple[str, list[tuple[int, int]]]:
    """Склеивает куски переводом строки, возвращает текст и границы кусков."""
    parts: list[str] = []
    spans: list[tuple[int, int]] = []
    cursor = 0

    for index, segment in enumerate(segments):
        if index:
            parts.append("\n")
            cursor += 1
        parts.append(segment.text)
        spans.append((cursor, cursor + len(segment.text)))
        cursor += len(segment.text)

    return "".join(parts), spans


def _owner_of(spans: list[tuple[int, int]], offset: int) -> int:
    """Индекс куска, которому принадлежит смещение."""
    for index, (low, high) in enumerate(spans):
        if low <= offset < high:
            return index
    return len(spans) - 1


def _clip_to_segment(finding: Finding, span: tuple[int, int]) -> Finding | None:
    """Обрезает находку по границам куска.

    Сущность, растянувшаяся на две ячейки, применяется к каждой из них
    отдельно. Текст внутри ячейки при этом закрывается целиком — утечки нет,
    хотя заглушка может лечь посреди фразы.
    """
    start = max(finding.start, span[0])
    end = min(finding.end, span[1])
    if start >= end:
        return None
    return Finding(
        entity=finding.entity,
        start=start,
        end=end,
        score=finding.score,
        source=finding.source,
    )


def _locations(spans: list[tuple[int, int]], locations: list[str], finding: Finding) -> str:
    """Человекочитаемый адрес находки по её границам."""
    hits = [
        locations[index]
        for index, (low, high) in enumerate(spans)
        if finding.start < high and low < finding.end
    ]
    unique = list(dict.fromkeys(hits))
    return ", ".join(unique)


def anonymize_segments(
    pipeline: AnonymizerPipeline,
    segments: list[Segment],
    strategy: Strategy = Strategy.PLACEHOLDER,
    entities: list[str] | None = None,
) -> SegmentResult:
    """Анонимизирует куски одним проходом, раскладывая результат по кускам."""
    if not segments:
        return SegmentResult(texts=[])

    combined = SegmentResult(texts=[])

    for offset in range(0, len(segments), BATCH_SIZE):
        batch = segments[offset : offset + BATCH_SIZE]
        joined, spans = _join(batch)
        locations = [segment.location for segment in batch]

        outcome = pipeline.anonymize(
            text=joined,
            strategy=strategy,
            entities=entities,
        )
        combined.elapsed_ms += outcome.elapsed_ms

        # Находка достаётся всем кускам, которых касается, а замена делается
        # уже по их собственному тексту: так длины заглушек не влияют на
        # соседние куски, а ФИО на три абзаца закрывается во всех трёх.
        per_segment: list[list[Finding]] = [[] for _ in batch]
        for finding in outcome.findings:
            for index, span in enumerate(spans):
                clipped = _clip_to_segment(finding, span)
                if clipped is not None:
                    per_segment[index].append(clipped)

        for index, segment in enumerate(batch):
            local = [
                Finding(
                    entity=item.entity,
                    start=item.start - spans[index][0],
                    end=item.end - spans[index][0],
                    score=item.score,
                    source=item.source,
                )
                for item in resolve_overlaps(per_segment[index])
            ]
            combined.texts.append(redact(segment.text, local, strategy))

        for finding in outcome.findings:
            combined.findings.append(
                SegmentFinding(
                    entity=finding.entity,
                    text=finding.extract(joined),
                    score=finding.score,
                    source=finding.source,
                    location=_locations(spans, locations, finding),
                )
            )

    for segment, text in zip(segments, combined.texts):
        segment.anonymized = text

    combined.units = len(segments)
    return combined

