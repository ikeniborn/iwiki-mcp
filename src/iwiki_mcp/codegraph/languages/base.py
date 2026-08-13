"""Language-neutral adapter contract."""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ..models import ParsedFile, ResolutionResult

if TYPE_CHECKING:
    # The project index is introduced by the resolution layer.  Keeping this
    # import type-only prevents parser adapters from coupling to that layer.
    from ..resolver import SymbolIndex


class LanguageAdapter(Protocol):
    """Extract declarations, then resolve them in a later language-neutral pass."""

    language: str
    prefix: str
    extensions: tuple[str, ...]

    def parse_file(self, source: bytes, path: str) -> ParsedFile:
        """Parse unexecuted source bytes into stable declarations."""

    def resolve_references(
        self, parsed: ParsedFile, project_index: "SymbolIndex"
    ) -> ResolutionResult:
        """Resolve references after project indexing (not implemented by parser task)."""
