"""Алгоритмы контрольных сумм для российских идентификаторов.

Используются, чтобы отсеять ложные срабатывания regex: номер, который не
проходит проверку контрольной суммы, физически не может быть настоящим.
"""

from __future__ import annotations


def is_inn_10(digits: str) -> bool:
    """ИНН юридического лица (10 цифр)."""
    if len(digits) != 10 or not digits.isdigit():
        return False
    coefficients = (2, 4, 10, 3, 5, 9, 4, 6, 8)
    total = sum(int(digits[i]) * coefficients[i] for i in range(9))
    return total % 11 % 10 == int(digits[9])


def is_inn_12(digits: str) -> bool:
    """ИНН физического лица (12 цифр)."""
    if len(digits) != 12 or not digits.isdigit():
        return False
    coefficients_10 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
    coefficients_11 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)

    total = sum(int(digits[i]) * coefficients_10[i] for i in range(10))
    if total % 11 % 10 != int(digits[10]):
        return False

    total = sum(int(digits[i]) * coefficients_11[i] for i in range(11))
    return total % 11 % 10 == int(digits[11])


def is_inn(digits: str) -> bool:
    return is_inn_10(digits) or is_inn_12(digits)


def is_snils(digits: str) -> bool:
    """СНИЛС: 9 значащих цифр + 2 контрольные."""
    if len(digits) != 11 or not digits.isdigit():
        return False

    number = int(digits[:9])
    if number > 999999999 or number < 1000:
        return False

    if number <= 1001998:
        expected = f"{number % 100:02d}"
    else:
        checksum = (sum(int(d) for d in digits[:8]) * 9) % 101
        if checksum == 100:
            checksum = 101
        expected = f"{checksum:02d}"

    return digits[9:11] == expected


def is_russian_passport(value: str) -> bool:
    """Серия (4 цифры) + номер (6 цифр) паспорта РФ.

    Контрольной суммы у паспорта нет (она в реестре Госполиции), поэтому
    проверяем только правила выдачи: серия начинается с 1..9, номер не может
    начинаться с 0, цифры не повторяются подряд.
    """
    raw = "".join(char for char in value if char.isdigit())
    if len(raw) != 10:
        return False

    series, number = raw[:4], raw[4:]
    if series[0] == "0" or number[0] == "0":
        return False
    if len(set(raw)) <= 2:
        return False
    return True


def is_credit_card(value: str) -> bool:
    """Проверка по алгоритму Луна (пробелы и дефисы допускаются)."""
    digits = "".join(char for char in value if char.isdigit())
    if not digits.isdigit() or not 13 <= len(digits) <= 19:
        return False
    total = 0
    for index, char in enumerate(reversed(digits)):
        digit = int(char)
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0