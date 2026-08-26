"""Fixed synthetic inputs for unified-search evaluation."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, MappingProxyType):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class WikiResponse:
    results: tuple[Any, ...] = ()
    error_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "results", tuple(_freeze(item) for item in self.results))

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"results": _thaw(self.results)}
        if self.error_code:
            value["error"] = {"code": self.error_code}
        return value


@dataclass(frozen=True)
class CodeResponse:
    state: str = "ready"
    fresh: bool = True
    revision: str = "revision-1"
    results: tuple[Any, ...] = ()
    error_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "results", tuple(_freeze(item) for item in self.results))

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "state": self.state, "fresh": self.fresh, "revision": self.revision,
            "results": _thaw(self.results),
        }
        if self.error_code:
            value["error"] = {"code": self.error_code}
        return value


@dataclass(frozen=True)
class ContextResponse:
    fresh: bool = True
    revision: str = "revision-1"
    seeds: tuple[str, ...] = ()
    nodes: tuple[Any, ...] = ()
    relations: tuple[Any, ...] = ()
    files: tuple[Any, ...] = ()
    wiki_pages: tuple[Any, ...] = ()
    truncated: bool = False
    warnings: tuple[str, ...] = ()
    error_code: str | None = None
    wiki_links_stale: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "seeds", tuple(self.seeds))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        for name in ("nodes", "relations", "files", "wiki_pages"):
            object.__setattr__(self, name, tuple(_freeze(item) for item in getattr(self, name)))

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "fresh": self.fresh, "revision": self.revision,
            "seeds": list(self.seeds), "nodes": _thaw(self.nodes),
            "relations": _thaw(self.relations), "files": _thaw(self.files),
            "wiki_pages": _thaw(self.wiki_pages),
            "limits": {"depth": 1, "nodes": 10}, "truncated": self.truncated,
            "warnings": list(self.warnings),
        }
        if self.error_code:
            value["error"] = {"code": self.error_code}
        if self.wiki_links_stale:
            value["wiki_links_stale"] = True
        return value


@dataclass(frozen=True)
class UnifiedSearchCase:
    id: str
    task_prompt: str
    backend_label: str
    wiki: WikiResponse
    code: CodeResponse
    context: ContextResponse
    expected_fact_ids: tuple[str, ...]
    expected_graph_state: str
    coordinated_meaning_code: bool
    scope_label: str = "synthetic-eval-scope"

    def __post_init__(self) -> None:
        object.__setattr__(self, "expected_fact_ids", tuple(self.expected_fact_ids))


_WIKI = WikiResponse(results=({"fact_id": "wiki-policy", "title": "Policy meaning"},))
_CODE = CodeResponse(results=({"entity_id": "entity-policy", "fact_id": "code-policy"},))
_CONTEXT = ContextResponse(
    seeds=("entity-policy",), nodes=({"entity_id": "entity-policy"},),
    wiki_pages=({"fact_id": "association-policy", "slug": "policy"},),
)

FIXED_CASES: tuple[UnifiedSearchCase, ...] = (
    UnifiedSearchCase("linked-meaning-code", "Find policy meaning and implementation", "sqlite", _WIKI, _CODE, _CONTEXT, ("wiki-policy", "code-policy", "association-policy"), "ready", True),
    UnifiedSearchCase("relevant-code-no-association", "Find standalone implementation", "postgresql", WikiResponse(), _CODE, ContextResponse(seeds=("entity-policy",), nodes=({"entity_id": "entity-policy"},)), ("code-policy",), "ready", False),
    UnifiedSearchCase("wiki-only", "Find documented policy", "hosted", _WIKI, CodeResponse(), ContextResponse(), ("wiki-policy",), "ready", False),
    UnifiedSearchCase("code-empty", "Find absent code", "sqlite", WikiResponse(), CodeResponse(), ContextResponse(), (), "ready", False),
    UnifiedSearchCase("code-graph-missing", "Check missing graph", "postgresql", _WIKI, CodeResponse(state="missing", fresh=False), ContextResponse(), ("wiki-policy",), "missing", False),
    UnifiedSearchCase("code-graph-dirty", "Check dirty graph", "hosted", _WIKI, CodeResponse(state="dirty", fresh=False), ContextResponse(), ("wiki-policy",), "dirty", False),
    UnifiedSearchCase("code-graph-busy", "Check busy graph", "sqlite", _WIKI, CodeResponse(state="busy", fresh=False), ContextResponse(), ("wiki-policy",), "busy", False),
    UnifiedSearchCase("code-graph-stale", "Check stale graph", "postgresql", _WIKI, CodeResponse(state="stale", fresh=False), ContextResponse(), ("wiki-policy",), "stale", False),
    UnifiedSearchCase("wiki-links-stale", "Find stale association", "hosted", _WIKI, _CODE, ContextResponse(seeds=("entity-policy",), wiki_pages=({"fact_id": "association-policy"},), wiki_links_stale=True), ("wiki-policy", "code-policy"), "ready", False),
    UnifiedSearchCase("context-truncated", "Find truncated context", "sqlite", _WIKI, _CODE, ContextResponse(seeds=("entity-policy",), truncated=True, warnings=("node_limit",)), ("wiki-policy", "code-policy"), "ready", False),
    UnifiedSearchCase("revision-mismatch", "Find revision mismatch", "postgresql", _WIKI, _CODE, ContextResponse(revision="revision-2", seeds=("entity-policy",)), ("wiki-policy", "code-policy"), "ready", False),
    UnifiedSearchCase("wiki-embedding-failure", "Find embedding failure", "hosted", WikiResponse(error_code="embedding_failed"), _CODE, _CONTEXT, ("code-policy", "association-policy"), "ready", True),
    UnifiedSearchCase("wiki-rerank-failure", "Find rerank failure", "sqlite", WikiResponse(error_code="rerank_failed"), _CODE, _CONTEXT, ("code-policy", "association-policy"), "ready", True),
    UnifiedSearchCase("code-reader-failure", "Find reader failure", "postgresql", _WIKI, CodeResponse(fresh=False, state="failed", error_code="reader_failed"), ContextResponse(), ("wiki-policy",), "failed", False),
    UnifiedSearchCase("invalid-filters", "Find filtered facts", "hosted", WikiResponse(error_code="invalid_filters"), CodeResponse(fresh=False, state="failed", error_code="invalid_filters"), ContextResponse(), (), "failed", False),
    UnifiedSearchCase("out-of-scope-domains", "Find scoped facts", "sqlite", WikiResponse(error_code="out_of_scope"), CodeResponse(fresh=False, state="failed", error_code="out_of_scope"), ContextResponse(), (), "failed", False),
    UnifiedSearchCase("sqlite-postgres-hosted-labels", "Check backend labels", "sqlite+postgresql+hosted", _WIKI, _CODE, _CONTEXT, ("wiki-policy", "code-policy", "association-policy"), "ready", True),
)
