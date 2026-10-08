import pytest

from app.config import Settings
from app.detectors.regex_detector import RegexDetector, normalize_spaces
from app.pipeline import AnonymizerPipeline
from app.redact import resolve_overlaps
from app.schemas import Finding, Strategy


@pytest.fixture(scope="module")
def detector() -> RegexDetector:
    return RegexDetector()


class TestRegexDetector:
    def test_finds_inn(self, detector: RegexDetector) -> None:
        findings = detector.analyze("Наш ИНН 7707083893, всё проверимо.")
        entities = {finding.entity for finding in findings}
        assert "INN_RU" in entities

    def test_rejects_inn_with_wrong_checksum(self, detector: RegexDetector) -> None:
        findings = detector.analyze("ИНН 7707083894 не проходит проверку.")
        assert all(finding.entity != "INN_RU" for finding in findings)

    def test_finds_snils(self, detector: RegexDetector) -> None:
        findings = detector.analyze("СНИЛС 123-456-78 121")
        assert any(finding.entity == "SNILS_RU" for finding in findings)

    def test_rejects_invalid_snils(self, detector: RegexDetector) -> None:
        findings = detector.analyze("СНИЛС 123-456-78 122")
        assert all(finding.entity != "SNILS_RU" for finding in findings)

    def test_email_and_phone(self, detector: RegexDetector) -> None:
        text = "Пишите на ivan.petrov@mail.ru или +7 916 123-45-67."
        entities = {finding.entity for finding in detector.analyze(text)}
        assert {"EMAIL_ADDRESS", "PHONE_NUMBER"} <= entities

    def test_credit_card_luhn(self, detector: RegexDetector) -> None:
        assert any(f.entity == "CREDIT_CARD" for f in detector.analyze("Карта 4539 1488 0343 6467"))
        assert not any(f.entity == "CREDIT_CARD" for f in detector.analyze("Карта 4539 1488 0343 6468"))

    def test_passport_requires_context(self, detector: RegexDetector) -> None:
        text = "Паспорт 45 08 123456, выдан ГУ МВД России."
        passport = [f for f in detector.analyze(text) if f.entity == "PASSPORT_RU"]
        assert passport
        assert passport[0].score > 0.6

    def test_passport_with_separator_word(self, detector: RegexDetector) -> None:
        text = "Паспорт серия 45 08 номер 123456."
        passport = [f for f in detector.analyze(text) if f.entity == "PASSPORT_RU"]
        assert passport
        assert "123456" in passport[0].extract(text)

    def test_bank_account_needs_context(self, detector: RegexDetector) -> None:
        text = "Р/с 40702810500000012345 в банке."
        assert any(f.entity == "BANK_ACCOUNT_RU" for f in detector.analyze(text))

    def test_spans_are_valid(self, detector: RegexDetector) -> None:
        text = "Контакты: a@b.ru, +7 916 123-45-67, ИНН 7707083893."
        for finding in detector.analyze(text):
            assert 0 <= finding.start < finding.end <= len(text)
            assert finding.extract(text).strip()


class TestOverlapResolution:
    def test_keeps_highest_score(self) -> None:
        findings = [
            Finding(entity="A", start=0, end=10, score=0.9),
            Finding(entity="B", start=5, end=15, score=0.4),
        ]
        resolved = resolve_overlaps(findings)
        assert len(resolved) == 1
        assert resolved[0].entity == "A"

    def test_keeps_longer_on_equal_score(self) -> None:
        findings = [
            Finding(entity="A", start=0, end=5, score=0.9),
            Finding(entity="B", start=3, end=20, score=0.9),
        ]
        resolved = resolve_overlaps(findings)
        assert len(resolved) == 1
        assert resolved[0].entity == "B"

    def test_disjoint_are_all_kept(self) -> None:
        findings = [
            Finding(entity="A", start=0, end=5, score=0.9),
            Finding(entity="B", start=6, end=10, score=0.4),
        ]
        assert len(resolve_overlaps(findings)) == 2


@pytest.fixture(scope="module")
def pipeline() -> AnonymizerPipeline:
    settings = Settings()
    return AnonymizerPipeline(settings)


