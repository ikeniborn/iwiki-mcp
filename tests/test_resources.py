from pathlib import Path
import re

from iwiki_mcp.resources import AUTHORING_RULES


def test_authoring_rules_cover_section_format():
    text = AUTHORING_RULES.lower()
    assert "description" in text          # description is the authored summary
    assert "](<type>/<slug>.md#heading)" in AUTHORING_RULES
    assert "[[" not in AUTHORING_RULES
    assert "##" in AUTHORING_RULES


def test_description_is_separate_summary_vector_not_prefix():
    # The stale two-level lie must be gone.
    assert "context prefix" not in AUTHORING_RULES
    # The corrected model must be stated.
    assert "summary-level vector" in AUTHORING_RULES


def test_links_use_type_slug_path_and_export_only_artifacts():
    assert "(<type>/<slug>.md#heading)" in AUTHORING_RULES
    assert "export-only" in AUTHORING_RULES


def test_authoring_rules_describe_current_search_and_update_tools():
    assert "hybrid`, `lexical`, and `semantic" in AUTHORING_RULES
    assert "IWIKI_SEARCH_MODE" in AUTHORING_RULES
    assert "IWIKI_RERANK_MODEL" in AUTHORING_RULES
    assert "wiki_update_page" in AUTHORING_RULES
    assert "wiki_remediation_plan" in AUTHORING_RULES


def test_agent_snippets_use_supported_existing_page_update_path():
    root = Path(__file__).parents[1]
    for relative in ("templates/AGENTS.md.snippet", "templates/CLAUDE.md.snippet"):
        text = (root / relative).read_text(encoding="utf-8")
        assert "wiki_update_page" in text
        assert "Do not imply the tool can update existing pages directly" not in text


def test_public_readmes_describe_description_as_a_separate_summary_vector():
    root = Path(__file__).parents[1]
    english = (root / "README.md").read_text(encoding="utf-8")
    russian = (root / "docs/README.ru.md").read_text(encoding="utf-8")

    assert "embedded as each section's context prefix" not in english
    assert "stored as a separate summary vector" in english
    assert "встраивается как контекстный префикс каждой секции" not in russian
    assert "хранится как отдельный summary-вектор" in russian


def test_repository_server_report_lists_current_search_modes_and_tool_surface():
    root = Path(__file__).parents[1]
    report = (root / "docs/reports/iwiki-mcp-server-report.html").read_text(
        encoding="utf-8"
    )
    tool_rows = set(re.findall(r"<tr><td><code>(wiki_[a-z_]+)</code>", report))

    assert "Hybrid / vector / lexical" not in report
    assert "hybrid / lexical / semantic" in report
    assert tool_rows == {
        "wiki_status", "wiki_list_domains", "wiki_list_pages", "wiki_read_page",
        "wiki_search", "wiki_related", "wiki_write_page", "wiki_update_page",
        "wiki_delete_page", "wiki_index", "wiki_create_domain", "wiki_bind",
        "wiki_lint", "wiki_remediation_plan", "wiki_migrate_okf", "wiki_apply_okf",
        "wiki_export_okf", "wiki_sync",
    }
