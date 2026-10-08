"""Тесты /api/v1/anonymize/file."""

from __future__ import annotations

import io
import json

import pytest
from docx import Document
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from tests.file_fixtures import (
    ACCOUNT,
    CARD,
    EMAIL,
    INN,
    PASSPORT,
    PERSON,
    PHONE,
    SNILS,
    make_csv,
    make_docx,
    make_json,
    make_pptx,
    make_txt,
    make_xlsx,
)


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def post(client: TestClient, payload: bytes, name: str, url: str = "", **params):
    return client.post(
        f"/api/v1/anonymize/file{url}",
        files={"file": (name, payload, "application/octet-stream")},
        params=params,
    )


def text_of(payload: bytes) -> str:
    return payload.decode("utf-8")


# Нам нужен только факт «это PDF», а не валидный документ: проверяется отказ
# принимать формат, а не разбор содержимого.
FAKE_PDF = b"%PDF-1.4\nnot a real pdf\n"


class TestPlainFiles:
    def test_returns_same_format(self, client: TestClient) -> None:
        response = post(client, make_txt(), "договор.txt")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")

    def test_pii_removed(self, client: TestClient) -> None:
        body = text_of(post(client, make_txt(), "a.txt").content)

        for secret in (PASSPORT, INN, EMAIL, PHONE):
            assert secret not in body

    def test_suggested_filename(self, client: TestClient) -> None:
        """Кириллица в имени возможна только через filename*: в заголовке latin-1."""
        response = post(client, make_txt(), "договор.txt")
        disposition = response.headers["content-disposition"]

        assert "filename*=UTF-8''" in disposition
        assert "%D0%B4%D0%BE%D0%B3%D0%BE%D0%B2%D0%BE%D1%80" in disposition
        assert disposition.split("filename=")[1].split(";")[0].isascii()

    def test_headers(self, client: TestClient) -> None:
        response = post(client, make_txt(), "a.txt")

        assert int(response.headers["X-Anon-Found"]) >= 3
        assert response.headers["X-Anon-Units"] == "1"

    def test_csv_kept_parseable(self, client: TestClient) -> None:
        body = text_of(post(client, make_csv(), "clients.csv").content)

        rows = [line.split(",") for line in body.split("\n") if line]
        assert all(len(row) == 4 for row in rows)

    def test_json_still_valid(self, client: TestClient) -> None:
        body = post(client, make_json(), "a.json").content

        json.loads(text_of(body))

    def test_json_numbers_kept(self, client: TestClient) -> None:
        body = post(client, make_json(), "a.json").content

        assert isinstance(json.loads(text_of(body))["client"]["inn"], int)


class TestOfficeFiles:
    def test_docx(self, client: TestClient) -> None:
        response = post(client, make_docx(), "a.docx")

        assert response.status_code == 200
        document = Document(io.BytesIO(response.content))
        text = "\n".join(paragraph.text for paragraph in document.paragraphs)
        assert PASSPORT not in text

    def test_xlsx(self, client: TestClient) -> None:
        response = post(client, make_xlsx(), "a.xlsx")

        workbook = load_workbook(io.BytesIO(response.content))
        values = [str(cell.value) for sheet in workbook for row in sheet.iter_rows() for cell in row]
        assert PERSON not in " ".join(values)

    def test_pptx(self, client: TestClient) -> None:
        response = post(client, make_pptx(), "a.pptx")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.presentationml"
        )

    def test_office_media_types(self, client: TestClient) -> None:
        assert post(client, make_docx(), "a.docx").headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.wordprocessingml"
        )
        assert post(client, make_xlsx(), "a.xlsx").headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.spreadsheetml"
        )


