from iwiki_mcp.resources import AUTHORING_RULES


def test_authoring_rules_cover_section_format():
    text = AUTHORING_RULES.lower()
    assert "## overview" in text
    assert "](slug.md#heading)" in AUTHORING_RULES
    assert "[[" not in AUTHORING_RULES
    assert "##" in AUTHORING_RULES
