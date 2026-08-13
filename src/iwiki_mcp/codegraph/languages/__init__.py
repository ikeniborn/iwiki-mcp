"""Language-specific static source adapters.

Adapters are intentionally not imported by :mod:`iwiki_mcp.server`: grammar
loading is deferred until an adapter parses source.
"""

from .base import LanguageAdapter

__all__ = ["LanguageAdapter"]
