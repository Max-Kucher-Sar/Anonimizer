from __future__ import annotations

import hmac
import logging
import re
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import Body, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.detectors.ollama_detector import OllamaError
from app.files.pipeline import (
    SUPPORTED_SUFFIXES,
    FilePipeline,
    FileTooLargeError,
    describe,
    suffix_of,
)
from app.files.schemas import FileFindingOut, FileReport
from app.pipeline import AnonymizerPipeline
from app.schemas import (
    AnonymizeRequest,
    AnonymizeResponse,
    FindingOut,
    HealthResponse,
    Strategy,
    parse_entity_filter,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

settings = get_settings()
pipeline: AnonymizerPipeline | None = None
file_pipeline: FilePipeline | None = None

# Заголовок с API-ключом. /health остаётся открытым для мониторинга и
# healthcheck'ов, всё остальное требует ключ.
API_KEY_HEADER = "X-API-Key"
PUBLIC_PATHS = {"/health"}


async def _require_api_key(request: Request, call_next) -> Response:
    """Проверяет X-API-Key на всех маршрутах, кроме публичных.

    Ключ не задан (локальная разработка) — пропускаем все запросы.
    Ключ задан — сверяем константным временем, чтобы не светить длину.
    Возвращаем JSONResponse напрямую: HTTPException из BaseHTTPMiddleware
    не долетает до обработчиков FastAPI.
    """
    if not settings.api_key:
        return await call_next(request)
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)
    provided = request.headers.get(API_KEY_HEADER, "")
    if not provided or not hmac.compare_digest(provided, settings.api_key):
        return JSONResponse(status_code=401, content={"detail": "Invalid or missing X-API-Key"})
    return await call_next(request)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global pipeline, file_pipeline

    # Прод без ключа — ошибка конфигурации: наружу торчит открытый API.
    if settings.env == "production" and not settings.api_key:
        raise RuntimeError(
            "ANON_ENV=production requires ANON_API_KEY to be set. "
            "Refusing to start with an open API."
        )

    logger.info("Warming up detectors (spacy=%s)...", settings.spacy_model)
    pipeline = AnonymizerPipeline(settings)
    file_pipeline = FilePipeline(pipeline, settings)

    logger.info(
        "Ready. spacy=%s entities=%s formats=%s",
        settings.spacy_model,
        len(pipeline.supported_entities),
        len(file_pipeline.supported_suffixes),
    )
    yield


app = FastAPI(
    title="Anonymizer API",
    version="0.2.0",
    description="Локальный сервис анонимизации персональных данных: текст и файлы.",
    lifespan=lifespan,
)

# Защита API-ключом: всё, кроме /health, требует X-API-Key.
app.middleware("http")(_require_api_key)


def get_pipeline() -> AnonymizerPipeline:
    if pipeline is None:
        raise HTTPException(status_code=503, detail="Pipeline is not ready yet")
    return pipeline


def get_file_pipeline() -> FilePipeline:
    if file_pipeline is None:
        raise HTTPException(status_code=503, detail="File pipeline is not ready yet")
    return file_pipeline


def resolve_entity_filter(requested: list[str] | None, supported: list[str]) -> list[str] | None:
    """Проверяет белый список типов.

    Молча пропускать неизвестные имена нельзя: опечатка в фильтре приводит к
    тому, что анонимайзер возвращает исходный текст без единого предупреждения.
    """
    if not requested:
        return None

    unknown = sorted({name for name in requested if name not in supported})
    if unknown:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "unknown_entities",
                "unknown": unknown,
                "supported_entities": supported,
            },
        )
    return requested




