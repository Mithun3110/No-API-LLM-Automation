"""Turning text read from the page into typed values. Shared by discovery and replay."""

import re
from decimal import Decimal, InvalidOperation


class ParseError(ValueError):
    """The page text is not the kind of value the recipe expects."""


def parse_value(text: str, kind: str) -> str | Decimal:
    """'$16,057.78' -> Decimal('16057.78') for currency; numbers likewise; text is trimmed."""
    text = text.strip()
    if kind == "text":
        return text
    cleaned = re.sub(r"[,\s]", "", text)
    if kind == "currency":
        cleaned = cleaned.replace("$", "")  # "-$12.00" -> "-12.00"
        if not re.fullmatch(r"-?\d+(\.\d{1,2})?", cleaned):
            raise ParseError(f"'{text}' is not a currency amount")
    try:
        return Decimal(cleaned)
    except InvalidOperation as e:
        raise ParseError(f"'{text}' is not a number") from e
