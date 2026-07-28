from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    domain: str
    query: str
    relevant: dict[str, int]
    intents: dict[str, list[str]] = field(default_factory=dict)
    k: int = 8


DEFAULT_LIVE_CASES = [
    BenchmarkCase(
        case_id="search-mode-api",
        domain="iwiki-mcp",
        query="IWIKI_SEARCH_MODE semantic lexical hybrid wiki_search mode enum",
        relevant={
            "iwiki-mcp/mcp-server.md#Tool surface:0": 3,
            "iwiki-mcp/retrieval.md#Hybrid search:0": 2,
        },
        intents={
            "api": ["iwiki-mcp/mcp-server.md#Tool surface:0"],
            "retrieval": ["iwiki-mcp/retrieval.md#Hybrid search:0"],
        },
    ),
    BenchmarkCase(
        case_id="chunking",
        domain="iwiki-mcp",
        query="Markdown chunking summary section chunk repeated heading",
        relevant={
            "iwiki-mcp/indexing.md#Markdown chunking:0": 3,
        },
        intents={
            "chunking": ["iwiki-mcp/indexing.md#Markdown chunking:0"],
        },
    ),
    BenchmarkCase(
        case_id="rerank-hydration",
        domain="iwiki-mcp",
        query="rerank candidates hydration stale provider top_n",
        relevant={
            "iwiki-mcp/retrieval.md#Hybrid search:0": 3,
            "iwiki-mcp/retrieval.md#Result shape:0": 2,
            "iwiki-mcp/mcp-server.md#Tool surface:0": 2,
        },
        intents={
            "rerank": ["iwiki-mcp/retrieval.md#Hybrid search:0"],
            "shape": ["iwiki-mcp/retrieval.md#Result shape:0"],
        },
    ),
]


OFFLINE_CASES = [
    BenchmarkCase(
        case_id="offline-symbol",
        domain="eval",
        query="refresh_token credentials",
        relevant={"eval/guide/auth.md#Rotation:0": 3},
        intents={"symbol": ["eval/guide/auth.md#Rotation:0"]},
        k=3,
    ),
    BenchmarkCase(
        case_id="offline-lost-before-top-k",
        domain="eval",
        query="needle late chunk",
        relevant={"eval/guide/long.md#Details:1": 3},
        intents={"chunk": ["eval/guide/long.md#Details:1"]},
        k=3,
    ),
]