@app.exception_handler(ValueError)
async def value_error_handler(_, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(OllamaError)
async def ollama_error_handler(_, exc: OllamaError) -> JSONResponse:
    return JSONResponse(status_code=503, content={'detail': str(exc)})


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    active = get_pipeline()
    return HealthResponse(
        status="ok",
        spacy_model=settings.spacy_model,
        supported_entities=active.supported_entities,
        supported_formats=list(SUPPORTED_SUFFIXES),
    )


@app.get("/api/v1/entities")
def entities() -> dict[str, object]:
    active = get_pipeline()
    return active.supported_entities


@app.post("/api/v1/anonymize/text", response_model=AnonymizeResponse)
def anonymize_text(request: AnonymizeRequest, raw: Request) -> AnonymizeResponse:

    active = get_pipeline()
    entities = resolve_entity_filter(
        parse_entity_filter(request.entities), active.supported_entities
    )


    result = active.anonymize(
        text=request.text,
        strategy=request.strategy,
        entities=entities,
    )

    findings = [
        FindingOut(**finding.model_dump(), text=finding.extract(request.text))
        for finding in result.findings
    ]

    warnings: list[str] = []
    if entities and not result.findings:
        warnings.append(
            "Фильтр entities применён, но не нашлось ни одной сущности — "
            "текст вернулся без изменений."
        )
    return AnonymizeResponse(
        text=result.text,
        original_length=len(request.text),
        anonymized_length=len(result.text),
        findings=findings,
        counts=result.counts(),
        strategy=request.strategy,
        elapsed_ms=round(result.elapsed_ms, 2),
        warnings=warnings,
    )


def _content_disposition(filename: str) -> str:
    """Готовит заголовок вложения для имени с кириллицей.

    Значения HTTP-заголовков ограничены latin-1, поэтому «договор.txt» в
    filename просто не отправить. Стандартный обход: ASCII-заглушка для
    старых клиентов плюс filename* в кодировке UTF-8 для новых.
    """
    fallback = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "file"
    return (
        f"attachment; filename=\"{fallback}\"; "
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )


def _safe_header(value: object, limit: int | None = None) -> str:
    """Готовит значение заголовка.

    Значения HTTP-заголовков ограничены latin-1, а предупреждения и превью
    написаны по-русски, поэтому непечатные символы percent-энкодим.
    """
    text = quote(str(value), safe=" ,;:_.-+")
    return text[:limit] if limit else text


def _read_upload(file: UploadFile, limit: int) -> bytes:
    payload = file.file.read(limit + 1)
    if len(payload) > limit:
        raise FileTooLargeError(f"Файл больше {limit // (1024 * 1024)} МБ.")
    return payload


@app.post(
    "/api/v1/anonymize/file",
    summary="Анонимизировать файл",
    description=(
        "Принимает txt, md, markdown, log, csv, tsv, json, docx, xlsx, pptx и "
        "возвращает файл того же формата с удалёнными ПДн.\n\n"
        "Формат определяется по расширению имени файла. PDF не поддерживается: "
        "закрыть текст в готовом PDF физически, не разрушив вёрстку и шрифты, "
        "нельзя, а заглушки в content stream не пишутся. Сканы и PDF "
        "разбирайте через извлечение текста во внешнем инструменте.\n\n"
        "Структура документа сохраняется: в xlsx меняются значения ячеек, в "
        "docx и pptx — текст абзацев, формулы не трогаются."
    ),
    response_model=None,
    responses={
        200: {
            "description": "Обезличенный файл либо JSON-отчёт.",
            "model": FileReport,
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    },
)
def anonymize_file(
    file: UploadFile = File(..., description="Файл для анонимизации."),
    strategy: Strategy = Query(
        default=Strategy.PLACEHOLDER,
        description=(
            "placeholder — [ФИО]; mask — звёздочки; redact — удалить совсем; "
            "hash — псевдоним."
        ),
    ),
    response_format: str = Query(
        default="file",
        pattern="^(file|json)$",
        description="file — сам файл; json — отчёт без содержимого.",
    ),
    entities: list[str] = Body(
        default=[],
        description=(
            "Cписок типов сущностей; можно повторять параметр. Пусто — все поддерживаемые."
        ),
    ),
) -> Response:
    active = get_file_pipeline()
    entity_filter = resolve_entity_filter(
        parse_entity_filter(entities), get_pipeline().supported_entities
    )

    filename = file.filename or ""
    suffix = suffix_of(filename)
    if suffix not in SUPPORTED_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Формат «{suffix or 'без расширения'}» не поддерживается. "
                f"Доступны: {', '.join(SUPPORTED_SUFFIXES)}."
            ),
        )

    payload = _read_upload(file, settings.max_file_bytes)

    result = active.anonymize(
        payload=payload,
        filename=filename,
        strategy=strategy,
        entities=entity_filter,
    )

    out_name = describe(filename, suffix)
    entities_found = sorted({finding.entity for finding in result.findings})
    warnings = list(result.warnings)
    if entity_filter and not result.findings:
        warnings.append(
            "Фильтр entities применён, но не нашлось ни одной сущности — "
            "файл вернулся без изменений."
        )

    headers = {
        "X-Anon-Filename": _safe_header(out_name, 200),
        "X-Anon-Found": str(len(result.findings)),
        "X-Anon-Units": str(result.units),
        "X-Anon-Entities": _safe_header(",".join(entities_found), 400),
        "X-Anon-Warnings": _safe_header("; ".join(warnings), 900),
        "X-Anon-Elapsed-Ms": f"{result.elapsed_ms:.0f}",
        "Content-Disposition": _content_disposition(out_name),
    }

    if response_format == "json":
        report = FileReport(
            filename=out_name,
            original_format=suffix,
            media_type=result.media_type,
            size_in=len(payload),
            size_out=len(result.payload),
            units=result.units,
            findings=[
                FileFindingOut(
                    entity=finding.entity,
                    text=finding.text,
                    score=finding.score,
                    source=finding.source,
                    location=finding.location,
                )
                for finding in result.findings
            ],
            counts=result.counts(),
            strategy=strategy,
            elapsed_ms=round(result.elapsed_ms, 2),
            warnings=warnings,
        )
        return JSONResponse(content=report.model_dump(), headers=headers)

    return Response(content=result.payload, media_type=result.media_type, headers=headers)
