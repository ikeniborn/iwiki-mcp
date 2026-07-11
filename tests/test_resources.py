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
