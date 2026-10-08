# syntax=docker/dockerfile:1

# ---------- Stage 1: зависимости (uv) ----------
# Образ uv уже содержит Python 3.12. Весь тяжёлый инсталл живёт здесь,
# а слой кешируется по содержимому requirements.txt: правки кода
# (app/tests) не инвалидируют этот слой.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    VIRTUAL_ENV=/opt/venv

WORKDIR /srv

# Сначала только requirements.txt: изменения кода не трогают кеш зависимостей.
# Кеш-маунт сохраняет скачанные пакеты между сборками.
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /opt/venv \
    && uv pip install --python /opt/venv/bin/python -r requirements.txt \
    && /opt/venv/bin/python -m spacy download ru_core_news_sm

# ---------- Test dependencies (built only for target test) ----------
FROM builder AS test-deps
COPY requirements-dev.txt .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/venv/bin/python -r requirements-dev.txt

# ---------- Stage 2: runtime ----------
# Тонкий образ без uv, pip и исходников сборки: только venv с зависимостями.
FROM python:3.12-slim-bookworm AS app-base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /srv

# curl нужен только для healthcheck в docker-compose.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

COPY pyproject.toml .
COPY app ./app
COPY tests ./tests

RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /srv
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# pytest/httpx are included only in the separate test image.
FROM app-base AS test
USER root
COPY --from=test-deps /opt/venv /opt/venv
USER appuser
CMD ["pytest", "-q"]

# Last stage is the default image when building without --target.
FROM app-base AS runtime
