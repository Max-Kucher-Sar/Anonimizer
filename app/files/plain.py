"""Текстовые файлы: txt, md, log, csv, tsv, json."""

from __future__ import annotations

import json
from typing import Any

from app.files.segments import Segment, anonymize_segments

PLAIN_SUFFIXES = (".txt", ".md", ".markdown", ".log", ".csv", ".tsv")
JSON_SUFFIXES = (".json",)
SUFFIXES = PLAIN_SUFFIXES + JSON_SUFFIXES

ENCODINGS = ("utf-8-sig", "utf-8", "cp1251", "koi8-r")


class FileDecodeError(ValueError):
    """Файл не декодируется ни в одной поддерживаемой кодировке."""


NON_UTF8_WARNING = (
    "Файл прочитан не в UTF-8. Если в нём были кириллические ПДн, часть "
    "находок могла не распознаться — проверьте результат."
)


def decode_bytes(payload: bytes) -> tuple[str, str]:
    """Возвращает (текст, кодировка).

    utf-8-sig идёт первым, чтобы BOM снялся: иначе первая строка получила бы
    невидимый символ и не совпала с остальными при обратной сборке.
    """
    for encoding in ENCODINGS:
        try:
            return payload.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue
    raise FileDecodeError(f"Файл не читается ни в одной кодировке: {', '.join(ENCODINGS)}.")


def _is_utf8(encoding: str) -> bool:
    return encoding in ("utf-8", "utf-8-sig")


NUMBERS_ZEROED_WARNING = (
    "Числовые значения с найденными ПДн обнулены: текстовая заглушка изменила "
    "бы тип и сломала схему данных. Адреса — в findings."
)


def iter_json_strings(node: Any, path: str = "") -> list[Segment]:
    """Собирает строковые значения JSON с их путями."""
    segments: list[Segment] = []

    if isinstance(node, dict):
        for key, value in node.items():
            segments.extend(iter_json_strings(value, f"{path}.{key}" if path else str(key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            segments.extend(iter_json_strings(value, f"{path}[{index}]"))
    elif isinstance(node, str) and node.strip():
        segments.append(Segment(text=node, location=path or "$"))

    return segments


def iter_json_numbers(node: Any, path: str = "") -> list[Segment]:
    """Собирает числа — они тоже могут быть персональными данными.

    ИНН в JSON часто пишут числом, и раньше он просто не разбирался: значение
    оставалось в файле как есть. Проверяется отдельно от строк, чтобы число не
    попадало в общий контекст и не склеивалось с соседними значениями.
    """
    segments: list[Segment] = []

    if isinstance(node, dict):
        for key, value in node.items():
            segments.extend(iter_json_numbers(value, f"{path}.{key}" if path else str(key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            segments.extend(iter_json_numbers(value, f"{path}[{index}]"))
    elif isinstance(node, (int, float)) and not isinstance(node, bool):
        segments.append(Segment(text=str(node), location=path or "$"))

    return segments


def apply_json(root: Any, strings: list[Segment], numbers: list[Segment]) -> bytes:
    """Подставляет анонимизированные строки, обнуляя числа с находками."""
    lookup = {segment.location: segment.anonymized for segment in strings}
    zeroed = {
        segment.location
        for segment in numbers
        if segment.anonymized is not None and segment.anonymized != segment.text
    }

    def walk(node: Any, path: str = "") -> Any:
        where = path or "$"
        if isinstance(node, dict):
            return {
                key: walk(value, f"{path}.{key}" if path else str(key))
                for key, value in node.items()
            }
        if isinstance(node, list):
            return [walk(value, f"{path}[{index}]") for index, value in enumerate(node)]
        if isinstance(node, str):
            return lookup.get(where, node)
        if isinstance(node, bool) or not isinstance(node, (int, float)):
            return node
        if where in zeroed:
            return 0 if isinstance(node, int) else 0.0
        return node

    return json.dumps(walk(root), ensure_ascii=False, indent=2).encode("utf-8")


def anonymize_text(payload: bytes, pipeline, **kwargs) -> tuple[bytes, list[str], object]:
    """Плоский текст целиком: один кусок, один проход."""
    text, encoding = decode_bytes(payload)
    warnings: list[str] = [] if _is_utf8(encoding) else [NON_UTF8_WARNING]

    result = anonymize_segments(pipeline, [Segment(text=text, location="")], **kwargs)
    warnings.extend(result.warnings)
    return result.texts[0].encode("utf-8"), warnings, result


def anonymize_json(payload: bytes, pipeline, **kwargs) -> tuple[bytes, list[str], object]:
    """JSON: анонимизируются и строки, и числа, структура и типы сохраняются."""
    text, encoding = decode_bytes(payload)
    warnings: list[str] = [] if _is_utf8(encoding) else [NON_UTF8_WARNING]

    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Файл не является корректным JSON: {exc}.") from exc

    strings = iter_json_strings(document)
    numbers = iter_json_numbers(document)

    result = anonymize_segments(pipeline, strings, **kwargs)
    warnings.extend(result.warnings)

    numeric = anonymize_segments(pipeline, numbers, **kwargs) if numbers else None
    if numeric is not None:
        result.findings.extend(numeric.findings)
        result.elapsed_ms += numeric.elapsed_ms
        result.units += numeric.units
        warnings.extend(numeric.warnings)
        if any(segment.anonymized != segment.text for segment in numbers):
            warnings.append(NUMBERS_ZEROED_WARNING)

    return apply_json(document, strings, numbers), warnings, result
