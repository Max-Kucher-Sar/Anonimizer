"""Офисные форматы: docx, xlsx, pptx.

Что сохраняется: вёрстка, формулы, объединённые ячейки, картинки. Меняется
только текст.

Что обходится: в .docx — абзацы, таблицы, колонтитулы; в .pptx — текстовые
рамки, таблицы на слайдах и заметки докладчика; в .xlsx — текстовые и числовые
ячейки всех листов.

Чего не обходится и почему:
  * формулы .xlsx лежат отдельным полем и в текст ячейки не входят — см.
    FORMULA_WARNING;
  * числа в .xlsx и .json разбираются, но заменить их текстом нельзя: тип
    изменится и схема данных сломается, поэтому они обнуляются.

Обход и запись идут одним и тем же генератором целей. Раньше сбор сегментов и
запись текста шли двумя почти одинаковыми функциями, и любая правка одной без
другой молча сдвигала замены на соседний абзац. Теперь рассинхрон невозможен
по построению.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from difflib import SequenceMatcher

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from openpyxl import load_workbook
from pptx import Presentation

from app.files.segments import Segment, anonymize_segments

SUFFIXES = (".docx", ".xlsx", ".pptx")

logger = logging.getLogger(__name__)


class CorruptFileError(ValueError):
    """Файл не открылся библиотекой формата."""


FORMULA_WARNING = (
    "Формулы в .xlsx не анализируются и не изменяются: они хранятся отдельно "
    "от текста ячеек. Если в формуле зашиты ПДн, они остались в файле."
)

NUMBERS_ZEROED_WARNING = (
    "Числовые ячейки с найденными ПДн обнулены: текстовая заглушка изменила бы "
    "тип и сломала бы форматирование и формулы, ссылающиеся на ячейку."
)


def _element_key(paragraph) -> object:
    """Стабильный ключ абзаца.

    Обращение к .paragraphs каждый раз создаёт новые прокси-объекты, поэтому id()
    самого абзаца не годится ни между обходами, ни для отлова дубликатов.
    Сам XML-элемент идентифицирует абзац и удерживается живым в seen.
    """
    # Keep the XML proxy alive in seen; otherwise lxml can recycle its id.
    return paragraph._p


def _replace_in_paragraph(paragraph, text: str) -> None:
    """Правит только текстовые XML-узлы, сохраняя рисунки, табы и поля.

    Изменения применяются справа налево по исходным смещениям. Текст в run
    с табуляцией или рисунком тоже заменяется, а не остаётся рядом с заглушкой.
    """
    original = paragraph.text
    if original == text:
        return
    if paragraph._p.tag != qn('w:p'):
        # PowerPoint paragraphs use DrawingML, not WordprocessingML.
        runs = list(paragraph.runs)
        if not runs:
            paragraph.add_run().text = text
        else:
            runs[0].text = text
            for run in runs[1:]:
                run.text = ''
        return
    nodes = []
    cursor = 0
    for run in paragraph._p.xpath('./w:r | ./w:hyperlink/w:r'):
        for node in run:
            tag = node.tag
            if tag == qn('w:t'):
                value = node.text or ''
            elif tag == qn('w:tab'):
                value = '\t'
            elif tag == qn('w:cr') or (
                tag == qn('w:br') and node.get(qn('w:type'), 'textWrapping') == 'textWrapping'
            ):
                value = '\n'
            else:
                continue
            nodes.append((node, cursor, cursor + len(value), value))
            cursor += len(value)
    if ''.join(value for _, _, _, value in nodes) != original:
        raise ValueError('Не удалось сопоставить текст DOCX с XML: запись отменена.')
    for operation, start, end, new_start, new_end in reversed(
        SequenceMatcher(None, original, text, autojunk=False).get_opcodes()
    ):
        if operation == 'equal':
            continue
        replacement = text[new_start:new_end]
        affected = [(node, low, high) for node, low, high, _ in nodes
                    if (low <= start < high if start == end else low < end and start < high)]
        if not affected:
            # Insertion at the right boundary of a preceding text node.
            anchor = next((node for node, low, high, _ in reversed(nodes)
                           if high <= start and node.tag == qn('w:t')), None)
            if anchor is None:
                paragraph.add_run(replacement)
            else:
                anchor.text = (anchor.text or '') + replacement
                anchor.set(qn('xml:space'), 'preserve')
            continue
        for index, (node, low, high) in enumerate(affected):
            value = node.text or '' if node.tag == qn('w:t') else (
                '\t' if node.tag == qn('w:tab') else '\n'
            )
            left = value[:max(0, start - low)]
            right = value[min(len(value), end - low):]
            changed = left + (replacement if index == 0 else '') + right
            if node.tag == qn('w:t'):
                node.text = changed
                node.set(qn('xml:space'), 'preserve')
            else:
                new_node = OxmlElement('w:t')
                new_node.text = changed
                new_node.set(qn('xml:space'), 'preserve')
                node.getparent().replace(node, new_node)
    if paragraph.text != text:
        raise ValueError('Проверка записи DOCX не прошла: файл не выдан.')


# --- docx ------------------------------------------------------------------


def _cell_paragraphs(cell, address: str) -> Iterator[tuple[object, str]]:
    for index, paragraph in enumerate(cell.paragraphs, start=1):
        yield paragraph, f"{address} абзац {index}"


def _iter_container(container, prefix: str, *, seen: set[object]) -> Iterator[tuple[object, str]]:
    """Обходит контейнер с текстом: абзацы, таблицы, их вложенные text box'ы.

    container — объект с атрибутами .paragraphs/.tables (Document, Header/Footer,
    _Cell) или XML-элемент w:txbxContent / w:sdtContent, который оборачивается
    в те же обёртки python-docx.

    Главное отличие от наивного обхода: text box'ы (w:txbxContent) внутри
    рисунков и sdt-блоки не видны через .paragraphs/.tables, поэтому их надо
    доставать рекурсивно по XML. Иначе таблицы в текстовых полях остаются и
    не маскируются, и могут быть затёрты при записи текста.
    """
    root = _xml_root(container)

    # 1. Верхнеуровневые абзацы и таблицы. Для объектов python-docx берём из
    #    .paragraphs/.tables; для XML-контейнера (txbxContent/sdtContent) прямых
    #    детей w:p/w:tbl оборачиваем в обёртки python-docx.
    if hasattr(container, "paragraphs"):
        paragraphs = list(container.paragraphs)
        tables = list(container.tables)
    else:
        paragraphs = [Paragraph(child, None) for child in root if _local(child) == "p"]
        tables = [Table(child, None) for child in root if _local(child) == "tbl"]

    for index, paragraph in enumerate(paragraphs, start=1):
        key = _element_key(paragraph)
        if key in seen:
            continue
        seen.add(key)
        yield paragraph, f"{prefix}абзац {index}"

    for table_index, table in enumerate(tables, start=1):
        yield from _iter_table(table, f"{prefix}таблица {table_index} ", seen=seen)

    # 2. Text box'ы и sdt-блоки, спрятанные в рисунках/полях. Их нет ни в
    #    .paragraphs, ни в .tables — только в XML. Обходим только те, что
    #    вложены в рисунок/поле верхнего уровня (не в другой txbxContent),
    #    иначе рекурсия зацикливается на вложенных копиях.
    for node in _iter_nested_blocks(root):
        tag = _local(node)
        if tag == "txbxContent":
            yield from _iter_container(node, f"{prefix}текстовое поле ", seen=seen)
        elif tag == "sdtContent":
            yield from _iter_container(node, f"{prefix}поле формы ", seen=seen)


def _xml_root(container):
    """XML-элемент контейнера: python-docx прячет его по-разному.

    Document/Header/Footer/_Cell не имеют общего API: у Document это .element,
    у _Cell — ._tc, у _Header/_Footer — свойство _element (возвращает корневой
    w:hdr/w:ftr часть), у XML-элемента (txbxContent/sdtContent) — он сам.
    _tc проверяем раньше _element: у _Cell есть оба атрибута, но настоящий
    корень ячейки именно _tc.
    """
    if hasattr(container, "element"):
        return container.element
    if hasattr(container, "_tc"):
        return container._tc
    if hasattr(container, "_element"):
        return container._element
    return container


def _local(node) -> str:
    """Локальное имя XML-тега без namespace."""
    return node.tag.split("}")[-1]


def _iter_nested_blocks(root) -> Iterator[object]:
    """w:txbxContent / w:sdtContent, вложенные в рисунки root верхнего уровня.

    Не отдаёт сам root (если это txbxContent/sdtContent) и блоки, вложенные в
    другой блок того же типа: они обрабатываются рекурсивно через
    _iter_container своего контейнера. Иначе на документе с text box'ом внутри
    text box'а (VML-fallback дублирует DrawingML-таблицу) рекурсия уходит в
    бесконечность.
    """
    for node in root.iter():
        tag = _local(node)
        if tag not in ("txbxContent", "sdtContent"):
            continue
        if node is root:
            continue
        # Пропускаем блоки, лежащие внутри другого блока того же типа.
        ancestor = node.getparent()
        nested = False
        while ancestor is not None and ancestor is not root:
            atag = _local(ancestor)
            if atag in ("txbxContent", "sdtContent"):
                nested = True
                break
            ancestor = ancestor.getparent()
        if not nested:
            yield node


def _iter_table(table, prefix: str, *, seen: set[object]) -> Iterator[tuple[object, str]]:
    """Абзацы всех ячеек таблицы + text box'ы внутри ячеек."""
    for row_index, row in enumerate(table.rows, start=1):
        for cell_index, cell in enumerate(row.cells, start=1):
            address = f"{prefix}строка {row_index} ячейка {cell_index} "
            yield from _iter_container(cell, address, seen=seen)


