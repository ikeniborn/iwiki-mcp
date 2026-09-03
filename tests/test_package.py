import dataclasses
import subprocess
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
    assert iwiki_mcp.__version__ == "0.7.243"


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
    assert "TypeScript support is Tree-sitter-only static extraction" in text
    assert "deterministic full rebuild" in text
    assert "schema-v1" in text
    assert "uv run python -m eval.code_graph" in text
    assert "<500 ms" in text
    assert "<150 ms" in text
    assert "incremental" not in {field.name for field in dataclasses.fields(CodeGraphConfig)}


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


def test_docs_retain_separate_wiki_and_code_search_workflow():
    documents = (
        (
            Path("README.md"),
            "`wiki_unified_search` remains intentionally unregistered",
            "docs/superpowers/evidence/",
        ),
        (
            Path("docs/README.ru.md"),
            "`wiki_unified_search` намеренно не зарегистрирован",
            "superpowers/evidence/",
        ),
        (
            Path("docs/architecture.md"),
            "`wiki_unified_search` remains intentionally unregistered",
            "superpowers/evidence/",
        ),
    )

    for path, status, evidence_prefix in documents:
        text = path.read_text(encoding="utf-8")
        assert status in text
        assert "do_not_implement" in text
        assert "wiki_search → wiki_code_search → wiki_code_context" in text
        assert text.count("wiki_unified_search") == 1
        for filename in (
            "wiki-unified-search-evaluation.md",
            "wiki-unified-search-evaluation.json",
        ):
            href = f"{evidence_prefix}{filename}"
            assert f"]({href})" in text
            assert (path.parent / href).is_file()


def test_postgres_tool_matrix_includes_section_mutations():
    documents = (
        (Path("README.md"), "### PostgreSQL MCP tool contract", "Git-only tools"),
        (
            Path("docs/README.ru.md"),
            "### Контракт MCP-инструментов PostgreSQL",
            "Git-only инструменты",
        ),
    )

    for path, heading, boundary in documents:
        text = path.read_text(encoding="utf-8")
        section = text[text.index(heading):text.index(boundary, text.index(heading))]
        assert all(
            tool in section
            for tool in (
                "wiki_insert_section",
                "wiki_delete_section",
                "wiki_move_section",
            )
        )


