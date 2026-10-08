"""Разрешение пересечений и замена сущностей в тексте."""

from __future__ import annotations

import hashlib

from app.schemas import Finding, Strategy

PLACEHOLDERS = {
    "PERSON": "ФИО",
    "ORGANIZATION": "ОРГАНИЗАЦИЯ",
    "LOCATION": "МЕСТО",
    "DATE_TIME": "ДАТА",
    "AGE": "ВОЗРАСТ",
    "EMAIL_ADDRESS": "EMAIL",
    "PHONE_NUMBER": "ТЕЛЕФОН",
    "URL": "ССЫЛКА",
    "IP_ADDRESS": "IP-АДРЕС",
    "PASSPORT_RU": "ПАСПОРТ",
    "INN_RU": "ИНН",
    "SNILS_RU": "СНИЛС",
    "BANK_ACCOUNT_RU": "СЧЁТ",
    "BIK_RU": "БИК",
    "CREDIT_CARD": "КАРТА",
    "IBAN_CODE": "IBAN",
    "CRYPTO": "КРИПТО",
    "US_SSN": "SSN",
    "CREDENTIAL": "ПАРОЛЬ",
    "QUANTITY": "ЧИСЛО",
    "MONEY": "СУММА",
}

MASK_CHAR = "*"


def resolve_overlaps(findings: list[Finding]) -> list[Finding]:
    """Оставляет непересекающиеся находки: сначала по score, затем по длине."""
    ordered = sorted(findings, key=lambda f: (-f.score, -f.length, f.start))

    accepted: list[Finding] = []
    for finding in ordered:
        if any(finding.start < other.end and other.start < finding.end for other in accepted):
            continue
        accepted.append(finding)

    return sorted(accepted, key=lambda f: f.start)


def placeholder_for(entity: str) -> str:
    return f"[{PLACEHOLDERS.get(entity, entity)}]"


def _replacement(finding: Finding, value: str, strategy: Strategy) -> str:
    if strategy is Strategy.REDACT:
        return ""
    if strategy is Strategy.MASK:
        return MASK_CHAR * max(4, min(len(value), 12))
    if strategy is Strategy.HASH:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
        return f"[{PLACEHOLDERS.get(finding.entity, finding.entity)}:{digest}]"
    return placeholder_for(finding.entity)


def redact(text: str, findings: list[Finding], strategy: Strategy) -> str:
    """Заменяет сущности справа налево, чтобы не сбивать смещения."""
    chunks: list[str] = []
    cursor = 0

    for finding in findings:
        value = finding.extract(text)
        if not value:
            continue
        chunks.append(text[cursor : finding.start])
        chunks.append(_replacement(finding, value, strategy))
        cursor = finding.end

    chunks.append(text[cursor:])
    return "".join(chunks)