def _docx_parts(document: Document) -> Iterator[tuple[object, str]]:
    """Все места с текстом и человекочитаемым адресом каждого.

    Колонтитулы пропускать нельзя: в шапке договора регулярно стоит ФИО и дата,
    и это самый заметный ПДн в документе после подписи. Text box'ы (в т.ч. с
    таблицами) тоже: они не видны через document.tables, но содержат ПДн.
    """
    seen: set[object] = set()
    yield from _iter_container(document, "", seen=seen)

    for section_index, section in enumerate(document.sections, start=1):
        parts = (
            ("верхний колонтитул", section.header),
            ("нижний колонтитул", section.footer),
            ("верхний колонтитул (чётная)", section.even_page_header),
            ("нижний колонтитул (чётная)", section.even_page_footer),
            ("верхний колонтитул (первая)", section.first_page_header),
            ("нижний колонтитул (первая)", section.first_page_footer),
        )
        for title, part in parts:
            if part.is_linked_to_previous:
                continue
            yield from _iter_container(part, f"секция {section_index} {title} ", seen=seen)


def _docx_targets(document: Document) -> Iterator[tuple[object, str]]:
    """Оставляет только непустые абзацы, без повторов.

    Секции часто делят один колонтитул: без проверки один и тот же абзац попал
    бы в обход дважды и получил бы две несовместимые замены. seen передаётся
    насквозь, поэтому абзац из text box'а, попавший и в .paragraphs, и в XML,
    обходится один раз.
    """
    seen: set[int] = set()
    for paragraph, address in _docx_parts(document):
        if not paragraph.text.strip():
            continue
        key = _element_key(paragraph)
        if key in seen:
            continue
        seen.add(key)
        yield paragraph, address


