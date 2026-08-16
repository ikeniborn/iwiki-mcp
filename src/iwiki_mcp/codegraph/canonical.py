"""One canonical JSON and SHA-256 implementation for code-graph data."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize a JSON-compatible value using the existing canonical bytes."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_bytes_sha256(payload: bytes, *, prefix: bool) -> str:
    """Hash bytes already produced by the canonical JSON serializer."""
    digest = hashlib.sha256(payload).hexdigest()
    return f"sha256:{digest}" if prefix else digest


def canonical_sha256(value: Any, *, prefix: bool) -> str:
    """Hash canonical JSON bytes, optionally using the snapshot hash prefix."""
    return canonical_bytes_sha256(canonical_json_bytes(value), prefix=prefix)
