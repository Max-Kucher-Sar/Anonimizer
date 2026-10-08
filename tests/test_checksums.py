from app.checksums import (
    is_credit_card,
    is_inn,
    is_inn_10,
    is_inn_12,
    is_russian_passport,
    is_snils,
)


class TestInn:
    def test_known_valid_inn_10(self) -> None:
        assert is_inn_10("7707083893")
        assert is_inn("7707083893")

    def test_invalid_inn_10(self) -> None:
        assert not is_inn_10("7707083894")
        assert not is_inn_10("abcdefghij")

    def test_inn_12_roundtrip(self) -> None:
        base = "5001007322"
        coefficients_10 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
        total = sum(int(base[i]) * coefficients_10[i] for i in range(10))
        tenth = str(total % 11 % 10)

        partial = base + tenth
        coefficients_11 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
        total = sum(int(partial[i]) * coefficients_11[i] for i in range(11))
        eleventh = str(total % 11 % 10)

        assert is_inn_12(partial + eleventh)
        assert not is_inn_12(partial + str((int(eleventh) + 1) % 10))

    def test_length_guard(self) -> None:
        assert not is_inn_12("1234567890123")


class TestSnils:
    def test_known_valid_snils(self) -> None:
        assert is_snils("12345678121")

    def test_invalid_checksum(self) -> None:
        assert not is_snils("12345678122")

    def test_rejects_short_and_long(self) -> None:
        assert not is_snils("1234567812")
        assert not is_snils("123456781211")

    def test_rejects_low_numbers(self) -> None:
        assert not is_snils("00000000000")


class TestPassport:
    def test_valid_format(self) -> None:
        assert is_russian_passport("45 08 123456")
        assert is_russian_passport("4508123456")

    def test_zero_prefix_rejected(self) -> None:
        assert not is_russian_passport("05 08 123456")
        assert not is_russian_passport("45 08 023456")

    def test_uniform_digits_rejected(self) -> None:
        assert not is_russian_passport("11 11 111111")


class TestCreditCard:
    def test_luhn_valid(self) -> None:
        assert is_credit_card("4539148803436467")
        assert is_credit_card("4539 1488 0343 6467")

    def test_luhn_invalid(self) -> None:
        assert not is_credit_card("4539148803436468")

    def test_length_guard(self) -> None:
        assert not is_credit_card("4539148803")