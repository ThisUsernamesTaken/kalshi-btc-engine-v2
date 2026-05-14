from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

ZERO = Decimal("0")
ONE = Decimal("1")
_MISSING = object()


def decimal_from_fixed(
    value: Any, *, default: Decimal | None | object = _MISSING
) -> Decimal | None:
    """Parse exchange fixed-point strings without passing through float."""
    if value is None:
        if default is not _MISSING:
            return default
        raise ValueError("missing decimal value")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        if default is not _MISSING:
            return default
        raise ValueError(f"invalid decimal value: {value!r}") from exc


def decimal_to_str(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def quantile_median(values: list[Decimal]) -> Decimal:
    if not values:
        raise ValueError("cannot compute median of empty sequence")
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / Decimal("2")
