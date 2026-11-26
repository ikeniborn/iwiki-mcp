"""Read OKF generation and verification records without modifying their history."""
from __future__ import annotations

from datetime import date, datetime, time, timezone
import re


_DATE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
_INSTANT = re.compile(
    "(" + _DATE + r"[Tt][0-9]{2}:[0-5][0-9]:[0-5][0-9])(?:\.([0-9]+))?"
    r"([Zz]|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*")
_PLACEHOLDERS = {"?", "-", "anonymous", "unknown", "unspecified", "none", "null", "n/a"}


def instant_key(value: object) -> tuple[datetime, str] | None:
    """Return UTC time and any remaining decimal digits for exact timestamp ordering."""
    remainder = ""
    try:
        if isinstance(value, str):
            if re.fullmatch(_DATE, value):
                value = date.fromisoformat(value)
            else:
                match = _INSTANT.fullmatch(value)
                if not match:
                    return None
                base, fraction, offset = match.groups()
                fraction = fraction or ""
                # Python 3.10 accepts exactly three or six fractional digits.
                micros = fraction[:6].ljust(6, "0")
                remainder = fraction[6:].rstrip("0")
                if offset.upper() == "Z":
                    offset = "+00:00"
                value = datetime.fromisoformat(f"{base}.{micros}{offset}")
        if isinstance(value, datetime):
            if value.utcoffset() is None:
                return None
            return value.astimezone(timezone.utc), remainder
        if isinstance(value, date):
            return datetime.combine(value, time.min, tzinfo=timezone.utc), remainder
    except (ValueError, OverflowError):
        pass
    return None


def instant(value: object) -> datetime | None:
    """Interpret an offset date-time or a calendar date at midnight UTC."""
    parsed = instant_key(value)
    return parsed[0] if parsed is not None else None


def _identified(value: str) -> bool:
    return bool(value and value.strip() == value and value.isprintable()
                and value.casefold() not in _PLACEHOLDERS
                and not (value.startswith("<") and value.endswith(">")))


def valid_actor(value: object) -> bool:
    """Accept identified scheme actors and imported producer/version agents."""
    if not isinstance(value, str):
        return False
    kind, separator, identifier = value.partition(":")
    if separator:
        if not _SCHEME.fullmatch(kind) or not _identified(identifier):
            return False
        if kind != "agent":
            return True
    else:
        identifier = value
    producer, slash, version = identifier.partition("/")
    return bool(slash and _identified(producer) and _identified(version))


def tier(meta: dict) -> str:
    """Return the strongest check that covers the current page generation."""
    generated = meta.get("generated")
    since = instant_key(generated.get("at")) if isinstance(generated, dict) else None
    if "generated" in meta and since is None:
        return "unverified"
    events = meta.get("verified")
    if isinstance(events, dict):
        events = [events]
    result = "unverified"
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict) or not valid_actor(event.get("by")):
                continue
            at = instant_key(event.get("at"))
            if at is None or (since is not None and at < since):
                continue
            if event["by"].startswith("human:"):
                return "human-reviewed"
            result = "machine-confirmed"
    return result
