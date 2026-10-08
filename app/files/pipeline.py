"""Диспетчер форматов файлов и лимиты."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.config import Settings
from app.files import office, plain
from app.files.segments import SegmentFinding, SegmentResult
from app.pipeline import AnonymizerPipeline
from app.schemas import Strategy

logger = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = plain.SUFFIXES + office.SUFFIXES

MEDIA_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".json": "application/json",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".log": "text/plain",
}


class UnsupportedFormatError(ValueError):
    """Расширение файла не поддерживается."""


class FileTooLargeError(ValueError):
    """Файл больше лимита."""


@dataclass
class FileResult:
    payload: bytes
    media_type: str
    findings: list[SegmentFinding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    units: int = 0
    elapsed_ms: float = 0.0

    def counts(self) -> dict[str, int]:
        counter: dict[str, int] = {}
        for finding in self.findings:
            counter[finding.entity] = counter.get(finding.entity, 0) + 1
        return counter


def suffix_of(filename: str) -> str:
    name = (filename or "").strip().lower()
    dot = name.rfind(".")
    return name[dot:] if dot > 0 else ""


def media_type_for(suffix: str) -> str:
    return MEDIA_TYPES.get(suffix, "application/octet-stream")


def describe(filename: str, suffix: str) -> str:
    stem = (filename or "file").rsplit(".", 1)[0]
    return f"{stem}.anonymized{suffix}"


class FilePipeline:
    """Гоняет файлы через общий текстовый пайплайн."""

    def __init__(self, pipeline: AnonymizerPipeline, settings: Settings) -> None:
        self._pipeline = pipeline
        self._settings = settings

    @property
    def supported_suffixes(self) -> list[str]:
        return list(SUPPORTED_SUFFIXES)

    def anonymize(
        self,
        payload: bytes,
        filename: str,
        strategy: Strategy = Strategy.PLACEHOLDER,
        entities: list[str] | None = None,
    ) -> FileResult:
        suffix = suffix_of(filename)

        if suffix not in SUPPORTED_SUFFIXES:
            raise UnsupportedFormatError(
                f"Формат «{suffix or 'без расширения'}» не поддерживается. "
                f"Доступны: {', '.join(SUPPORTED_SUFFIXES)}."
            )
        if len(payload) > self._settings.max_file_bytes:
            raise FileTooLargeError(
                f"Файл больше {self._settings.max_file_bytes // (1024 * 1024)} МБ."
            )
        if not payload:
            raise ValueError("Файл пустой.")

        kwargs = {
            "strategy": strategy,
            "entities": entities,
        }

        if suffix in office.SUFFIXES:
            handler = {
                ".docx": office.anonymize_docx,
                ".xlsx": office.anonymize_xlsx,
                ".pptx": office.anonymize_pptx,
            }[suffix]
            output, warnings, result = handler(payload, self._pipeline, **kwargs)
        elif suffix in plain.JSON_SUFFIXES:
            output, warnings, result = plain.anonymize_json(payload, self._pipeline, **kwargs)
        else:
            output, warnings, result = plain.anonymize_text(payload, self._pipeline, **kwargs)

        self._check_units(result)

        return FileResult(
            payload=output,
            media_type=media_type_for(suffix),
            findings=result.findings,
            warnings=warnings,
            units=result.units,
            elapsed_ms=result.elapsed_ms,
        )

    def _check_units(self, result: SegmentResult) -> None:
        """Ограничивает объём разобранного документа.

        Без предела разобранный .docx на 200 тысяч абзацев съест память
        целиком, и виноват будет уже не размер файла.
        """
        if len(result.texts) > self._settings.max_office_segments:
            raise ValueError(
                f"В документе больше {self._settings.max_office_segments} текстовых "
                "блоков — разбейте файл на части."
            )
        if sum(len(text) for text in result.texts) > self._settings.max_text_chars:
            raise ValueError(
                f"Текста больше {self._settings.max_text_chars} символов."
            )