def test_publisher_operator_docs_define_safe_scheduled_publication_contract():
    english = Path("README.md").read_text(encoding="utf-8")
    russian = Path("docs/README.ru.md").read_text(encoding="utf-8")
    architecture = Path("docs/architecture.md").read_text(encoding="utf-8")

    command = "iwiki-mcp code publish --project <checkout> [--json]"
    for text in (english, russian, architecture):
        assert command in text
        assert "<project>/.iwiki/code-<domain>.sqlite3" in text
        assert "<wiki-base>/.iwiki/code-<domain>.sqlite3" in text
        assert ".git/info/exclude" in text
        assert "IWIKI_DB_PASSWORD" in text
        assert "IWIKI_EMBED_MODEL" in text
        assert "IWIKI_EMBED_DIMENSIONS" in text
        assert "IWIKI_CODE_GRAPH_MCP_URL" in text
        assert "IWIKI_CODE_GRAPH_MCP_TOKEN" in text
        assert "wiki_code_status" in text
        assert "fresh == true" in text
        assert "wiki_code_search" in text
        assert "wiki_code_context" in text
        assert "wiki_search" in text

    normalized_english = " ".join(english.split())
    normalized_russian = " ".join(russian.split())
    normalized_architecture = " ".join(architecture.split())

    assert all(
        term in normalized_english
        for term in (
            "exactly-one `publish_mode`",
            "do not improvise fallback",
            "`0` when ready",
            "`1` for runtime/publication failure",
            "`2` for usage/configuration failure",
            "no password, token, URL, DSN, or checkout path",
            "existing publisher abstraction",
            "remote Streamable HTTP",
            "not implemented",
        )
    )
    assert all(
        term in normalized_russian
        for term in (
            "ровно-одного `publish_mode`",
            "не придумывайте fallback",
            "`0`, когда snapshot ready",
            "`1` при runtime/publication failure",
            "`2` при usage/configuration failure",
            "password, token, URL, DSN или checkout path",
            "publisher abstraction",
            "не реализован",
        )
    )
    assert all(
        term in normalized_architecture
        for term in (
            "`publish_mode` selects exactly one",
            "no adapter fallback",
            "`0` for a ready snapshot, `1` for runtime/publication failure, "
            "or `2` for usage/configuration",
            "redact password, token, URL, DSN, and paths",
            "publisher abstraction",
            "local or remote Streamable HTTP",
            "wiki/code search is future capability, not an implemented interface",
        )
    )

    systemd_contract = (
        "[Unit]",
        "Description=Publish iwiki code graph",
        "[Service]",
        "Type=oneshot",
        "User=iwiki",
        "WorkingDirectory=/srv/project",
        "EnvironmentFile=/etc/iwiki/codegraph-publisher.env",
        "ExecStart=/usr/local/bin/iwiki-mcp code publish --project /srv/project --json",
        "Description=Schedule iwiki code graph publication",
        "[Timer]",
        "OnCalendar=hourly",
        "Persistent=true",
        "Unit=iwiki-codegraph-publisher.service",
        "[Install]",
        "WantedBy=timers.target",
        "export IWIKI_DB_PASSWORD",
        "export IWIKI_EMBED_MODEL",
        "export IWIKI_EMBED_DIMENSIONS",
        "export IWIKI_CODE_GRAPH_MCP_URL",
        "export IWIKI_CODE_GRAPH_MCP_TOKEN",
        "iwiki-mcp code publish --project <checkout> --json",
    )
    for text in (english, russian):
        assert all(term in text for term in systemd_contract)

    def publisher_section(text: str, heading: str, next_heading: str) -> str:
        start = text.index(heading)
        end = text.index(next_heading, start)
        return text[start:end]

    english_publisher = publisher_section(
        english,
        "### Scheduled publisher operation",
        "### SQLite snapshot profiles and commit uncertainty",
    )
    russian_publisher = publisher_section(
        russian,
        "### Плановая публикация оператором",
        "### Профили снапшота SQLite и неопределённость коммита",
    )
    architecture_publisher = publisher_section(
        architecture,
        "### Publication application and operator boundary",
        "### Shared ECMAScript core",
    )
    normalized_english_publisher = " ".join(english_publisher.split())
    normalized_russian_publisher = " ".join(russian_publisher.split())
    normalized_architecture_publisher = " ".join(architecture_publisher.split())

    def mode_table(text: str, heading: str) -> str:
        start = text.index(heading)
        end = text.index("\n\n`wiki_code_index`", start)
        return " ".join(text[start:end].split())

    english_mode_table = mode_table(english, "| Mode | Publishes to | Requires |")
    russian_mode_table = mode_table(russian, "| Режим | Публикует в | Требует |")
    assert all(
        term in english_mode_table
        for term in (
            "`sqlite`",
            "no mode-specific publication environment variables",
            "`postgres`",
            "`[storage]`",
            "`IWIKI_DB_PASSWORD`",
            "`IWIKI_EMBED_MODEL`",
            "`IWIKI_EMBED_DIMENSIONS`",
            "`mcp`",
            "authenticated Streamable HTTP endpoint on same machine or remote",
            "`IWIKI_CODE_GRAPH_MCP_URL`",
            "`IWIKI_CODE_GRAPH_MCP_TOKEN`",
        )
    )
    assert all(
        term in russian_mode_table
        for term in (
            "`sqlite`",
            "нет mode-specific publication environment variables",
            "`postgres`",
            "`[storage]`",
            "`IWIKI_DB_PASSWORD`",
            "`IWIKI_EMBED_MODEL`",
            "`IWIKI_EMBED_DIMENSIONS`",
            "`mcp`",
            "Authenticated Streamable HTTP endpoint на той же машине или удалённый",
            "`IWIKI_CODE_GRAPH_MCP_URL`",
            "`IWIKI_CODE_GRAPH_MCP_TOKEN`",
        )
    )
    assert all(
        term in normalized_english_publisher
        for term in (
            "`sqlite` publishes to",
            "configured Git Wiki base",
            "`<wiki-base>/.iwiki/code-<domain>.sqlite3`",
            "Only the PostgreSQL source cache remains local at "
            "`<project>/.iwiki/code-<domain>.sqlite3`",
            "`postgres` uses existing publisher abstraction",
            "`mcp` uses same publication protocol",
            "local or remote Streamable HTTP endpoint",
            "Text and `--json` choose only output format",
            "Either format exits `0`",
            "/etc/systemd/system/iwiki-codegraph-publisher.service",
            "/etc/systemd/system/iwiki-codegraph-publisher.timer",
            "root-owned mode `0600`",
            "access to the checkout",
            "Mode-specific EnvironmentFile contents: `postgres` uses",
            "`mcp` uses `IWIKI_CODE_GRAPH_MCP_URL` and `IWIKI_CODE_GRAPH_MCP_TOKEN`",
            "`sqlite` needs no mode-specific publication variables",
        )
    )
    assert all(
        term in normalized_russian_publisher
        for term in (
            "`sqlite` публикует в",
            "настроенным Git Wiki base",
            "`<wiki-base>/.iwiki/code-<domain>.sqlite3`",
            "Только PostgreSQL source cache остаётся локальным по пути "
            "`<project>/.iwiki/code-<domain>.sqlite3`",
            "`postgres` использует существующую publisher abstraction",
            "`mcp` использует тот же publication protocol",
            "local или remote Streamable HTTP endpoint",
            "Text и `--json` выбирают только output format",
            "Оба формата завершаются с `0`",
            "/etc/systemd/system/iwiki-codegraph-publisher.service",
            "/etc/systemd/system/iwiki-codegraph-publisher.timer",
            "root-owned mode `0600`",
            "доступ к checkout",
            "Mode-specific EnvironmentFile contents: `postgres` использует",
            "`mcp` использует `IWIKI_CODE_GRAPH_MCP_URL` и `IWIKI_CODE_GRAPH_MCP_TOKEN`",
            "`sqlite` не требует mode-specific publication variables",
        )
    )
    assert all(
        term in normalized_architecture_publisher
        for term in (
            "`sqlite`",
            "The SQLite target/cache instead remains under the configured Git Wiki "
            "base at `<wiki-base>/.iwiki/code-<domain>.sqlite3`",
            "direct `postgres`",
            "`mcp` target",
            "PostgreSQL uses the publisher abstraction",
            "MCP publication uses a local or remote Streamable HTTP endpoint",
            "Text and compact `--json` are output formats",
            "either format exits `0`",
        )
    )
    for section in (
        normalized_english_publisher,
        normalized_russian_publisher,
        normalized_architecture_publisher,
    ):
        assert "local stdio" not in section.lower()
    assert "never stdio" in normalized_english_publisher.lower()
    assert "никогда не stdio" in normalized_russian_publisher.lower()
    assert "never stdio" in normalized_architecture_publisher.lower()

    tracked_artifacts = frozenset(
        path
        for path in (
            Path(value)
            for value in subprocess.check_output(["git", "ls-files"], text=True).splitlines()
        )
        if path.suffix in {".service", ".timer"}
        or path.parts[:2] == (".github", "workflows")
    )
    # Baseline from f1e5eb0; keep known pre-existing deployment files explicit.
    allowed_preexisting_artifacts = frozenset()
    assert tracked_artifacts == allowed_preexisting_artifacts
