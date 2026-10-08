"""Детектор на базе Presidio: библиотека встроенных распознавателей + spaCy ru.

Ключевой момент: spaCy выдаёт метки PER/ORG/LOC, а Presidio ожидает
PERSON/ORGANIZATION/LOCATION. Без явного маппинга русские NER-сущности
молча теряются.
"""

from __future__ import annotations

import copy
import logging
from functools import lru_cache

from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
from presidio_analyzer.nlp_engine import NerModelConfiguration, SpacyNlpEngine

from app.schemas import Finding

logger = logging.getLogger(__name__)

SUPPORTED_LANGUAGES = ["ru", "en"]

SPACY_TO_PRESIDIO = {
    "PER": "PERSON",
    "PERSON": "PERSON",
    "ORG": "ORGANIZATION",
    "LOC": "LOCATION",
    "GPE": "LOCATION",
    "DATE": "DATE_TIME",
    "TIME": "DATE_TIME",
    "MONEY": "MONEY",
    "AGE": "AGE",
    # служебные числовые метки нам не нужны
    "CARDINAL": "QUANTITY",
    "ORDINAL": "QUANTITY",
    "PERCENT": "QUANTITY",
    "MONEY_ORDINAL": "QUANTITY",
}

# Распознаватели Presidio, которые по умолчанию ограничены английским,
# но по факту работают с любым алфавитом и цифрами.
LANGUAGE_AGNOSTIC_RECOGNIZERS = {
    "EmailRecognizer",
    "PhoneRecognizer",
    "CreditCardRecognizer",
    "IpRecognizer",
    "UrlRecognizer",
    "CryptoRecognizer",
    "IbanRecognizer",
    "UsSsnRecognizer",
}

# spaCy-модель ru_core_news_sm помечает короткие аббревиатуры и химические
# формулы как ORG: «ВХОД», «КОФ», «CaCl», «NaOH» получают score 0.85 — столько
# же, сколько настоящая организация, поэтому порогом это не отсечь.
# Отличить шум помогает форма: настоящие названия либо не сплошь в верхнем
# регистре, либо длиннее, либо содержат признак юрлица («ООО …», «… БАНК»).
_ORG_FORM_MARKERS = (
    "ооо",
    "пао",
    "зао",
    "ао",
    "нпо",
    "аоот",
    "компания",
    "фирма",
    "корпорация",
    "холдинг",
    "группа",
    "банк",
    "завод",
    "институт",
    "университет",
    "концерн",
)
_ORG_MAX_ABBREV_LENGTH = 6


def _is_abbreviation_org(text: str) -> bool:
    """True для короткого слова сплошь в верхнем регистре без признака юрлица.

    Такие токены («ВХОД», «КОФ») spaCy считает организациями, но для
    анонимайзера это ложное срабатывание: маскировать их — значит портить
    документ, не защищая никакие данные.
    """
    letters = [ch for ch in text if ch.isalpha()]
    if not letters or len(letters) > _ORG_MAX_ABBREV_LENGTH:
        return False
    if any(ch.islower() for ch in letters):
        return False
    lowered = text.lower()
    return not any(marker in lowered for marker in _ORG_FORM_MARKERS)


def _is_chemical_formula_org(text: str) -> bool:
    """True для короткого латинского токена вида «CaCl», «NaCl», «NaOH».

    Химические формулы состоят из заглавных букв элементов с необязательным
    индексом (CaCl2) и для маленькой модели выглядят как названия компаний.
    Настоящие организации по-русски так не пишутся: кириллица вперемешку с
    латиницей — это уже не имя собственное, а формула или марка реагента.
    """
    letters = [ch for ch in text if ch.isalpha()]
    if not letters or len(letters) > _ORG_MAX_ABBREV_LENGTH:
        return False
    if not all(ch.isascii() and ch.isalpha() for ch in letters):
        return False
    # как минимум две заглавные латинские буквы («CaCl»), иначе это обычное
    # слово («Вход» с кириллицей отсекла проверка ascii, «NaOH» проходит)
    capitals = sum(1 for ch in text if ch.isupper() and ch.isascii())
    return capitals >= 2


