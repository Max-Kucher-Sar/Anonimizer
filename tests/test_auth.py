"""Контур аутентификации: X-API-Key на всех ручках, /health открыт.

Middleware читает settings.api_key динамически, поэтому тесты переключают его
через monkeypatch на модульном объекте app.main.settings.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app, settings


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def api_key(monkeypatch):
    key = "test-secret-key"
    monkeypatch.setattr(settings, "api_key", key)
    return key


SAMPLE = {"text": "Иван Иванов, +7 912 345-67-89"}


class TestApiKeyRequired:
    def test_without_key_is_401(self, client: TestClient, api_key: str) -> None:
        response = client.post("/api/v1/anonymize/text", json=SAMPLE)

        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid or missing X-API-Key"

    def test_wrong_key_is_401(self, client: TestClient, api_key: str) -> None:
        response = client.post(
            "/api/v1/anonymize/text", json=SAMPLE, headers={"X-API-Key": "wrong"}
        )

        assert response.status_code == 401

    def test_valid_key_is_200(self, client: TestClient, api_key: str) -> None:
        response = client.post(
            "/api/v1/anonymize/text", json=SAMPLE, headers={"X-API-Key": api_key}
        )

        assert response.status_code == 200
        assert "Иван" not in response.json()["text"]

    def test_file_route_requires_key(self, client: TestClient, api_key: str) -> None:
        response = client.post(
            "/api/v1/anonymize/file",
            files={"file": ("a.txt", "Иван Иванов".encode(), "text/plain")},
        )

        assert response.status_code == 401

    def test_file_route_with_key_is_200(self, client: TestClient, api_key: str) -> None:
        response = client.post(
            "/api/v1/anonymize/file",
            files={"file": ("a.txt", "Иван Иванов".encode(), "text/plain")},
            headers={"X-API-Key": api_key},
        )

        assert response.status_code == 200


class TestPublicPaths:
    def test_health_is_open_without_key(self, client: TestClient, api_key: str) -> None:
        response = client.get("/health")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_entities_requires_key(self, client: TestClient, api_key: str) -> None:
        response = client.get("/api/v1/entities")

        assert response.status_code == 401

    def test_no_key_configured_passes_everything(self, client: TestClient) -> None:
        """Без настроенного ключа (dev) сервис работает как раньше."""
        response = client.post("/api/v1/anonymize/text", json=SAMPLE)

        assert response.status_code == 200