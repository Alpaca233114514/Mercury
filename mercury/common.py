"""Shared strict values and canonical evidence serialization."""

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def number(value, *, positive=False):
    if isinstance(value, bool):
        raise ValueError("boolean is not a numeric value")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("invalid decimal") from None
    if not result.is_finite() or abs(result) > Decimal("1e15"):
        raise ValueError("number must be finite and within 1e15")
    if positive and result <= 0:
        raise ValueError("number must be positive")
    return result


def money(value):
    return number(value).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO-8601 string with timezone")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid timestamp") from None
    if dt.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def age_seconds(later, earlier):
    return (datetime.fromisoformat(timestamp(later)) - datetime.fromisoformat(timestamp(earlier))).total_seconds()


def symbol(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,19}", value):
        raise ValueError("symbol must be uppercase, 1-20 ticker characters")
    return value


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()
