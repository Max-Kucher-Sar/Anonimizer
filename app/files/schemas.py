"""Схемы ответа файлового анонимайзера."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas import Strategy


class FileFindingOut(BaseModel):
    entity: str
    text: str
    score: float
    source: str
    location: str = Field(
        description="Где найдено: «стр. 2», «слайд 1 фигура 2», «абзац 14», путь в JSON."
    )


class FileReport(BaseModel):
    filename: str = Field(description="Имя возвращаемого файла.")
    original_format: str = Field(description="Расширение исходного файла.")
    media_type: str
    size_in: int
    size_out: int
    units: int = Field(description="Сколько текстовых блоков разобрано: абзацы, ячейки, слайды.")
    findings: list[FileFindingOut]
    counts: dict[str, int]
    strategy: Strategy
    elapsed_ms: float
    warnings: list[str] = Field(default_factory=list)
