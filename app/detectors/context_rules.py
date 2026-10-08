"""Узкие правила для подписей и обозначений размеров оборудования."""

import re

from app.schemas import Finding

_INITIALS = r'[А-ЯЁ]\.[ \t\u00a0]*[А-ЯЁ]\.'
_SURNAME = r'[А-ЯЁ][а-яё]{2,}(?:-[А-ЯЁ][а-яё]+)?'
_SIGNATURE = re.compile(rf'(?:{_INITIALS}[ \t\u00a0]*{_SURNAME}|{_SURNAME}[ \t\u00a0]+{_INITIALS})')
_ROLE = re.compile(r'директор|инженер|начальник|руководител|специалист|исполнител|подписант|ответственн|согласова|утвержда|составил|проверил|фио|ф\.\s*и\.\s*о\.', re.IGNORECASE)
_SIZE = re.compile(r'(?:Ду|ДУ|DN|PN)\s*[=:]?\s*\d+(?:[.,]\d+)?', re.IGNORECASE)
_PARTY = re.compile(
    r'(?:покупател(?:ь|я|ю|ем|е|и|ей|ям|ями|ях)|'
    r'(?:поставщик|заказчик|участник|организатор|бенефициар|договор)(?:[ауеи]|ом|ов|ам|ами|ах)?|'
    r'документаци(?:я|и|ю|ей)|сторон(?:а|ы|у|е|ой|ам|ами|ах)?|положени(?:е|я|ю|ем|и))',
    re.IGNORECASE,
)


def signatures(text: str) -> list[Finding]:
    findings = []
    for match in _SIGNATURE.finditer(text):
        line_start = text.rfind('\n', 0, match.start()) + 1
        context = text[max(line_start, match.start() - 160):match.start()]
        if _ROLE.search(context) or re.search(r'_{3,}\s*$', context):
            findings.append(Finding(entity='PERSON', start=match.start(), end=match.end(),
                                    score=0.96, source='context_rule'))
    return findings


def is_technical_size(text: str, finding: Finding) -> bool:
    if finding.entity not in {'PERSON', 'ORGANIZATION', 'LOCATION'}:
        return False
    if finding.entity == 'PERSON' and _SIZE.search(finding.extract(text)):
        return True
    # The candidate must lie entirely inside the dimension, not a whole sentence.
    for match in _SIZE.finditer(text, max(0, finding.start - 8), min(len(text), finding.end + 32)):
        if match.start() <= finding.start and finding.end <= match.end():
            return True
    return False


def is_contract_label(text: str, finding: Finding) -> bool:
    """Одиночные обозначения сторон договора не являются ФИО."""
    return finding.entity == 'PERSON' and _PARTY.fullmatch(finding.extract(text).strip()) is not None
