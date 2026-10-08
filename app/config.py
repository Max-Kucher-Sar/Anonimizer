from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ANON_", extra="ignore")

    spacy_model: str = "ru_core_news_sm"
    ollama_enabled: bool = False
    ollama_url: str = 'http://127.0.0.1:11434'
    ollama_allow_container_host: bool = False
    ollama_model: str = 'qwen2.5:7b'
    ollama_timeout_seconds: int = Field(default=180, ge=1, le=1800)
    ollama_chunk_chars: int = Field(default=2400, ge=800, le=4000)

    # Внешний доступ: API-ключ в заголовке X-API-Key. Пусто — ключ не требуется
    # (локальная разработка); задаётся через ANON_API_KEY в docker-compose/.env.
    api_key: str = ""

    # Окружение. production с пустым ключом — ошибка конфигурации: сервис
    # откажется стартовать, чтобы не открыть данные в интернет.
    env: str = "development"

    # Файловый анонимайзер.
    max_file_bytes: int = 20 * 1024 * 1024
    max_office_segments: int = 20_000
    max_text_chars: int = 2_000_000

    api_workers: int = 2


@lru_cache
def get_settings() -> Settings:
    return Settings()
