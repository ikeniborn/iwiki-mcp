"""Shared code-graph reader protocol."""
from __future__ import annotations

from typing import Protocol

from .context import ContextRequest
from .query import ValidatedSearchRequest


class CodeGraphReader(Protocol):
    def status(self) -> dict[str, object]: ...

    def search(self, request: ValidatedSearchRequest) -> dict[str, object]: ...

    def context(self, request: ContextRequest) -> dict[str, object]: ...
