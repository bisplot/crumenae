"""Small helpers shared by every module: decimals, JSON, time."""
from __future__ import annotations

import decimal
import json
import time
from decimal import Decimal
from typing import Any, Optional

# Token amounts can have 18 decimals and 10+ integer digits: keep plenty of precision.
decimal.DefaultContext.prec = 60
decimal.setcontext(decimal.Context(prec=60))

ZERO = Decimal(0)


def D(value: Any) -> Decimal:
    """Convert to Decimal without going through binary floats when avoidable."""
    if value is None or value == "":
        return ZERO
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value))


def dstr(value: Optional[Decimal]) -> Optional[str]:
    """Plain (non-scientific) string without trailing zeros, or None."""
    if value is None:
        return None
    s = format(value, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("", "-0", "-"):
        s = "0"
    return s


def scaled(raw: Any, decimals: int) -> Decimal:
    """Integer base units (wei, lamports, sun, satoshi...) to a token amount."""
    return D(int(raw)) / (Decimal(10) ** int(decimals))


def now() -> int:
    return int(time.time())


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=_json_default, ensure_ascii=False)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return dstr(obj)
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    raise TypeError(f"not JSON serializable: {type(obj).__name__}")


def round_eur(value: Optional[Decimal]) -> Optional[Decimal]:
    if value is None:
        return None
    return value.quantize(Decimal("0.01"), rounding=decimal.ROUND_HALF_UP)