def _docx_apply(document: Document, targets, replacements: list[str | None]) -> bytes:
    for (paragraph, _), text in zip(targets, replacements):
        if text is not None:
            _replace_in_paragraph(paragraph, text)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# --- xlsx ------------------------------------------------------------------


def _xlsx_is_formula(value: object) -> bool:
    return isinstance(value, str) and value.startswith("=")


def _xlsx_targets(workbook) -> Iterator[tuple[object, str, bool]]:
    """Отдаёт (ячейка, адрес, это_ли_формула).

    Числовые ячейки тоже разбираются: ИНН в выгрузках часто приходит числом,
    и раньше такое значение оставалось в файле как есть.
    """
    for sheet in workbook.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                value = cell.value
                if value is None or isinstance(value, bool):
                    continue
                if _xlsx_is_formula(value):
                    yield cell, f"«{sheet.title}»!{cell.coordinate}", True
                elif isinstance(value, (str, int, float)):
                    yield cell, f"«{sheet.title}»!{cell.coordinate}", False


def _xlsx_apply(workbook, replacements: dict[str, str], zeroed: set[str]) -> bytes:
    for cell, address, is_formula in _xlsx_targets(workbook):
        if is_formula:
            continue
        if address in zeroed:
            cell.value = 0
        elif isinstance(cell.value, str) and address in replacements:
            cell.value = replacements[address]
        # Число без находок не трогаем: подстановка его строкового вида
        # превратила бы 1500.5 в "1500.5" и сломала бы форматирование ячейки.

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# --- pptx ------------------------------------------------------------------


def _shapes(shapes) -> Iterator[object]:
    """Разворачивает группы фигур: внутри группы текст тоже есть."""
    for shape in shapes:
        if hasattr(shape, "shapes"):
            yield from _shapes(shape.shapes)
        else:
            yield shape


