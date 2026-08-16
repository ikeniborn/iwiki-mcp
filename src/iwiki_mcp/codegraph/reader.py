"""Shared code-graph reader protocol."""
from __future__ import annotations

from typing import Protocol

from .context import ContextRequest
from .query import ValidatedSearchRequest


MISSING_READ_RESULT = {
    "state": "missing",
    "fresh": False,
    "error": "missing_snapshot",
}
EMPTY_CONTEXT_RESULT = {
    "nodes": [],
    "relations": [],
    "files": [],
    "wiki_pages": [],
    "warnings": [],
}


class CodeGraphReader(Protocol):
    def status(self) -> dict[str, object]: ...

    def search(self, request: ValidatedSearchRequest) -> dict[str, object]: ...

    def context(self, request: ContextRequest) -> dict[str, object]: ...
