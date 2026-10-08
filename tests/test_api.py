import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


SAMPLE = (
    "Иванов Иван Иванович; +7 (912) 345-67-89; ivan.ivanov@example.com; "
    "Меня зовут Иван, я работаю инженером-конструктором."
)


class TestEntityFilterValidation:
    def test_unknown_entity_is_rejected(self, client: TestClient) -> None:
        """Неизвестное имя сущности в массиве — это 422, а не молчание."""
        response = client.post(
            "/api/v1/anonymize/text",
            json={"text": SAMPLE, "entities": ["string"]},
        )

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert detail["error"] == "unknown_entities"
        assert detail["unknown"] == ["string"]
        assert "PERSON" in detail["supported_entities"]

    def test_partially_unknown_filter_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/anonymize/text",
            json={"text": SAMPLE, "entities": ["PERSON", "PII"]},
        )

        assert response.status_code == 422
        assert response.json()["detail"]["unknown"] == ["PII"]

    def test_valid_filter_is_accepted(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/anonymize/text",
            json={"text": SAMPLE, "entities": ["EMAIL_ADDRESS"]},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["counts"] == {"EMAIL_ADDRESS": 1}
        assert "ivan.ivanov@example.com" not in body["text"]

    def test_empty_filter_means_no_filter(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/anonymize/text",
            json={"text": SAMPLE, "entities": [], "mode": "fast"},
        )

        assert response.status_code == 200
        assert response.json()["counts"]

    def test_filter_is_the_same_in_text_and_file(self, client: TestClient) -> None:
        """Фильтр сущностей должен трактоваться одинаково в обеих ручках."""
        text = client.post(
            "/api/v1/anonymize/text",
            json={"text": SAMPLE, "entities": ["EMAIL_ADDRESS"]},
        )
        file_ = client.post(
            "/api/v1/anonymize/file?entities=EMAIL_ADDRESS&response_format=json",
            files={"file": ("a.txt", SAMPLE.encode(), "text/plain")},
        )

        assert text.status_code == file_.status_code == 200
        assert text.json()["counts"] == file_.json()["counts"]

    def test_spaces_and_duplicates_in_filter_are_tolerated(self, client: TestClient) -> None:
        """«EMAIL_ADDRESS, EMAIL_ADDRESS» и пустые элементы — тот же фильтр."""
        spaced = client.post(
            "/api/v1/anonymize/text",
            json={"text": SAMPLE, "entities": ["EMAIL_ADDRESS", " EMAIL_ADDRESS "]},
        )
        messy = client.post(
            "/api/v1/anonymize/text",
            json={"text": SAMPLE, "entities": ["EMAIL_ADDRESS", "", "  "]},
        )

        assert spaced.status_code == messy.status_code == 200
        assert spaced.json()["counts"] == messy.json()["counts"] == {"EMAIL_ADDRESS": 1}

    def test_string_filter_is_rejected_with_migration_hint(self, client: TestClient) -> None:
        """Старый формат-строка должен падать с подсказкой, а не молча."""
        response = client.post(
            "/api/v1/anonymize/text",
            json={"text": SAMPLE, "entities": "PERSON,EMAIL_ADDRESS"},
        )

        assert response.status_code == 422
        assert "массивом строк" in response.text
        assert "PERSON,EMAIL_ADDRESS" in response.text


class TestAnonymizeResponse:
    def test_covers_all_ru_pii(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/anonymize/text",
            json={"text": SAMPLE, "mode": "fast"},
        )

        body = response.json()
        assert "Иванов Иван Иванович" not in body["text"]
        assert "345-67-89" not in body["text"]
        assert "ivan.ivanov@example.com" not in body["text"]
        assert body["counts"]["PERSON"] >= 1
        assert body["counts"]["PHONE_NUMBER"] == 1

    def test_filter_yielding_nothing_warns(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/anonymize/text",
            json={
                "text": SAMPLE,
                "entities": ["CRYPTO"],
                "mode": "fast",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["text"] == SAMPLE
        assert body["warnings"], "пустой фильтр обязан сопровождаться предупреждением"

    def test_repeated_requests_return_same_findings(self, client: TestClient) -> None:
        """Повторный запрос без кэша обязан давать тот же результат."""
        payload = {"text": "Уникальный текст 4242: a@b.ru", "mode": "fast"}

        first = client.post("/api/v1/anonymize/text", json=payload).json()
        second = client.post("/api/v1/anonymize/text", json=payload).json()

        assert second["elapsed_ms"] >= 0
        assert [f["text"] for f in first["findings"]] == [f["text"] for f in second["findings"]]
        assert "cached" not in second


class TestServiceEndpoints:
    def test_health(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_entities_listing(self, client: TestClient) -> None:
        body = client.get("/api/v1/entities").json()
        assert "PERSON" in body["all"]
        assert "EMAIL_ADDRESS" in body["regex"]

    def test_openapi_documents_valid_example(self, client: TestClient) -> None:
        schema = client.get("/openapi.json").json()
        entities = schema["components"]["schemas"]["AnonymizeRequest"]["properties"]["entities"]
        examples = entities.get("examples") or [entities.get("example")]
        assert "PERSON" in examples[0]
        assert "EMAIL_ADDRESS" in examples[0]

    def test_openapi_documents_every_route(self, client: TestClient) -> None:
        """Несериализуемая схема в responses ломает /docs для всех маршрутов."""
        response = client.get("/openapi.json")

        assert response.status_code == 200
        paths = response.json()["paths"]
        for route in (
            "/api/v1/anonymize/text",
            "/api/v1/anonymize/file",
            "/health",
        ):
            assert route in paths, f"{route} отсутствует в OpenAPI"

class TestEntitiesEndpoint:
    """Контракт /api/v1/entities раньше не совпадал с тем, что реально находится."""

    def test_splits_entities_by_source(self, client: TestClient) -> None:
        body = client.get("/api/v1/entities").json()

        assert "INN_RU" in body["regex"]
        assert "QUANTITY" in body["presidio"]
        assert set(body["all"]) == set(body["regex"]) | set(body["presidio"])

    def test_filter_accepts_presidio_entity(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/anonymize/text",
            params={"mode": "fast"},
            json={"text": SAMPLE, "entities": ["QUANTITY"]},
        )

        assert response.status_code == 200

    def test_filter_rejects_bogus_entity(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/anonymize/text",
            params={"mode": "fast"},
            json={"text": SAMPLE, "entities": ["NOT_A_ENTITY"]},
        )

        assert response.status_code == 422
        assert response.json()["detail"]["error"] == "unknown_entities"


class TestRemovedModes:
    """balanced и deep исчезли вместе с LLM.

    Молча превращать их в fast нельзя: клиент попросил более тщательную
    проверку и получил бы более слабую, не заметив подмены.
    """

    @pytest.mark.parametrize("mode", ["balanced", "deep"])
    def test_rejected_loudly(self, client: TestClient, mode: str) -> None:
        response = client.post("/api/v1/anonymize/text", params={"mode": mode}, json={"text": SAMPLE})

        assert response.status_code == 422
        assert "LLM-эскалацией" in response.text

    def test_rejected_in_body(self, client: TestClient) -> None:
        response = client.post("/api/v1/anonymize/text", json={"text": SAMPLE, "mode": "deep"})

        assert response.status_code == 422
        assert "Остался один режим" in response.text

    @pytest.mark.parametrize("mode", ["balanced", "deep"])
    def test_rejected_by_file_route_too(self, client: TestClient, mode: str) -> None:
        response = client.post(
            "/api/v1/anonymize/file",
            params={"mode": mode},
            files={"file": ("a.txt", b"\xd0\x98\xd0\x9d\xd0\x9d 7707083893", "text/plain")},
        )

        assert response.status_code == 422
        assert "LLM-эскалацией" in response.text

    def test_fast_still_accepted(self, client: TestClient) -> None:
        response = client.post("/api/v1/anonymize/text", params={"mode": "fast"}, json={"text": SAMPLE})

        assert response.status_code == 200
        assert response.json()["mode"] == "fast"

    def test_no_llm_fields_in_response(self, client: TestClient) -> None:
        """В ответе не должно остаться полей про несуществующую фичу."""
        body = client.post("/api/v1/anonymize/text", json={"text": SAMPLE}).json()

        assert "llm_used" not in body
        assert "llm_used" not in client.get("/health").json()
        assert "llm_used" not in client.get("/api/v1/stats").json()
        assert "llm" not in client.get("/api/v1/entities").json()

    @pytest.mark.parametrize("param", ["mode", "entities"])
    def test_query_placeholder_not_silently_ignored(self, client: TestClient, param: str) -> None:
        """Клиент с /file пришлёт эти поля в query — молчать здесь нельзя."""
        value = "PERSON" if param == "entities" else "fast"
        response = client.post(
            "/api/v1/anonymize/text", params={param: value}, json={"text": SAMPLE}
        )

        if param == "mode":
            assert response.status_code == 200  # fast — это и есть значение по умолчанию
        else:
            assert response.status_code == 422
            assert response.json()["detail"]["error"] == "parameter_in_query_not_body"
