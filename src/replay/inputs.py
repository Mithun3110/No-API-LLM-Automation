"""Input validation and placeholder filling for replay. Runs before anything touches the page."""

import re
from decimal import Decimal, InvalidOperation

from src.models import Recipe
from src.models.recipe import PLACEHOLDER


def validate_inputs(recipe: Recipe, raw: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Check inputs against the recipe's contract. Returns (clean values, errors)."""
    errors = []
    unknown = sorted(set(raw) - set(recipe.inputs))
    if unknown:
        errors.append(f"unknown input(s): {', '.join(unknown)} (expected: {', '.join(recipe.inputs)})")
    values: dict[str, str] = {}
    for name, spec in recipe.inputs.items():
        value = str(raw.get(name, "")).strip()
        if not value:
            if spec.required:
                errors.append(f"{name} is required")
            continue
        if spec.pattern and not re.fullmatch(spec.pattern, value):
            # Never echo a sensitive value back in an error: it would end up in logs.
            shown = "the value" if spec.sensitive else f"'{value}'"
            errors.append(f"{name}: {shown} does not match {spec.pattern}")
            continue
        if spec.type in ("number", "currency"):
            try:
                Decimal(value.replace(",", "").lstrip("$"))
            except InvalidOperation:
                errors.append(f"{name} must be a {spec.type}")
                continue
        values[name] = value
    return values, errors


def fill(template: str, values: dict[str, str]) -> str:
    """'/member/{{member_id}}' -> '/member/12345'. A missing value is a bug, so it raises."""
    return PLACEHOLDER.sub(lambda m: values[m.group(1)], template)