class TestPipelineFastMode:
    def test_anonymizes_contact_block(self, pipeline: AnonymizerPipeline) -> None:
        text = "Меня зовут Иван Петров, пишите на ivan.petrov@mail.ru или +7 916 123-45-67."
        result = pipeline.anonymize(text)

        assert "ivan.petrov@mail.ru" not in result.text
        assert "+7 916 123-45-67" not in result.text
        assert "[EMAIL]" in result.text
        assert "[ТЕЛЕФОН]" in result.text

    def test_redact_strategy_removes_everything(self, pipeline: AnonymizerPipeline) -> None:
        text = "Пишите на ivan.petrov@mail.ru."
        result = pipeline.anonymize(text, strategy=Strategy.REDACT)
        assert "ivan" not in result.text

    def test_mask_strategy(self, pipeline: AnonymizerPipeline) -> None:
        text = "Пишите на ivan.petrov@mail.ru."
        result = pipeline.anonymize(text, strategy=Strategy.MASK)
        assert "*" in result.text
        assert "ivan.petrov" not in result.text

    def test_hash_strategy_is_stable(self, pipeline: AnonymizerPipeline) -> None:
        text = "Пишите на ivan.petrov@mail.ru."
        first = pipeline.anonymize(text, strategy=Strategy.HASH)
        second = pipeline.anonymize(text, strategy=Strategy.HASH)
        assert first.text == second.text
        assert "@" not in first.text

    def test_entity_filter(self, pipeline: AnonymizerPipeline) -> None:
        text = "Пишите на ivan.petrov@mail.ru или +7 916 123-45-67."
        result = pipeline.anonymize(text, entities=["EMAIL_ADDRESS"])
        assert "+7 916 123-45-67" in result.text
        assert "ivan.petrov@mail.ru" not in result.text

    def test_named_entity_from_spacy(self, pipeline: AnonymizerPipeline) -> None:
        text = "Слушатель Алексей Никитин оставил заявку."
        result = pipeline.anonymize(text)
        assert "Алексей" not in result.text or "Никитин" not in result.text

    def test_clean_text_untouched(self, pipeline: AnonymizerPipeline) -> None:
        text = "Погода сегодня хорошая, команда отработала спринт."
        result = pipeline.anonymize(text)
        assert result.text == text

    def test_findings_do_not_contain_pii(self, pipeline: AnonymizerPipeline) -> None:
        text = "ИНН 7707083893, СНИЛС 123-456-78 121, карта 4539 1488 0343 6467."
        result = pipeline.anonymize(text)
        assert "7707083893" not in result.text
        assert "4539148803436467" not in result.text.replace(" ", "")

class TestSupportedEntities:
    """Список сущностей — это контракт с клиентом, он должен совпадать с реальностью."""

    def test_includes_presidio_own_entities(self, pipeline: AnonymizerPipeline) -> None:
        """Раньше фильтр по QUANTITY отбивался 422, хотя presidio его находит."""
        assert "QUANTITY" in pipeline.supported_entities
        assert "MAC_ADDRESS" in pipeline.supported_entities

    def test_no_llm_only_entities_promised(self, pipeline: AnonymizerPipeline) -> None:
        """MEDICAL умел только LLM — обещать его после его удаления нельзя."""
        assert "MEDICAL" not in pipeline.supported_entities
        assert pipeline.regex_entities | pipeline.presidio_entities >= {
            "INN_RU",
            "QUANTITY",
        }


class TestInvisibleSpaces:
    """Word и Excel подставляют неразрывный пробел, и он не ломает поиск.

    Раньше «45 08 123456» с U+00A0, «4276 3800 1234 5679» и телефон
    «+7 912 345-67-89» не находились ни одним детектором и уходили клиенту
    в исходном виде — при том что рядом стоящие через обычный пробел
    находились.
    """

    NBSP = "\u00a0"
    CARD = "4276380012345679"
    SNILS = "11223344579"

    @staticmethod
    def _groups(value: str, size: int = 4) -> str:
        return " ".join(value[i : i + size] for i in range(0, len(value), size))

    @pytest.mark.parametrize("space", [" ", "\u00a0", "\u202f", "\u2009", "  ", "\u2011"])
    def test_card_is_found_with_any_separator(
        self, pipeline: AnonymizerPipeline, space: str
    ) -> None:
        card = self._groups(self.CARD).replace(" ", space)
        text = f"Карта: {card}"

        result = pipeline.anonymize(text)

        assert [f.entity for f in result.findings] == ["CREDIT_CARD"]
        assert result.text == "Карта: [КАРТА]"
        # замена обязана попасть ровно в исходный диапазон, несмотря на нормализацию
        assert result.findings[0].start == len("Карта: ")
        assert result.findings[0].end == len(text)

    @pytest.mark.parametrize("space", [" ", "\u00a0", "\u202f"])
    def test_phone_is_found_with_any_separator(
        self, pipeline: AnonymizerPipeline, space: str
    ) -> None:
        text = f"тел: +7{space}912{space}345-67-89"

        result = pipeline.anonymize(text)

        assert "PHONE_NUMBER" in {f.entity for f in result.findings}
        assert "912" not in result.text

    def test_passport_and_snils_survive_nbsp(
        self, pipeline: AnonymizerPipeline
    ) -> None:
        nb = self.NBSP
        text = f"паспорт 45{nb}08{nb}123456 и снилс {self.SNILS}"

        result = pipeline.anonymize(text)
        entities = {f.entity for f in result.findings}

        assert "PASSPORT_RU" in entities
        assert "SNILS_RU" in entities
        assert "123456" not in result.text
        assert "11223344579" not in result.text

    def test_normalization_preserves_offsets(self) -> None:
        """Нормализация обязана быть один-к-одному, иначе ломается замена."""
        probe = "a\u00a0b\u2009c\u202fd\u200be\ufefff  g"
        result = normalize_spaces(probe)

        assert len(result) == len(probe)
        assert result == "a b c d e f  g"

    def test_ordinary_text_is_not_broken(
        self, pipeline: AnonymizerPipeline
    ) -> None:
        text = "Обычный текст без номеров: 2024 год, сумма 1 500 рублей."
        result = pipeline.anonymize(text)

        assert result.findings == []
        assert result.text == text


