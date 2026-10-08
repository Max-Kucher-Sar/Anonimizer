"""Тесты движка кусков: склейка переводом строки и возврат по смещениям."""

from __future__ import annotations

import re

from app.files.segments import (
    BATCH_SIZE,
    Segment,
    _clip_to_segment,
    _join,
    _owner_of,
    anonymize_segments,
)
from app.pipeline import AnonymizeResult
from app.redact import redact
from app.schemas import Finding, Strategy

PERSON = r"Иванов\s+Иван\s+Иванович"


class StubPipeline:
    """Подмена пайплайна: режет всё, что похоже на ПДн, без spaCy."""

    def __init__(self, pattern: str = PERSON, entity: str = "PERSON") -> None:
        self.pattern = pattern
        self.entity = entity
        self.calls: list[str] = []

    def _findings(self, text: str) -> list[Finding]:
        found: list[Finding] = []
        cursor = 0
        while match := re.search(self.pattern, text[cursor:]):
            start = cursor + match.start()
            found.append(
                Finding(
                    entity=self.entity,
                    start=start,
                    end=start + len(match.group()),
                    score=1.0,
                    source="stub",
                )
            )
            cursor = start + len(match.group())
        return found

    def anonymize(self, text, strategy=Strategy.PLACEHOLDER, entities=None):
        self.calls.append(text)
        findings = self._findings(text)
        return AnonymizeResult(
            text=redact(text, findings, strategy),
            findings=findings,
        )


class TestJoin:
    def test_uses_plain_newline(self) -> None:
        """Метки внутри фразы ломают распознавание ФИО — их быть не должно."""
        joined, _ = _join([Segment(text="Иванов"), Segment(text="Иванович")])

        assert joined == "Иванов\nИванович"

    def test_reports_spans(self) -> None:
        joined, spans = _join([Segment(text="аб"), Segment(text="вгд")])

        assert joined == "аб\nвгд"
        assert spans == [(0, 2), (3, 6)]
        assert joined[spans[0][0] : spans[0][1]] == "аб"
        assert joined[spans[1][0] : spans[1][1]] == "вгд"

    def test_single_segment_has_no_separator(self) -> None:
        """Иначе у txt-файла пропада бы завершающий перевод строки."""
        text = "строка\nвторая\n"

        joined, spans = _join([Segment(text=text)])

        assert joined == text
        assert joined[spans[0][0] : spans[0][1]] == text


class TestOwnerLookup:
    def test_first(self) -> None:
        assert _owner_of([(0, 5), (6, 10)], 3) == 0

    def test_second(self) -> None:
        assert _owner_of([(0, 5), (6, 10)], 7) == 1

    def test_past_the_end_lands_on_last(self) -> None:
        assert _owner_of([(0, 5), (6, 10)], 99) == 1


class TestClipToSegment:
    def test_inside(self) -> None:
        clipped = _clip_to_segment(Finding(entity="PERSON", start=1, end=4), (0, 10))

        assert (clipped.start, clipped.end) == (1, 4)

    def test_overflowing_is_trimmed(self) -> None:
        """ФИО на две ячейки закрывает обе, а не выпадает целиком."""

        clipped = _clip_to_segment(Finding(entity="PERSON", start=5, end=25), (10, 20))

        assert (clipped.start, clipped.end) == (10, 20)

    def test_outside_yields_none(self) -> None:
        assert _clip_to_segment(Finding(entity="PERSON", start=0, end=3), (10, 20)) is None


class TestAnonymizeSegments:
    def test_single_pass(self) -> None:
        stub = StubPipeline()

        result = anonymize_segments(stub, [Segment(text="Иванов Иван Иванович", location="стр. 1")])

        assert result.texts == ["[ФИО]"]
        assert len(stub.calls) == 1

    def test_keeps_context_across_segments(self) -> None:
        """Главная причина склейки: ФИО в трёх соседних абзацах.

        Находка перекрывает все три куска, поэтому каждый из них закрывается
        целиком — иначе фамилия уехала бы в соседнюю ячейку.
        """
        segments = [
            Segment(text="Иванов", location="абзац 1"),
            Segment(text="Иван", location="абзац 2"),
            Segment(text="Иванович", location="абзац 3"),
        ]

        result = anonymize_segments(StubPipeline(), segments)

        assert result.texts == ["[ФИО]"] * 3

    def test_crossing_finding_never_leaks(self) -> None:
        """Часть находки, попавшая в кусок, обязана быть закрыта в нём же."""
        segments = [Segment(text="абзац про"), Segment(text="званка с")]

        result = anonymize_segments(StubPipeline(pattern=r"про[\s\S]*званка"), segments)

        assert "про" not in result.texts[0]
        assert "званка" not in result.texts[1]

    def test_writes_back_to_segment(self) -> None:
        segment = Segment(text="Иванов Иван Иванович")

        anonymize_segments(StubPipeline(), [segment])

        assert segment.anonymized == "[ФИО]"

    def test_reports_location(self) -> None:
        segments = [
            Segment(text="Иванов Иван Иванович", location="«Клиенты»!A2"),
            Segment(text="Иванов Иван Иванович", location="«Клиенты»!A5"),
        ]

        result = anonymize_segments(StubPipeline(), segments)

        assert sorted({finding.location for finding in result.findings}) == [
            "«Клиенты»!A2",
            "«Клиенты»!A5",
        ]

    def test_finding_keeps_original_text(self) -> None:
        result = anonymize_segments(
            StubPipeline(), [Segment(text="контакт: Иванов Иван Иванович", location="ячейка")]
        )

        assert result.findings[0].text == "Иванов Иван Иванович"

    def test_counts(self) -> None:
        segments = [Segment(text="Иванов Иван Иванович"), Segment(text="Иванов Иван Иванович")]

        assert anonymize_segments(StubPipeline(), segments).counts() == {"PERSON": 2}

    def test_empty_input(self) -> None:
        assert anonymize_segments(StubPipeline(), []).texts == []

    def test_untouched_segment_stays_byte_identical(self) -> None:
        segments = [Segment(text="ничего тут нет"), Segment(text="Иванов Иван Иванович")]

        result = anonymize_segments(StubPipeline(), segments)

        assert result.texts[0] == "ничего тут нет"

    def test_batches_long_documents(self) -> None:
        """Документ длиннее пачки режется на несколько проходов, а не одним."""
        count = BATCH_SIZE + 25
        stub = StubPipeline()
        segments = [Segment(text=f"абзац {index}") for index in range(count)]

        result = anonymize_segments(stub, segments)

        assert len(result.texts) == count
        assert len(stub.calls) == 2

    def test_units_counted(self) -> None:
        segments = [Segment(text="а"), Segment(text="б"), Segment(text="в")]

        assert anonymize_segments(StubPipeline(), segments).units == 3

    def test_strategy_applied(self) -> None:
        result = anonymize_segments(
            StubPipeline(), [Segment(text="Иванов Иван Иванович")], strategy=Strategy.REDACT
        )

        assert result.texts == [""]

