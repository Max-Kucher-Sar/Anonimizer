"""Файлы для тестов: генерируются кодом, сеть не нужна.

Все фикстуры содержат одинаковый набор ПДн, чтобы по одному файлу можно было
проверить и наличие находок, и адреса, и что именно исчезло из результата.
"""

from __future__ import annotations

import io

from docx import Document
from openpyxl import Workbook
from pptx import Presentation

PASSPORT = "45 08 123456"
INN = "7707083893"
EMAIL = "ivan.ivanov@example.com"
PHONE = "+7 912 345-67-89"
PERSON = "Иванов Иван Иванович"
ACCOUNT = "40817810099910004312"
CARD = "4276380012345679"
SNILS = "112-233-445 79"

SAMPLE_TXT = (
    f"Договор поставки № 14\n"
    f"Паспорт: {PASSPORT}\n"
    f"ИНН: {INN}\n"
    f"Счёт: {ACCOUNT}\n"
    f"Карта: {CARD}\n"
    f"СНИЛС: {SNILS}\n"
    f"Контакт: {PERSON}, {EMAIL}, {PHONE}\n"
    "Без персональных данных."
)

SAMPLE_CSV = (
    "id,name,phone,note\n"
    f"1,{PERSON},{PHONE},подписан\n"
    f"2,Петров Сергей Иванович,+7 916 111-22-33,ждёт\n"
)

SAMPLE_JSON = (
    '{"client": {"name": "' + PERSON + '", "inn": ' + INN + ', "active": true},'
    ' "contacts": [{"email": "' + EMAIL + '"}], "passport": "' + PASSPORT + '"}'
)


def make_txt() -> bytes:
    return SAMPLE_TXT.encode("utf-8")


def make_txt_cp1251() -> bytes:
    """Тот же файл в windows-1251: проверяем автоопределение кодировки."""
    return SAMPLE_TXT.encode("cp1251")


def make_csv() -> bytes:
    return SAMPLE_CSV.encode("utf-8")


def make_json() -> bytes:
    return SAMPLE_JSON.encode("utf-8")


def make_broken_json() -> bytes:
    return b'{"name": "Ivanov",,}'


def make_docx() -> bytes:
    document = Document()
    document.add_heading("Договор поставки", level=1)
    document.add_paragraph("Исполнитель: Петров Сергей Иванович")
    document.add_paragraph(f"Паспорт {PASSPORT}, ИНН {INN}")

    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Получатель"
    table.rows[0].cells[1].text = PERSON

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def make_docx_split_name() -> bytes:
    """ФИО, разнесённое по соседним абзацам.

    По одному абзацу «Иванов» сущностью не считается: spaCy видит фамилию
    без имени и отчества. Обход через склейку кусков это чинит.
    """
    document = Document()
    document.add_paragraph("Иванов")
    document.add_paragraph("Иван")
    document.add_paragraph("Иванович")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def make_xlsx() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Клиенты"
    sheet["A1"] = "ФИО"
    sheet["B1"] = "Телефон"
    sheet["A2"] = PERSON
    sheet["B2"] = PHONE
    sheet["A3"] = INN
    sheet["B3"] = "=B2"

    other = workbook.create_sheet("Архив")
    other["A1"] = EMAIL

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def make_docx_full() -> bytes:
    """Документ с ПДн не только в теле, но и в колонтитулах.

    Колонтитул — самое частое место утечки в договорах: ФИО в шапке стоит
    чаще, чем в самом тексте.
    """
    document = Document()
    document.add_paragraph("Договор поставки № 14")
    document.add_paragraph(f"Подписант: {PERSON}")

    header = document.sections[0].header
    header.paragraphs[0].text = f"Договор с {PERSON}"
    footer = document.sections[0].footer
    footer.paragraphs[0].text = f"Паспорт {PASSPORT}"

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def make_xlsx_numbers() -> bytes:
    """Числовые ПДн: ИНН, записанный числом, а не строкой."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Клиенты"
    sheet["A1"] = "ИНН"
    sheet["A2"] = int(INN)
    sheet["B1"] = "Сумма"
    sheet["B2"] = 1500.5

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def make_pptx() -> bytes:
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.title.text = "Отчёт по клиентам"

    box = slide.shapes.add_textbox(0, 0, 100, 100)
    frame = box.text_frame
    frame.text = f"Ответственный: {PERSON}"
    frame.add_paragraph().text = f"Телефон {PHONE}"

    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def make_pptx_notes() -> bytes:
    """Заметки докладчика: их не видно на слайде, но ПДн там бывают."""
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.title.text = "Отчёт по клиентам"
    slide.notes_slide.notes_text_frame.text = f"Докладчик {PERSON}, {EMAIL}"

    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()

