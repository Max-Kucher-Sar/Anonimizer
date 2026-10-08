"""Детерминированный детектор: regex + проверка контрольных сумм.

Без внешних моделей и тяжёлых зависимостей. Проверка контрольных сумм — не
перестраховка, а основная защита: номер с неверной последней цифрой ИНН или СНИЛС
не должен попасть в отчёт как найденный.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from app.checksums import is_credit_card, is_inn, is_russian_passport, is_snils
from app.schemas import Finding

CONTEXT_WINDOW = 60


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


# Пробелы, неотличимые от обычного глазом, но ломающие регулярки. Word и Excel
# подставляют неразрывный пробел при переносе строк и выравнивании колонок, так
# что «45 08 123456» с U+00A0 не находилось ни одним детектором.
_INVISIBLE_SPACES = (
    "\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007"
    "\u2008\u2009\u200a\u202f\u205f\u3000\u200b\ufeff"
)
_SPACE_TABLE = str.maketrans({char: " " for char in _INVISIBLE_SPACES})


def normalize_spaces(text: str) -> str:
    """Заменяет невидимые пробелы на обычный, сохраняя длину строки.

    Каждая замена — один символ на один символ, поэтому смещения находок,
    посчитанные по нормализованному тексту, годятся и для исходного. На этом
    держится вся замена: анонимайзер правит оригинал по найденным границам.
    """
    return text.translate(_SPACE_TABLE)


# Разделитель групп внутри номера. Ноль, один или два символа: «45 08 123456»,
# «45  08  123456» (двойной пробел из вёрстки) и «45‑08‑123456» с неразрывным
# дефисом. Границы жёсткие, иначе на длинных цифровых сериях получится катастрофический
# бэктрекинг.
SEP = r"[ \u2010\u2011\-]{0,2}"


@dataclass(frozen=True)
class Pattern:
    entity: str
    regex: str
    score: float
    validator: Callable[[str], bool] | None = None
    context: tuple[str, ...] = field(default=())
    context_boost: float = 0.2


def _snils(value: str) -> bool:
    return is_snils(_digits(value))


PATTERNS: tuple[Pattern, ...] = (
    Pattern(
        entity="EMAIL_ADDRESS",
        regex=r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
        score=0.95,
    ),
    Pattern(
        entity="URL",
        regex=r"\b(?:https?://|www\.)[^\s<>\"']+",
        score=0.9,
    ),
    Pattern(
        entity="IP_ADDRESS",
        regex=r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
        score=0.9,
        validator=lambda v: all(0 <= int(p) <= 255 for p in v.split(".")),
    ),
    Pattern(
        entity="CRYPTO",
        regex=r"\b(?:[13][a-km-zA-HJ-NP-Z1-9]{25,34}|bc1[ac-hj-np-z02-9]{11,71})\b",
        score=0.85,
    ),
    Pattern(
        entity="IBAN_CODE",
        regex=rf"\b[A-Z]{{2}}\d{{2}}{SEP}(?:[A-Z0-9]{{4}}{SEP}){{2,7}}[A-Z0-9]{{1,4}}\b",
        score=0.8,
        validator=lambda v: 15 <= len(re.sub(r"\s", "", v)) <= 34,
    ),
    Pattern(
        entity="PHONE_NUMBER",
        regex=rf"(?:\+7|8){SEP}\(?\d{{3}}\)?{SEP}\d{{3}}{SEP}\d{{2}}{SEP}\d{{2}}\b",
        score=0.9,
        validator=lambda v: len(_digits(v)) in (11,),
    ),
    Pattern(
        entity="CREDIT_CARD",
        regex=rf"\b(?:\d{SEP}){{12,18}}\d\b",
        score=0.9,
        validator=lambda v: is_credit_card(_digits(v)),
    ),
    Pattern(
        entity="INN_RU",
        regex=r"\b\d{12}\b|\b\d{10}\b",
        score=0.85,
        validator=lambda v: is_inn(_digits(v)),
        context=("инн", "налог", "inn"),
    ),
    Pattern(
        entity="SNILS_RU",
        # 3-3-3-2 и 3-3-2-3 — оба варианта записи СНИЛС встречаются на практике
        regex=(
            rf"\b\d{{3}}{SEP}\d{{3}}{SEP}\d{{3}}{SEP}\d{{2}}\b"
            rf"|\b\d{{3}}{SEP}\d{{3}}{SEP}\d{{2}}{SEP}\d{{3}}\b"
        ),
        score=0.85,
        validator=_snils,
        context=("снилс", "snils", "страховой"),
    ),
Pattern(
        entity="PASSPORT_RU",
        # допускаем слово-разделитель между серией и номером («серия 45 08 номер 123456»)
        regex=rf"\b\d{{2}}{SEP}\d{{2}}{SEP}(?:номер{SEP})?\d{{3}}{SEP}\d{{3}}\b",
        score=0.6,
        validator=is_russian_passport,
        context=("паспорт", "серия", "выдан", "рф", "россии"),
        context_boost=0.3,
    ),
    Pattern(
        entity="BANK_ACCOUNT_RU",
        regex=r"\b\d{20}\b|\b\d{17}\b",
        score=0.4,
        context=("р/с", "рс ", "расчетный счет", "счет", "счёт", "корреспондентский", "на счет", "на счёт"),
        context_boost=0.45,
    ),
    Pattern(
        entity="BIK_RU",
        regex=r"\b\d{9}\b",
        score=0.5,
        validator=lambda v: v.isdigit(),
        context=("бик", "bik"),
        context_boost=0.4,
    ),
    Pattern(
        entity="US_SSN",
        regex=r"\b\d{3}-\d{2}-\d{4}\b",
        score=0.9,
    ),
    Pattern(
        entity="CREDENTIAL",
        regex=r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|token|пароль)\s*[:=]\s*[\"']?(\S{6,})[\"']?",
        score=0.8,
    ),
)


class RegexDetector:
    """Находит сущности по регулярным выражениям с валидацией."""

    def __init__(self, patterns: tuple[Pattern, ...] = PATTERNS) -> None:
        self._compiled = [
            (pattern, re.compile(pattern.regex, re.IGNORECASE)) for pattern in patterns
        ]

    @property
    def entities(self) -> set[str]:
        return {pattern.entity for pattern, _ in self._compiled}

    def analyze(self, text: str) -> list[Finding]:
        # Длина строки при нормализации не меняется, поэтому смещения находок
        # остаются верными и для исходного текста, который правит анонимайзер.
        text = normalize_spaces(text)
        lowered = text.lower()
        findings: list[Finding] = []

        for pattern, regex in self._compiled:
            for match in regex.finditer(text):
                value = match.group(0)
                if pattern.validator is not None and not pattern.validator(value):
                    continue

                score = pattern.score
                if pattern.context:
                    window = lowered[max(0, match.start() - CONTEXT_WINDOW) : match.end() + CONTEXT_WINDOW]
                    if any(keyword in window for keyword in pattern.context):
                        score = min(1.0, score + pattern.context_boost)
                    elif score < 0.7:
                        continue

                findings.append(
                    Finding(
                        entity=pattern.entity,
                        start=match.start(),
                        end=match.end(),
                        score=score,
                        source="regex",
                    )
                )

        return findings