import dataclasses
from importlib.metadata import requires, version
from pathlib import Path

import iwiki_mcp
from packaging.requirements import Requirement
from iwiki_mcp.codegraph.config import CodeGraphConfig


def test_package_version_matches_distribution_metadata():
    assert iwiki_mcp.__version__ == version("iwiki-mcp")


def test_package_metadata_rejects_mcp_v2():
    dependencies = requires("iwiki-mcp") or []
    mcp_requirement = next(
        Requirement(dependency)
        for dependency in dependencies
        if Requirement(dependency).name == "mcp"
    )

    assert not mcp_requirement.specifier.contains("2.0.0")


def test_code_graph_benchmark_package_version():
    assert iwiki_mcp.__version__ == "0.7.146"


def test_user_docs_describe_python_code_graph_mvp_contract():
    text = Path("README.md").read_text(encoding="utf-8")

    assert all(
        name in text
        for name in (
            "wiki_code_status",
            "wiki_code_index",
            "wiki_code_search",
            "wiki_code_context",
        )
    )
    assert "Incremental indexing is not part of the Python MVP" in text
    assert "TypeScript is not part of the Python MVP" in text
    assert "deterministic full rebuild" in text
    assert "schema-v1" in text
    assert "uv run python -m eval.code_graph" in text
    assert "<500 ms" in text
    assert "<150 ms" in text
    assert "incremental" not in {field.name for field in dataclasses.fields(CodeGraphConfig)}
    assert not Path("src/iwiki_mcp/codegraph/languages/typescript.py").exists()


def test_docs_describe_hosted_domain_authority_contract():
    english = Path("README.md").read_text(encoding="utf-8")
    russian = Path("docs/README.ru.md").read_text(encoding="utf-8")
    architecture = Path("docs/architecture.md").read_text(encoding="utf-8")
    required = (
        "can_create_domain",
        "managed_domains",
        "wiki_list_domain_grants",
        "wiki_set_domain_grant",
        "wiki_revoke_domain_grant",
        "token set-create-domain",
        "token set-domain-management",
        ".iwiki.toml",
        ".iwikiignore",
        "selected",
        "effective",
    )

    for text in (english, russian, architecture):
        assert all(term in text for term in required)
        assert "can_manage_grants" in text
    for text in (english, architecture):
        normalized = " ".join(text.lower().split())
        assert "no down migration" in normalized
        assert "management authority cannot be delegated" in normalized
    normalized_russian = " ".join(russian.split())
    assert "down migration отсутствует" in normalized_russian
    assert "management authority нельзя делегировать" in normalized_russian
