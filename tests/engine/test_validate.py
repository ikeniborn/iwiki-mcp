from iwiki_mcp.engine.validate import validate_page


def _types(content):
    return {f["type"] for f in validate_page(content)}


CLEAN = (
    "# T\n\n## Overview\nsummary of all sections.\n\n"
    "## A\nlead alpha.\n\n## B\nlead beta.\n"
)


def test_clean_page_has_no_findings():
    assert validate_page(CLEAN) == []


def test_deep_heading_is_blocking():
    fs = [f for f in validate_page("## Overview\ns.\n\n## A\nx.\n\n### too deep\n")
          if f["type"] == "deep_heading"]
    assert fs and fs[0]["severity"] == "block"


def test_pre_h2_text_is_blocking():
    fs = [f for f in validate_page("# T\n\nstray prose\n\n## Overview\ns.\n\n## A\nx.\n")
          if f["type"] == "pre_h2_text"]
    assert fs and fs[0]["severity"] == "block"


def test_single_h1_before_h2_is_allowed():
    assert "pre_h2_text" not in _types(CLEAN)


def test_no_missing_overview_finding():
    assert "missing_overview" not in _types("# T\n\n## A\nlead.\n")


def test_reserved_sections_exempt_from_missing_lead():
    page = ("# T\n\n## Role\nlead here.\n\n## Outgoing links\n- [x](y.md)\n\n"
            "## External links\n- https://example.com\n")
    fs = [f for f in validate_page(page) if f["type"] == "missing_lead"]
    assert fs == []          # link lists are not prose — no lead expected


def test_unknown_status_advisory():
    page = "---\ntype: person\ndescription: d\nstatus: bogus\n---\n# T\n\n## A\nlead.\n"
    fs = [f for f in validate_page(page) if f["type"] == "unknown_status"]
    assert fs and fs[0]["severity"] == "advisory"


def test_known_status_no_finding():
    page = "---\ntype: person\ndescription: d\nstatus: stable\n---\n# T\n\n## A\nlead.\n"
    assert "unknown_status" not in _types(page)


def test_status_as_list_does_not_crash():
    # `status: [a, b]` parses to a list; the isinstance guard skips it, no crash.
    page = "---\ntype: person\ndescription: d\nstatus: [a, b]\n---\n# T\n\n## A\nlead.\n"
    assert "unknown_status" not in _types(page)


def test_missing_lead_is_advisory():
    fs = [f for f in validate_page("## Overview\ns.\n\n## A\n\n## B\nlead.\n")
          if f["type"] == "missing_lead"]
    assert fs and fs[0]["severity"] == "advisory"


def test_long_lead_is_advisory():
    long = "x " * 200  # > 250 chars
    fs = [f for f in validate_page(f"## Overview\ns.\n\n## A\n{long}\n")
          if f["type"] == "long_lead"]
    assert fs and fs[0]["severity"] == "advisory"


_SCENARIO_FENCE = (
    "```iwiki-gwt\n"
    'id = "confirm-account-opening"\n'
    'title = "Confirm account opening"\n'
    "given = []\n"
    'when = { role = "command", name = "ConfirmAccountOpening" }\n'
    'then = [{ role = "event", name = "AccountOpened" }]\n'
    "code = [\n"
    '  { relation = "implements", symbol = "accounts.Account.confirm" },\n'
    '  { relation = "verifies", file = "tests/test_account.py" },\n'
    "]\n"
    "```\n"
)


def _fenced_page(page_type):
    declared = "" if page_type is None else f"type: {page_type}\n"
    return (
        f"---\n{declared}description: d\nstatus: stable\n---\n"
        "# T\n\n## Overview\nsummary.\n\n## Scenario\nlead.\n\n"
        f"{_SCENARIO_FENCE}"
    )


def test_scenario_fence_outside_a_specification_page_is_advisory():
    fs = [f for f in validate_page(_fenced_page("concept"))
          if f["type"] == "unprojected_scenario"]
    assert fs and fs[0]["severity"] == "advisory"


def test_scenario_fence_on_a_specification_page_has_no_finding():
    assert "unprojected_scenario" not in _types(_fenced_page("specification"))


def test_page_without_a_scenario_fence_has_no_finding():
    assert "unprojected_scenario" not in _types(CLEAN)


def test_scenario_fence_without_frontmatter_is_advisory():
    body = "# T\n\n## Overview\nsummary.\n\n## Scenario\nlead.\n\n" + _SCENARIO_FENCE
    assert "unprojected_scenario" in _types(body)


def test_tilde_scenario_fence_is_detected():
    page = _fenced_page("concept").replace("```", "~~~")
    assert "unprojected_scenario" in _types(page)
