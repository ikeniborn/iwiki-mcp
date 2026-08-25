"""One-shot local code-graph build and publication application service."""
from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Mapping

from iwiki_mcp import base as wiki_base
from iwiki_mcp.storage import GitBinding, PostgresBinding

from . import config as codegraph_config
from . import indexer as codegraph_indexer
from . import linking
from . import runtime as codegraph_runtime
from .languages import javascript, python, typescript
from .models import CodeGraphError


class CodeGraphApplicationError(CodeGraphError):
    code = "invalid_config"


@dataclass(frozen=True)
class CodeGraphSourceContext:
    base: str
    project_dir: str
    primary: str
    wiki_base: str | None


def source_context(
    binding: GitBinding | PostgresBinding,
) -> CodeGraphSourceContext:
    if binding.primary is None:
        raise CodeGraphApplicationError("primary domain is required")
    if isinstance(binding, PostgresBinding):
        if not wiki_base.ensure_graph_store_excluded(binding.project_dir):
            raise CodeGraphApplicationError(
                "local code graph cache exclusion is required"
            )
        return CodeGraphSourceContext(
            base=binding.project_dir,
            project_dir=binding.project_dir,
            primary=binding.primary,
            wiki_base=None,
        )
    return CodeGraphSourceContext(
        base=binding.base,
        project_dir=binding.project_dir,
        primary=binding.primary,
        wiki_base=binding.base,
    )


def _distribution_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unavailable"


_PYTHON_PARSER_VERSION = (
    "tree-sitter-python:" + _distribution_version("tree-sitter-python")
)
_TYPESCRIPT_PARSER_VERSION = (
    "tree-sitter-typescript:" + _distribution_version("tree-sitter-typescript")
)


def code_graph_adapter_factories(
    repository_id: str,
    config: codegraph_config.CodeGraphConfig | None = None,
) -> Mapping[str, codegraph_indexer.AdapterFactory]:
    def create_python_adapter(source_paths):
        return getattr(python, "Python" + "Adapter")(
            repository_id,
            source_paths,
            parser_version=_PYTHON_PARSER_VERSION,
        )

    def create_typescript_adapter(source_paths):
        return typescript.TypeScriptAdapter(
            repository_id,
            source_paths,
            parser_version=_TYPESCRIPT_PARSER_VERSION,
            type_boost_enabled=bool(
                config is not None and config.typescript_type_boost
            ),
        )

    def create_javascript_adapter(source_paths):
        return javascript.JavaScriptAdapter(
            repository_id,
            source_paths,
            parser_version=_TYPESCRIPT_PARSER_VERSION,
        )

    return {
        "python": codegraph_indexer.AdapterFactory(
            create=create_python_adapter,
            extensions=(".py",),
            parser_version=_PYTHON_PARSER_VERSION,
            grammar_version=";".join((
                "tree-sitter:" + _distribution_version("tree-sitter"),
                "tree-sitter-language-pack:"
                + _distribution_version("tree-sitter-language-pack"),
                _PYTHON_PARSER_VERSION,
            )),
            adapter_version="python-adapter-v2",
        ),
        "typescript": codegraph_indexer.AdapterFactory(
            create=create_typescript_adapter,
            extensions=(".ts", ".tsx"),
            parser_version=_TYPESCRIPT_PARSER_VERSION,
            grammar_version=";".join((
                "tree-sitter:" + _distribution_version("tree-sitter"),
                "tree-sitter-language-pack:"
                + _distribution_version("tree-sitter-language-pack"),
                _TYPESCRIPT_PARSER_VERSION,
            )),
            adapter_version="typescript-adapter-v1",
        ),
        "javascript": codegraph_indexer.AdapterFactory(
            create=create_javascript_adapter,
            extensions=(".js", ".jsx", ".mjs", ".cjs"),
            parser_version=_TYPESCRIPT_PARSER_VERSION,
            grammar_version=";".join((
                "tree-sitter:" + _distribution_version("tree-sitter"),
                "tree-sitter-language-pack:"
                + _distribution_version("tree-sitter-language-pack"),
                _TYPESCRIPT_PARSER_VERSION,
            )),
            adapter_version="javascript-adapter-v1",
        ),
    }


def code_runtime(
    source: CodeGraphSourceContext,
) -> codegraph_runtime.CodeGraphRuntime:
    try:
        config = codegraph_config.load_code_graph_config(source.project_dir)
    except codegraph_config.CodeGraphConfigError:
        config = None
    runtime = codegraph_runtime.CodeGraphRuntime(
        source,
        adapter_factories=code_graph_adapter_factories(source.primary, config),
    )
    if runtime._indexer is not None and source.wiki_base is not None:
        runtime._indexer.wiki_selector_resolver = linking.WikiSelectorResolver(
            source.wiki_base
        )
    return runtime