def _pptx_targets(presentation: Presentation) -> Iterator[tuple[object, str]]:
    """Отдаёт (абзац, адрес) для текста слайдов и заметок докладчика.

    Заметки не читаются глазами при беглом просмотре, но там регулярно лежат
    ФИО автора и внутренние телефоны, поэтому пропустить их нельзя.
    """
    for slide_index, slide in enumerate(presentation.slides, start=1):
        for shape_index, shape in enumerate(_shapes(slide.shapes), start=1):
            if getattr(shape, "has_text_frame", False):
                for paragraph_index, paragraph in enumerate(
                    shape.text_frame.paragraphs, start=1
                ):
                    if paragraph.text.strip():
                        yield paragraph, (
                            f"слайд {slide_index} фигура {shape_index} "
                            f"абзац {paragraph_index}"
                        )
            if getattr(shape, "has_table", False):
                for row_index, row in enumerate(shape.table.rows, start=1):
                    for cell_index, cell in enumerate(row.cells, start=1):
                        yield from _cell_paragraphs(
                            cell,
                            f"слайд {slide_index} таблица строка {row_index} "
                            f"ячейка {cell_index}",
                        )

        # has_notes_slide проверяется первым: обращение к notes_slide создаёт
        # пустую заметку и раздувает файл, если её не было.
        if slide.has_notes_slide:
            for paragraph in slide.notes_slide.notes_text_frame.paragraphs:
                if paragraph.text.strip():
                    yield paragraph, f"заметки слайда {slide_index}"


def _pptx_apply(presentation: Presentation, targets, replacements: list[str | None]) -> bytes:
    for (paragraph, _), text in zip(targets, replacements):
        if text is not None:
            _replace_in_paragraph(paragraph, text)

    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


# --- публичное -------------------------------------------------------------


def anonymize_docx(payload: bytes, pipeline, **kwargs) -> tuple[bytes, list[str], object]:
    try:
        document = Document(io.BytesIO(payload))
    except Exception as exc:
        raise CorruptFileError(f"Не удалось открыть .docx: {exc}") from exc

    targets = list(_docx_targets(document))
    segments = [Segment(text=paragraph.text, location=where) for paragraph, where in targets]
    result = anonymize_segments(pipeline, segments, **kwargs)

    return (
        _docx_apply(document, targets, [segment.anonymized for segment in segments]),
        list(result.warnings),
        result,
    )


def anonymize_xlsx(payload: bytes, pipeline, **kwargs) -> tuple[bytes, list[str], object]:
    try:
        # data_only=False: нужны исходные значения ячеек, а не результат формул.
        workbook = load_workbook(io.BytesIO(payload), data_only=False)
    except Exception as exc:
        raise CorruptFileError(f"Не удалось открыть .xlsx: {exc}") from exc

    targets = [
        (cell, address)
        for cell, address, is_formula in _xlsx_targets(workbook)
        if not is_formula
    ]
    segments = [
        Segment(text=str(cell.value), location=address) for cell, address in targets
    ]
    result = anonymize_segments(pipeline, segments, **kwargs)

    has_formulas = any(
        is_formula for _, _, is_formula in _xlsx_targets(workbook)
    )
    zeroed = {
        address
        for (cell, address), segment in zip(targets, segments)
        if isinstance(cell.value, (int, float))
        and not isinstance(cell.value, bool)
        and segment.anonymized != segment.text
    }

    warnings = list(result.warnings)
    if has_formulas:
        warnings.append(FORMULA_WARNING)
    if zeroed:
        warnings.append(NUMBERS_ZEROED_WARNING)

    replacements = {address: segment.anonymized for (cell, address), segment in zip(targets, segments)}
    return _xlsx_apply(workbook, replacements, zeroed), warnings, result


def anonymize_pptx(payload: bytes, pipeline, **kwargs) -> tuple[bytes, list[str], object]:
    try:
        presentation = Presentation(io.BytesIO(payload))
    except Exception as exc:
        raise CorruptFileError(f"Не удалось открыть .pptx: {exc}") from exc

    targets = list(_pptx_targets(presentation))
    segments = [Segment(text=paragraph.text, location=where) for paragraph, where in targets]
    result = anonymize_segments(pipeline, segments, **kwargs)

    return (
        _pptx_apply(presentation, targets, [segment.anonymized for segment in segments]),
        list(result.warnings),
        result,
    )
