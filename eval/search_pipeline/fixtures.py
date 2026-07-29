from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field


_HARD_NEGATIVE_MODES = frozenset(("hybrid", "lexical", "semantic"))


@dataclass(frozen=True)
class HardNegativeTarget:
    identity: str
    mode: str


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    domain: str
    query: str
    relevant: dict[str, int]
    intents: dict[str, list[str]] = field(default_factory=dict)
    k: int = 8
    query_class: str = "unspecified"
    hard_negatives: tuple[HardNegativeTarget, ...] = ()


def hard_negative_records(cases, traces) -> list[dict]:
    trace_list = list(traces)
    targets = []
    for case in cases:
        hard_negatives = case.hard_negatives
        if not isinstance(hard_negatives, tuple):
            targets.append((case, None))
            continue
        targets.extend((case, target) for target in hard_negatives)
    counts = {}
    for case, target in targets:
        mode = target.mode if isinstance(target, HardNegativeTarget) else None
        identity = (
            target.identity if isinstance(target, HardNegativeTarget) else None
        )
        if isinstance(mode, str) and isinstance(identity, str):
            key = (case.case_id, mode, identity)
            counts[key] = counts.get(key, 0) + 1

    records = []
    for case, target in targets:
        mode = target.mode if isinstance(target, HardNegativeTarget) else None
        identity = (
            target.identity if isinstance(target, HardNegativeTarget) else None
        )
        valid_target = (
            isinstance(target, HardNegativeTarget)
            and isinstance(mode, str)
            and isinstance(identity, str)
        )
        key = (case.case_id, mode, identity) if valid_target else None
        matching = [
            trace for trace in trace_list
            if isinstance(trace, dict)
            and trace.get("case_id") == case.case_id
            and trace.get("mode") == mode
        ]
        ranking = None
        candidates = None
        trace_k = None
        if len(matching) == 1:
            trace = matching[0]
            metrics_input = trace.get("metrics_input")
            stages = trace.get("stages")
            fusion = stages.get("fusion") if isinstance(stages, dict) else None
            trace_k = trace.get("k")
            if isinstance(metrics_input, dict) and isinstance(fusion, dict):
                ranking = metrics_input.get("ranking")
                candidates = fusion.get("candidate_identities")
        valid = (
            valid_target
            and mode in _HARD_NEGATIVE_MODES
            and isinstance(case.relevant, dict)
            and identity in case.relevant
            and counts.get(key) == 1
            and len(matching) == 1
            and isinstance(ranking, list)
            and all(isinstance(item, str) for item in ranking)
            and isinstance(candidates, list)
            and all(isinstance(item, str) for item in candidates)
            and isinstance(trace_k, int)
            and not isinstance(trace_k, bool)
            and trace_k > 0
        )
        baseline_rank = (
            candidates.index(identity) + 1
            if valid and identity in candidates
            else None
        )
        state = "invalid"
        if valid:
            state = "active" if (
                baseline_rank is not None
                and baseline_rank > trace_k
                and identity not in ranking
            ) else "unavailable"
        records.append({
            "case_id": case.case_id,
            "mode": mode,
            "identity": identity,
            "state": state,
            "baseline_rank": baseline_rank,
        })
    return sorted(
        records,
        key=lambda record: (
            repr(record["case_id"]),
            repr(record["mode"]),
            repr(record["identity"]),
        ),
    )


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
        query_class="exact_identifier",
    ),
    BenchmarkCase(
        case_id="update-page-api",
        domain="iwiki-mcp",
        query="wiki_update_page heading new_body source description status",
        relevant={
            "iwiki-mcp/architecture.md#wiki_update_page transaction:0": 3,
            "iwiki-mcp/mcp-server.md#Tool surface:0": 2,
        },
        intents={
            "transaction": [
                "iwiki-mcp/architecture.md#wiki_update_page transaction:0",
            ],
            "api": ["iwiki-mcp/mcp-server.md#Tool surface:0"],
        },
        query_class="exact_identifier",
    ),
    BenchmarkCase(
        case_id="stale-write-protection",
        domain="iwiki-mcp",
        query="prevent overwriting newer remote knowledge before changing a page",
        relevant={"iwiki-mcp/git-sync.md#Pre-write freshness guard:0": 3},
        intents={
            "freshness": ["iwiki-mcp/git-sync.md#Pre-write freshness guard:0"],
        },
        query_class="semantic_paraphrase",
        hard_negatives=(HardNegativeTarget(
            identity="iwiki-mcp/git-sync.md#Pre-write freshness guard:0",
            mode="lexical",
        ),),
    ),
    BenchmarkCase(
        case_id="binding-resolution",
        domain="iwiki-mcp",
        query="choose knowledge base and project domain from current workspace",
        relevant={
            "iwiki-mcp/base-binding.md#Resolving the binding:0": 3,
            "iwiki-mcp/base-binding.md#Binding model:0": 2,
        },
        intents={
            "binding": [
                "iwiki-mcp/base-binding.md#Resolving the binding:0",
                "iwiki-mcp/base-binding.md#Binding model:0",
            ],
        },
        query_class="semantic_paraphrase",
    ),
    BenchmarkCase(
        case_id="rerank-hydration",
        domain="iwiki-mcp",
        query="rerank candidates hydration stale provider top_n result fields",
        relevant={
            "iwiki-mcp/retrieval.md#Hybrid search:0": 3,
            "iwiki-mcp/retrieval.md#Result shape:0": 2,
            "iwiki-mcp/mcp-server.md#Tool surface:0": 2,
        },
        intents={
            "rerank": ["iwiki-mcp/retrieval.md#Hybrid search:0"],
            "shape": ["iwiki-mcp/retrieval.md#Result shape:0"],
            "api": ["iwiki-mcp/mcp-server.md#Tool surface:0"],
        },
        query_class="multi_intent",
    ),
    BenchmarkCase(
        case_id="embedding-storage-config",
        domain="iwiki-mcp",
        query="configure embedding endpoint dimensions and persist vectors",
        relevant={
            "iwiki-mcp/installation.md#Required environment:0": 3,
            "iwiki-mcp/indexing.md#Embeddings client:0": 3,
            "iwiki-mcp/indexing.md#Vector store:0": 2,
        },
        intents={
            "config": [
                "iwiki-mcp/installation.md#Required environment:0",
                "iwiki-mcp/indexing.md#Embeddings client:0",
            ],
            "storage": ["iwiki-mcp/indexing.md#Vector store:0"],
        },
        query_class="multi_intent",
    ),
    BenchmarkCase(
        case_id="chunking",
        domain="iwiki-mcp",
        query="Markdown chunking summary section chunk repeated heading",
        relevant={"iwiki-mcp/indexing.md#Markdown chunking:0": 3},
        intents={"chunking": ["iwiki-mcp/indexing.md#Markdown chunking:0"]},
        query_class="repeated_heading",
    ),
    BenchmarkCase(
        case_id="okf-repeated-section",
        domain="iwiki-mcp",
        query="migrate apply OKF frontmatter repeated section chunks",
        relevant={
            "iwiki-mcp/okf-governance.md#Migrate and apply tools:0": 3,
            "iwiki-mcp/okf-governance.md#Migrate and apply tools:1": 3,
        },
        intents={
            "migration": [
                "iwiki-mcp/okf-governance.md#Migrate and apply tools:0",
                "iwiki-mcp/okf-governance.md#Migrate and apply tools:1",
            ],
        },
        query_class="repeated_heading",
    ),
    BenchmarkCase(
        case_id="sync-locking",
        domain="iwiki-mcp",
        query="coordinate concurrent pull rebase push without repository races",
        relevant={
            "iwiki-mcp/git-sync.md#Inter-process locking:0": 3,
            "iwiki-mcp/git-sync.md#Explicit sync:0": 2,
        },
        intents={
            "concurrency": ["iwiki-mcp/git-sync.md#Inter-process locking:0"],
            "sync": ["iwiki-mcp/git-sync.md#Explicit sync:0"],
        },
        query_class="graph_distractor",
    ),
    BenchmarkCase(
        case_id="related-sections",
        domain="iwiki-mcp",
        query="find neighboring knowledge through vectors links and backlinks",
        relevant={"iwiki-mcp/retrieval.md#Related sections:0": 3},
        intents={"related": ["iwiki-mcp/retrieval.md#Related sections:0"]},
        query_class="graph_distractor",
        hard_negatives=(HardNegativeTarget(
            identity="iwiki-mcp/retrieval.md#Related sections:0",
            mode="semantic",
        ),),
    ),
    BenchmarkCase(
        case_id="search-scope",
        domain="iwiki-mcp",
        query="project bound explicit domains scope resolution for wiki search",
        relevant={
            "iwiki-mcp/base-binding.md#Search scope:0": 3,
            "iwiki-mcp/mcp-server.md#Tool surface:0": 2,
        },
        intents={
            "scope": ["iwiki-mcp/base-binding.md#Search scope:0"],
            "api": ["iwiki-mcp/mcp-server.md#Tool surface:0"],
        },
        query_class="competing_evidence",
    ),
    BenchmarkCase(
        case_id="frontmatter-migration",
        domain="iwiki-mcp",
        query="derive type tags from source log then migrate metadata",
        relevant={
            "iwiki-mcp/okf-governance.md#Frontmatter assembly:0": 3,
            "iwiki-mcp/okf-governance.md#Migrate and apply tools:0": 2,
        },
        intents={
            "assembly": ["iwiki-mcp/okf-governance.md#Frontmatter assembly:0"],
            "migration": [
                "iwiki-mcp/okf-governance.md#Migrate and apply tools:0",
            ],
        },
        query_class="competing_evidence",
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
