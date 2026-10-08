"""Общие модели: внутренний Finding и публичные схемы API."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field, field_validator, model_validator


def parse_entity_filter(raw: list[str] | None) -> list[str] | None:
    """Нормализует список имён сущностей из тела запроса.

    Общий разбор для обеих ручек: срезает пробелы, убирает пустые элементы и
    дубликаты, сохраняя порядок. Пробелы и дубли клиент присылает легко —
    «PERSON, PERSON» и «PERSON,,» в одном массиве не должны ломать запрос.

    Пустой список и None означают «без фильтра»: возвращать [] заставило бы
    вызывающий код отличать «все сущности» от «ни одной».
    """
    if not raw:
        return None
    names = [name.strip() for name in raw]
    unique = list(dict.fromkeys(name for name in names if name))
    return unique or None



class Strategy(StrEnum):
    """Способ замены найденных сущностей."""

    PLACEHOLDER = "placeholder"
    MASK = "mask"
    REDACT = "redact"
    HASH = "hash"


class Finding(BaseModel):
    """Найденный фрагмент персональных данных."""

    entity: str
    start: int
    end: int
    score: float = 0.0
    source: str = "unknown"

    def extract(self, text: str) -> str:
        return text[self.start : self.end]

    @property
    def length(self) -> int:
        return self.end - self.start


class FindingOut(Finding):
    text: str


class AnonymizeRequest(BaseModel):
    text: Annotated[str, Field(min_length=1, max_length=200_000)]
    strategy: Strategy = Strategy.PLACEHOLDER
    language: str = "ru"
    entities: list[str] | None = Field(
        default=None,
        examples=[["PERSON", "EMAIL_ADDRESS"]],
        description=(
            "Белый список типов сущностей. None или пустой список — все "
            "поддерживаемые. Неизвестное имя возвращает ошибку 422."
        ),
    )

    @field_validator("entities", mode="before")
    @classmethod
    def _entities_as_list(cls, value: object) -> object:
        """Один элемент — не ошибка, но и не причина молча принимать строку.

        Строка — это прошлый формат («PERSON,EMAIL_ADDRESS»); клиент, который
        ещё не перешёл на массив, должен увидеть подсказку, а не получить 422
        без объяснения.
        """
        if isinstance(value, str):
            raise ValueError(  # noqa: TRY004
                "entities принимается массивом строк, например "
                f'["PERSON", "EMAIL_ADDRESS"], а не строкой «{value}».'
            )
        return value


class AnonymizeResponse(BaseModel):
    text: str
    original_length: int
    anonymized_length: int
    findings: list[FindingOut]
    counts: dict[str, int]
    strategy: Strategy
    elapsed_ms: float
    warnings: list[str] = Field(
        default_factory=list,
        description="Подозрительные, но не блокирующие условия ответа.",
    )


class HealthResponse(BaseModel):
    status: str
    spacy_model: str
    supported_entities: list[str]
    supported_formats: list[str] = Field(
        default_factory=list,
        description="Расширения файлов, которые принимает /api/v1/anonymize/file.",
    )