class TestOrganizationAbbreviationFilter:
    """Короткие аббревиатуры («ВХОД», «КОФ») не должны считаться организациями."""

    def test_false_positives_are_dropped(self, pipeline: AnonymizerPipeline) -> None:
        text = "Максимальное давление на входе: ВХОД, фланцевое с КОФ под приварку."
        result = pipeline.anonymize(text)

        assert [f.entity for f in result.findings if f.entity == "ORGANIZATION"] == []
        assert result.text == text

    def test_real_organization_is_kept(self, pipeline: AnonymizerPipeline) -> None:
        text = "Контрагент — ООО «Газпромнефть-Заполярье», платежи через Сбербанк."
        result = pipeline.anonymize(text)

        orgs = [f.extract(text) for f in result.findings if f.entity == "ORGANIZATION"]
        assert any("Газпромнефть" in org for org in orgs)
        assert any("Сбербанк" in org for org in orgs)

    def test_mixed_case_abbreviation_is_kept(self, pipeline: AnonymizerPipeline) -> None:
        """Смешанный регистр («Банк», «Газпром») — это нормальные названия."""
        result = pipeline.anonymize("Штаб-квартира Газпрома в Москве, рядом офис ВТБ.")

        orgs = [f.extract("Штаб-квартира Газпрома в Москве, рядом офис ВТБ.") for f in result.findings]
        assert any("Газпром" in org for org in orgs)


class TestChemicalFormulaFilter:
    """Химические формулы («CaCl», «NaOH») не должны считаться организациями."""

    def test_chemical_formula_is_dropped(self, pipeline: AnonymizerPipeline) -> None:
        text = "Реагент CaCl добавляется в раствор NaCl."
        result = pipeline.anonymize(text)

        assert [f.entity for f in result.findings if f.entity == "ORGANIZATION"] == []
        assert result.text == text

    def test_formula_with_index_is_dropped(self, pipeline: AnonymizerPipeline) -> None:
        text = "Концентрация H2SO4 не должна превышать 0.1%."
        result = pipeline.anonymize(text)

        assert [f.entity for f in result.findings if f.entity == "ORGANIZATION"] == []
        assert result.text == text

    def test_latin_org_name_is_kept(self, pipeline: AnonymizerPipeline) -> None:
        """Обычные латинские названия (Samsung, Apple) не должны отсекаться."""
        text = "Партнёр — Samsung и Apple Inc."
        result = pipeline.anonymize(text)

        orgs = [f.extract(text) for f in result.findings if f.entity == "ORGANIZATION"]
        assert any("Samsung" in org for org in orgs)
        assert any("Apple" in org for org in orgs)


class TestPresidioFilterWithoutRecognizers:
    def test_regex_only_filter_is_not_an_error(self, pipeline: AnonymizerPipeline) -> None:
        """Под INN_RU нет ни одного распознавателя presidio.

        Раньше это давало ValueError, который проглатывался как ошибка анализа.
        """
        result = pipeline.anonymize("ИНН 7707083893.", entities={"INN_RU"})

        assert [f.entity for f in result.findings] == ["INN_RU"]

    def test_foreign_filter_skips_presidio_quietly(self, pipeline: AnonymizerPipeline) -> None:
        """Фильтр, который presidio обслужить не может, даёт пустой список без шума."""
        result = pipeline.anonymize(
            "Диагноз: гипертония.", entities={"MAC_ADDRESS"}
        )

        assert result.findings == []
