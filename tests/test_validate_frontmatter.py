from iwiki_mcp.engine.validate import validate_page


def _types(findings):
    return {f["type"] for f in findings}


def test_frontmatter_does_not_trigger_pre_h2_text():
    page = "---\ntype: api\n---\n# T\n\n## Overview\ns\n\n## B\nbody\n"
    assert "pre_h2_text" not in _types(validate_page(page))


def test_missing_type_and_description_are_advisory():
    page = "---\ntitle: X\n---\n# T\n\n## B\nbody without overview\n"
    findings = validate_page(page)
    types = _types(findings)
    assert "missing_type" in types
    assert "missing_description" in types
    assert all(f["severity"] == "advisory"
               for f in findings if f["type"] in {"missing_type", "missing_description"})


def test_unknown_type_flagged_advisory():
    page = "---\ntype: bogus\n---\n# T\n\n## Overview\ns\n\n## B\nbody\n"
    findings = validate_page(page)
    assert "unknown_type" in _types(findings)


def test_valid_typed_page_has_no_frontmatter_findings():
    page = "---\ntype: api\ndescription: d\n---\n# T\n\n## Overview\ns\n\n## B\nbody\n"
    types = _types(validate_page(page))
    assert not ({"missing_type", "unknown_type", "missing_description"} & types)
