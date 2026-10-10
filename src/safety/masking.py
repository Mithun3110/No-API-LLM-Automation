"""One masking function used by ALL logging, so no log line can skip it.

Three layers, because no single one is enough:
1. Field names: any key matching policy.mask_fields (member_id, savings_balance, new_phone...).
2. Known values: the actual secrets and sensitive inputs/outputs of this run, wherever they
   appear in free text (an LLM's reason, an error message).
3. Patterns: phone numbers, money, account numbers, member IDs, even if we never saw them.
"""

import re
from typing import Any

MASK = "***"

# Order matters: longest numbers first, so an account number is not half-masked as a member ID.
PATTERNS = [
    (re.compile(r"\b\d{8,}\b"), MASK),                                # account numbers
    (re.compile(r"\b\d{3}-\d{3}-\d{4}\b"), MASK),                     # phone numbers
    (re.compile(r"-?\$\s?\d[\d,]*(?:\.\d{2})?"), "$" + MASK),          # money
    (re.compile(r"(?<![\d.])\d[\d,]*\.\d{2}(?![\d.])"), MASK),          # bare amounts: 50.00, 16057.78
    (re.compile(r"\b\d{5}\b"), lambda m: mask_member_id(m.group())),  # member IDs (and zip codes)
]


def mask_member_id(value: str) -> str:
    """Keep the last 2 digits: enough to tell runs apart when debugging, not enough to identify."""
    return MASK + value[-2:] if len(value) > 2 else MASK


class Masker:
    def __init__(self, mask_fields: list[str], secrets: list[str] | None = None):
        self.mask_fields = mask_fields
        self._known: dict[str, str] = {}  # sensitive value -> its masked form
        for s in secrets or []:
            self.add_value(s)

    def add_value(self, value: Any, field: str | None = None) -> None:
        """Register a sensitive value of this run (an input, an output, a password)."""
        text = str(value).strip()
        if len(text) >= 3:  # very short values would mask unrelated text everywhere
            self._known[text] = self.mask_value(field, text) if field else MASK

    def is_sensitive_field(self, key: str) -> bool:
        # "savings_balance" and "new_phone" match; "recipe_id" does not.
        key = key.lower()
        return any(key == f or key.endswith("_" + f) or key.startswith(f + "_") for f in self.mask_fields)

    def mask_value(self, field: str | None, value: Any) -> str:
        if field and (field == "member_id" or field.endswith("_member_id")):
            return mask_member_id(str(value))
        return MASK

    def mask_text(self, text: str) -> str:
        for value in sorted(self._known, key=len, reverse=True):  # longest first
            text = text.replace(value, self._known[value])
        for pattern, replacement in PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def mask(self, data: Any, field: str | None = None) -> Any:
        """Mask any JSON-like value: dicts by key name, strings by known values and patterns."""
        if isinstance(data, dict):
            return {k: self.mask(v, k) for k, v in data.items()}
        if isinstance(data, list):
            return [self.mask(v, field) for v in data]
        if data is None or isinstance(data, bool):
            return data
        if field and self.is_sensitive_field(field):
            return self.mask_value(field, data)
        if isinstance(data, str):
            return self.mask_text(data)
        if isinstance(data, (int, float)):
            # Numbers under a non-sensitive key (step=3, attempts=2) are fine as they are.
            return data
        return self.mask_text(str(data))  # Decimal and anything else: treat as text