class TestPdfRejected:
    """PDF не принимается: закрыть текст в готовом файле физически нельзя."""

    def test_pdf_rejected(self, client: TestClient) -> None:
        response = post(client, FAKE_PDF, "a.pdf", strategy="redact")

        assert response.status_code == 400
        assert "не поддерживается" in response.json()["detail"]

    def test_pdf_rejected_even_with_redact(self, client: TestClient) -> None:
        """Явная стратегия не должна пробивать запрет формата."""
        response = post(client, FAKE_PDF, "scan.pdf", strategy="redact")

        assert response.status_code == 400

    def test_pdf_absent_from_health(self, client: TestClient) -> None:
        assert ".pdf" not in client.get("/health").json()["supported_formats"]

    def test_double_extension_with_pdf_tail_rejected(self, client: TestClient) -> None:
        """Хвост расширения решает: report.pdf.exe — не PDF, а неподдерживаемый."""
        assert post(client, b"%PDF-1.4", "report.pdf.exe").status_code == 400

    def test_pdf_content_with_txt_name_accepted(self, client: TestClient) -> None:
        """Формат определяется именем, а не содержимым файла.

        Содержимое всё равно прогоняется через детекторы как текст.
        """
        response = post(client, b"%PDF-1.4 not really a pdf", "notes.txt")

        assert response.status_code == 200


class TestJsonReport:
    def test_report_shape(self, client: TestClient) -> None:
        response = post(client, make_txt(), "a.txt", response_format="json")
        report = response.json()

        assert report["original_format"] == ".txt"
        assert report["units"] == 1
        assert report["counts"]
        assert report["elapsed_ms"] >= 0
        assert report["filename"] == "a.anonymized.txt"

    def test_findings_carry_original_text(self, client: TestClient) -> None:
        report = post(client, make_txt(), "a.txt", response_format="json").json()

        found = {finding["text"] for finding in report["findings"]}
        assert PASSPORT in found
        assert EMAIL in found

    def test_locations_present(self, client: TestClient) -> None:
        report = post(client, make_xlsx(), "a.xlsx", response_format="json").json()

        assert all(finding["location"] for finding in report["findings"])

    def test_report_without_file_body(self, client: TestClient) -> None:
        """В json-режиме сам файл не отдаём: это отчёт, а не документ."""

        response = post(client, make_txt(), "a.txt", response_format="json")

        assert response.headers["content-type"].startswith("application/json")
        assert set(response.json()) >= {"filename", "findings", "counts"}

    def test_report_intentionally_lists_found_values(self, client: TestClient) -> None:
        """Отчёт показывает, что именно нашлось, — так его и читают.

        Значит ответ содержит ПДн: поэтому json-режим не для отправки третьим
        лицам, а file-режим — для выгрузки обезличенного документа.
        """
        report = post(client, make_txt(), "a.txt", response_format="json").json()

        assert PASSPORT in {finding["text"] for finding in report["findings"]}

    def test_formula_warning_surfaced(self, client: TestClient) -> None:
        report = post(client, make_xlsx(), "a.xlsx", response_format="json").json()

        assert any("Формулы" in warning for warning in report["warnings"])

    def test_headers_present_in_json_mode(self, client: TestClient) -> None:
        response = post(client, make_txt(), "a.txt", response_format="json")

        assert int(response.headers["X-Anon-Found"]) >= 3


