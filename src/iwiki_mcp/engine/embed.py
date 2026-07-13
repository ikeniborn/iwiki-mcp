"""OpenAI-compatible embeddings client. Batches inputs; respects HTTPS_PROXY.

Transient backend failures (timeouts, connection errors, HTTP 5xx) are retried
with bounded exponential backoff before surfacing as EmbedError.
"""
from __future__ import annotations
import math
import time
import httpx
from .config import Config

_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.5  # seconds; doubled each retry


class EmbedError(RuntimeError):
    """Raised when the embedding backend is unreachable or errors (stop rule)."""


def _is_transient(exc: httpx.HTTPError) -> bool:
    """Timeouts, connection/transport failures, and HTTP 5xx are worth retrying."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)


def probe_embedding_endpoint(cfg: Config) -> None:
    """Validate the configured embedding endpoint with one startup request."""
    url = f"{cfg.base_url}/embeddings"
    payload = {
        "model": cfg.embed_model,
        "input": ["iwiki startup probe"],
        "dimensions": cfg.dimensions,
    }
    headers = {"Authorization": f"Bearer {cfg.api_key}"}

    try:
        response = httpx.post(
            url,
            json=payload,
            headers=headers,
            timeout=10.0,
        )
        response.raise_for_status()
    except httpx.InvalidURL as exc:
        raise EmbedError("embedding probe URL is invalid") from exc
    except httpx.TimeoutException as exc:
        raise EmbedError("embedding endpoint probe timed out") from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        reason = exc.response.reason_phrase or "Unknown"
        raise EmbedError(
            f"embedding endpoint returned HTTP {status} {reason}"
        ) from exc
    except httpx.TransportError as exc:
        raise EmbedError("embedding endpoint probe transport error") from exc

    try:
        document = response.json()
    except (TypeError, ValueError) as exc:
        raise EmbedError("embedding endpoint returned malformed JSON") from exc

    data = document.get("data") if isinstance(document, dict) else None
    if not isinstance(data, list) or len(data) != 1:
        raise EmbedError(
            "embedding endpoint response must contain exactly one data row"
        )

    row = data[0]
    if not isinstance(row, dict) or "embedding" not in row:
        raise EmbedError("embedding endpoint response is missing embedding vector")

    embedding = row["embedding"]
    if (
        not isinstance(embedding, list)
        or not embedding
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or (isinstance(value, float) and not math.isfinite(value))
            for value in embedding
        )
    ):
        raise EmbedError("embedding endpoint returned invalid embedding vector")

    if len(embedding) != cfg.dimensions:
        raise EmbedError(
            "embedding endpoint dimension mismatch: "
            f"expected {cfg.dimensions}, got {len(embedding)}"
        )


def embed_texts(cfg: Config, texts: list[str]) -> list[list[float]]:
    """Return one float vector per input text. Raises EmbedError on failure."""
    if not texts:
        return []
    url = f"{cfg.base_url}/embeddings"
    payload: dict = {"model": cfg.embed_model, "input": texts}
    if cfg.dimensions:
        payload["dimensions"] = cfg.dimensions
    headers = {"Authorization": f"Bearer {cfg.api_key}"}
    last: httpx.HTTPError | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = httpx.post(url, json=payload, headers=headers, timeout=60.0)
            resp.raise_for_status()
            data = resp.json().get("data", [])
            return [row["embedding"] for row in sorted(data, key=lambda r: r["index"])]
        except httpx.HTTPError as e:
            last = e
            if attempt + 1 < _MAX_ATTEMPTS and _is_transient(e):
                time.sleep(_BACKOFF_BASE * (2 ** attempt))
                continue
            break
    raise EmbedError(f"embedding backend unreachable: {last}") from last
