from pathlib import Path

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