class TestGuards:
    @pytest.mark.parametrize(
        "name", ["a.exe", "noextension", "a.doc", "a.jpeg", "a.rtf", "a.odt"]
    )
    def test_unsupported_format(self, client: TestClient, name: str) -> None:
        response = post(client, b"x", name)

        assert response.status_code == 400
        assert "не поддерживается" in response.json()["detail"]

    def test_empty_file(self, client: TestClient) -> None:
        assert post(client, b"", "a.txt").status_code == 400

    def test_entities_filter(self, client: TestClient) -> None:
        response = post(
            client, make_txt(), "a.txt", entities=["EMAIL_ADDRESS"], response_format="json"
        )

        assert response.json()["counts"] == {"EMAIL_ADDRESS": 1}

    def test_entities_filter_accepts_spaces_and_duplicates(
        self, client: TestClient
    ) -> None:
        messy = post(
            client,
            make_txt(),
            "a.txt",
            entities=["EMAIL_ADDRESS", " EMAIL_ADDRESS "],
            response_format="json",
        )

        assert messy.status_code == 200
        assert messy.json()["counts"] == {"EMAIL_ADDRESS": 1}

    def test_no_filter_by_default_covers_all_supported_entities(
        self, client: TestClient
    ) -> None:
        """Дефолт без entities не должен оставлять часть ПДн открытой.

        Такой дефолт однажды стоял в коде: фильтр из четырёх сущностей, из-за
        чего номер счёта и паспорт уходили клиенту в исходном виде.
        """
        doc = (
            f"Паспорт: {PASSPORT}\n"
            f"ИНН: {INN}\n"
            f"Счёт: {ACCOUNT}\n"
            f"Карта: {CARD}\n"
            f"СНИЛС: {SNILS}\n"
            f"Контакт: {PERSON}, {EMAIL}, {PHONE}\n"
        ).encode()
        response = post(client, doc, "счёт.txt")

        assert response.status_code == 200
        body = response.text
        for secret in (PASSPORT, INN, ACCOUNT, CARD, SNILS, EMAIL, PHONE):
            assert secret not in body, f"{secret} остался в ответе"

    def test_entities_filter_narrows_result(self, client: TestClient) -> None:
        """Явный фильтр по-прежнему сужает набор, а не расширяет."""
        response = post(
            client,
            make_txt(),
            "a.txt",
            entities=["EMAIL_ADDRESS"],
            response_format="json",
        )

        assert set(response.json()["counts"]) == {"EMAIL_ADDRESS"}

    def test_unknown_entity_is_422(self, client: TestClient) -> None:
        response = post(client, make_txt(), "a.txt", entities=["NOT_A_THING"])

        assert response.status_code == 422
        assert response.json()["detail"]["error"] == "unknown_entities"

    def test_filter_without_hits_warns(self, client: TestClient) -> None:
        response = post(
            client, make_txt(), "a.txt", entities=["CRYPTO"], response_format="json"
        )

        assert any("без изменений" in warning for warning in response.json()["warnings"])

    def test_strategies(self, client: TestClient) -> None:
        assert PASSPORT not in text_of(
            post(client, make_txt(), "a.txt", strategy="redact").content
        )
        assert "***" in text_of(post(client, make_txt(), "a.txt", strategy="mask").content)
        assert "[ПАСПОРТ:" in text_of(post(client, make_txt(), "a.txt", strategy="hash").content)

    def test_unknown_strategy_is_422(self, client: TestClient) -> None:
        assert post(client, make_txt(), "a.txt", strategy="nope").status_code == 422

    def test_size_limit(self, client: TestClient) -> None:
        big = ("Паспорт 45 08 123456\n" * 1_000_000).encode()

        assert post(client, big, "a.txt").status_code == 400


class TestOpenApi:
    def test_route_documented(self, client: TestClient) -> None:
        schema = client.get("/openapi.json").json()

        assert "/api/v1/anonymize/file" in schema["paths"]

    def test_image_routes_gone(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]

        assert "/api/v1/anonymize/image" not in paths
        assert "/api/v1/extract/text" not in paths

    def test_health_lists_formats(self, client: TestClient) -> None:
        body = client.get("/health").json()

        assert ".docx" in body["supported_formats"]
        assert ".xlsx" in body["supported_formats"]
        assert ".pptx" in body["supported_formats"]

    def test_route_description_mentions_no_pdf(self, client: TestClient) -> None:
        """Клиент читает Swagger, а не код: запрет должен быть описан там."""
        schema = client.get("/openapi.json").json()
        description = schema["paths"]["/api/v1/anonymize/file"]["post"]["description"]

        assert "PDF не поддерживается" in description
