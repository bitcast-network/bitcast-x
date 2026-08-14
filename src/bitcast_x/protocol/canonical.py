"""Consensus-stable JSON normalization and domain-separated hashing."""

import hashlib
import json
import math
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel

from bitcast_x.errors import ProtocolError


def normalize_text(value: str) -> str:
    """Normalize protocol strings with Unicode NFKC."""

    return unicodedata.normalize("NFKC", value)


def _normalize(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _normalize(value.model_dump(mode="python", exclude_none=False))
    if isinstance(value, Enum):
        return _normalize(value.value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ProtocolError("canonical timestamps must be timezone-aware")
        utc_value = value.astimezone(UTC)
        if utc_value.microsecond:
            rendered = utc_value.isoformat(timespec="microseconds")
        else:
            rendered = utc_value.isoformat(timespec="seconds")
        return rendered.replace("+00:00", "Z")
    if isinstance(value, str):
        return normalize_text(value)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProtocolError("canonical JSON does not allow non-finite numbers")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProtocolError("canonical JSON object keys must be strings")
            normalized_key = normalize_text(key)
            if normalized_key in normalized:
                raise ProtocolError("object keys collide after NFKC normalization")
            normalized[normalized_key] = _normalize(item)
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, memoryview)):
        return [_normalize(item) for item in value]
    raise ProtocolError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> bytes:
    """Encode a value as UTF-8 canonical JSON for consensus hashing."""

    try:
        return json.dumps(
            _normalize(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"value cannot be encoded canonically: {exc}") from exc


def hash_value(domain: str, value: Any) -> bytes:
    """Hash a canonical value with an unambiguous UTF-8 domain prefix."""

    normalized_domain = normalize_text(domain)
    if not normalized_domain or "\x00" in normalized_domain:
        raise ProtocolError("hash domain must be non-empty and contain no NUL")
    return hashlib.sha256(
        normalized_domain.encode("utf-8") + b"\x00" + canonical_json(value)
    ).digest()


def hash_hex(domain: str, value: Any) -> str:
    """Return a lower-case hexadecimal domain-separated hash."""

    return hash_value(domain, value).hex()