class PresidioDetector:
    """Обёртка над AnalyzerEngine."""

    def __init__(self, spacy_model: str = "ru_core_news_sm", score_threshold: float = 0.35) -> None:
        self._score_threshold = score_threshold
        self._engine = self._build_engine(spacy_model)

    @staticmethod
    def _build_engine(spacy_model: str) -> AnalyzerEngine:
        ner_configuration = NerModelConfiguration(
            model_to_presidio_entity_mapping=SPACY_TO_PRESIDIO,
            low_score_entity_names=["DATE", "TIME"],
            aggregation_strategy="MAX",
        )

        nlp_engine = SpacyNlpEngine(
            models=[{"lang_code": "ru", "model_name": spacy_model}],
            ner_model_configuration=ner_configuration,
        )
        nlp_engine.load()

        registry = RecognizerRegistry(supported_languages=SUPPORTED_LANGUAGES)
        engine = AnalyzerEngine(
            nlp_engine=nlp_engine,
            registry=registry,
            supported_languages=SUPPORTED_LANGUAGES,
        )

        PresidioDetector._enable_language_agnostic(registry)
        logger.info("Presidio recognizers registered: %d", len(registry.recognizers))
        return engine

    @staticmethod
    def _enable_language_agnostic(registry: RecognizerRegistry) -> None:
        """Дублирует «англоязычные» распознаватели для языка ru.

        Presidio хранит ровно один supported_language на распознаватель, а
        фильтр в get_recognizers сравнивает его с языком запроса. Распознаватели
        для email/телефона/карты работают на любом алфавите, поэтому просто
        копируем их и переключаем язык.
        """
        clones: list = []
        for recognizer in list(registry.recognizers):
            if type(recognizer).__name__ in LANGUAGE_AGNOSTIC_RECOGNIZERS:
                clone = copy.deepcopy(recognizer)
                clone.supported_language = "ru"
                clone.name = f"{recognizer.name}_RU"
                clones.append(clone)
        registry.recognizers.extend(clones)

    @property
    def supported_entities(self) -> set[str]:
        """Сущности, которые реально обслуживают зарегистрированные распознаватели.

        Список нужен не для красоты: в него попадает всё, что presidio умеет
        находить сам, иначе фильтр entities отвергал бы рабочие запросы.
        """
        return {entity for r in self._engine.registry.recognizers for entity in r.supported_entities}

    def analyze(self, text: str, entities: set[str] | None = None) -> list[Finding]:
        """Ищет сущности; фильтр, который нечем обслуживать, даёт пустой список.

        Важно: не поднимать ValueError на фильтре вида {"INN_RU"} — под него
        нет ни одного распознавателя presidio, это нормальный запрос.
        """
        if entities is not None and not entities & self.supported_entities:
            return []

        results = self._engine.analyze(
            text=text,
            language="ru",
            entities=sorted(entities) if entities else None,
            score_threshold=self._score_threshold,
        )
        findings = [
            Finding(
                entity=result.entity_type,
                start=result.start,
                end=result.end,
                score=float(result.score),
                source="presidio",
            )
            for result in results
        ]
        # Дочерняя модель плохо отличает короткие аббревиатуры от настоящих
        # организаций; порог не помогает (у обоих классов score 0.85), поэтому
        # ложные ORG-токены отсекаем по форме слова.
        return [finding for finding in findings if not self._is_false_org(text, finding)]

    @staticmethod
    def _is_false_org(text: str, finding: Finding) -> bool:
        """True, если ORGANIZATION-находка — это просто короткая аббревиатура.

        «ВХОД» и «КОФ» в верхнем регистре приходят от NER как ORG; оставлять
        их нельзя — анонимайзер испортит текст. Настоящие названия («Сбербанк»,
        «ООО «Газпромнефть-Заполярье»») не подпадают под форму аббревиатуры.
        """
        if finding.entity != "ORGANIZATION":
            return False
        value = finding.extract(text)
        return _is_abbreviation_org(value) or _is_chemical_formula_org(value)


@lru_cache(maxsize=4)
def get_presidio_detector(spacy_model: str = "ru_core_news_sm") -> PresidioDetector:
    return PresidioDetector(spacy_model=spacy_model)