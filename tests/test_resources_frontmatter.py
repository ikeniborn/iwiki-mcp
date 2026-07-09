from iwiki_mcp.resources import AUTHORING_RULES


def test_authoring_rules_mention_frontmatter_and_types():
    assert "frontmatter" in AUTHORING_RULES.lower()
    for t in ("architecture", "api", "guide", "reference", "runbook", "concept"):
        assert t in AUTHORING_RULES
