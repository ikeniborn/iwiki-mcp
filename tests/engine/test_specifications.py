from dataclasses import FrozenInstanceError
import hashlib
import json

import pytest

from iwiki_mcp.engine.specifications import (
    PhaseItem,
    Scenario,
    SpecificationBinding,
    parse_specification_page,
)


AGGREGATE_BLOCK = '''id = "confirm-account-opening"
title = "Confirm account opening"
given = [{ role = "event", name = "AccountOpeningRequested" }]
when = { role = "command", name = "ConfirmAccountOpening" }
then = [{ role = "event", name = "AccountOpened" }]
code = [
  { relation = "implements", phase = "when", symbol = "accounts.Account.confirm" },
  { relation = "verifies", file = "tests/accounts/test_opening.py" },
]
'''


def _page(block: str, *, page_type: str = "specification", heading: str = "Scenario") -> str:
    return (
        f"---\ntype: {page_type}\ntitle: Behavior\n---\n"
        f"# Behavior\n\n## {heading}\n\n"
        f"```iwiki-gwt\n{block}```\n"
    )


def _parse(block: str, *, page_type: str = "specification"):
    return parse_specification_page(
        domain="payments",
        slug="specification/open-account",
        markdown=_page(block, page_type=page_type),
    )


def test_parses_event_sourced_aggregate_into_exact_immutable_model():
    result = _parse(AGGREGATE_BLOCK, page_type=" Specification ")

    assert result.findings == ()
    assert len(result.scenarios) == 1
    scenario = result.scenarios[0]
    assert isinstance(scenario, Scenario)
    assert scenario.identity == "payments#confirm-account-opening"
    assert scenario.title == "Confirm account opening"
    assert scenario.slug == "specification/open-account"
    assert scenario.heading == "Scenario"
    assert scenario.anchor == "scenario"
    assert scenario.items == (
        PhaseItem("given", "event", "AccountOpeningRequested"),
        PhaseItem("when", "command", "ConfirmAccountOpening"),
        PhaseItem("then", "event", "AccountOpened"),
    )
    assert tuple((item.relation, item.phase, item.selector_kind, item.selector)
                 for item in scenario.bindings) == (
        ("implements", "when", "symbol", "accounts.Account.confirm"),
        ("verifies", None, "file", "tests/accounts/test_opening.py"),
    )
    with pytest.raises(FrozenInstanceError):
        scenario.title = "Changed"


def test_parses_request_response_and_event_roles_with_all_selector_kinds():
    block = '''id = "accept-payment"
title = "Accept payment"
given = [{ role = "fact", name = "Customer is active" }, { role = "state", name = "Balance is positive" }]
when = { role = "request", name = "POST /payments" }
then = [{ role = "response", name = "202 Accepted" }, { role = "event", name = "PaymentAccepted" }]
code = [
  { relation = "implements", source_glob = "src/payments/**" },
  { relation = "verifies", symbol = "tests.payments.test_accept_payment" },
]
'''
    scenario = _parse(block).scenarios[0]

    assert tuple((item.phase, item.role) for item in scenario.items) == (
        ("given", "fact"),
        ("given", "state"),
        ("when", "request"),
        ("then", "response"),
        ("then", "event"),
    )
    assert {binding.selector_kind for binding in scenario.bindings} == {
        "source_glob", "symbol",
    }


@pytest.mark.parametrize(
    ("given", "when_role", "then_role", "then_name"),
    [
        ("[]", "action", "outcome", "Payment recorded"),
        ('[{ role = "event", name = "PaymentRequested" }]',
         "command", "exception", "PaymentRejected"),
    ],
)
def test_accepts_empty_given_action_outcome_and_sole_exception(
    given, when_role, then_role, then_name,
):
    block = f'''id = "process-payment"
title = "Process payment"
given = {given}
when = {{ role = "{when_role}", name = "ProcessPayment" }}
then = [{{ role = "{then_role}", name = "{then_name}" }}]
code = [
  {{ relation = "implements", symbol = "payments.process" }},
  {{ relation = "verifies", symbol = "tests.payments.test_process" }},
]
'''

    result = _parse(block)

    assert result.findings == ()
    expected_given = (
        (("given", "event", "PaymentRequested"),)
        if given != "[]" else ()
    )
    assert tuple((item.phase, item.role, item.name)
                 for item in result.scenarios[0].items) == (
        *expected_given,
        ("when", when_role, "ProcessPayment"),
        ("then", then_role, then_name),
    )


def test_accepts_exact_legal_scalar_and_selector_bounds():
    scenario_id = "a" * 128
    title = "界" * 250
    name = "é" * 512
    selector = "s" * 4096
    block = f'''id = "{scenario_id}"
title = "{title}"
given = [{{ role = "fact", name = "{name}" }}]
when = {{ role = "action", name = "Act" }}
then = [{{ role = "outcome", name = "Done" }}]
code = [
  {{ relation = "implements", symbol = "{selector}" }},
  {{ relation = "verifies", file = "tests/test_bounds.py" }},
]
'''

    result = _parse(block)

    assert result.findings == ()
    scenario = result.scenarios[0]
    assert len(scenario.scenario_id.encode("utf-8")) == 128
    assert len(scenario.title) == 250
    assert len(scenario.items[0].name.encode("utf-8")) == 1024
    assert len(scenario.bindings[0].selector.encode("utf-8")) == 4096


def test_identity_binding_ids_and_source_hash_survive_page_and_section_moves():
    first = parse_specification_page("payments", "specification/one", _page(
        AGGREGATE_BLOCK, heading="First location"
    )).scenarios[0]
    second = parse_specification_page("payments", "specification/two", _page(
        AGGREGATE_BLOCK, heading="Second location"
    )).scenarios[0]

    assert first.identity == second.identity == "payments#confirm-account-opening"
    assert first.source_hash == second.source_hash == hashlib.sha256(
        AGGREGATE_BLOCK.encode("utf-8")
    ).hexdigest()
    assert tuple(item.binding_id for item in first.bindings) == tuple(
        item.binding_id for item in second.bindings
    )
    expected = hashlib.sha256("\0".join((
        "payments", "confirm-account-opening", "implements", "when",
        "symbol", "accounts.Account.confirm",
    )).encode("utf-8")).hexdigest()
    assert first.bindings[0].binding_id == f"spec:binding:{expected}"


@pytest.mark.parametrize(
    ("fragment", "replacement"),
    [
        ('when = { role = "command", name = "ConfirmAccountOpening" }',
         'when = { role = "event", name = "ConfirmAccountOpening" }'),
        ('given = [{ role = "event", name = "AccountOpeningRequested" }]',
         'given = [{ role = "command", name = "AccountOpeningRequested" }]'),
        ('then = [{ role = "event", name = "AccountOpened" }]',
         'then = [{ role = "command", name = "AccountOpened" }]'),
    ],
)
def test_rejects_roles_outside_the_exact_phase_vocabulary(fragment, replacement):
    result = _parse(AGGREGATE_BLOCK.replace(fragment, replacement))

    assert result.scenarios == ()
    assert [finding["type"] for finding in result.findings] == ["invalid_scenario"]


def test_exception_then_item_is_exclusive():
    block = AGGREGATE_BLOCK.replace(
        'then = [{ role = "event", name = "AccountOpened" }]',
        'then = [{ role = "exception", name = "AccountRejected" }, { role = "event", name = "AuditRecorded" }]',
    )

    result = _parse(block)

    assert result.scenarios == ()
    assert result.findings[0]["reason"] == "exception_not_exclusive"


@pytest.mark.parametrize(
    "block",
    [
        AGGREGATE_BLOCK + 'unknown = "value"\n',
        AGGREGATE_BLOCK.replace(
            '{ role = "event", name = "AccountOpeningRequested" }',
            '{ role = "event", name = "AccountOpeningRequested", extra = "x" }',
        ),
        AGGREGATE_BLOCK.replace(
            '{ relation = "implements", phase = "when", symbol = "accounts.Account.confirm" }',
            '{ relation = "implements", phase = "when", symbol = "accounts.Account.confirm", extra = "x" }',
        ),
        AGGREGATE_BLOCK.replace(
            'title = "Confirm account opening"',
            'id = "duplicate"\ntitle = "Confirm account opening"',
        ),
        AGGREGATE_BLOCK.replace(
            'when = { role = "command", name = "ConfirmAccountOpening" }',
            'when = { role = "command", name = ',
        ),
    ],
)
def test_rejects_unknown_duplicate_and_malformed_toml(block):
    result = _parse(block)

    assert result.scenarios == ()
    assert [finding["type"] for finding in result.findings] == ["invalid_scenario"]
    serialized = json.dumps(result.findings, sort_keys=True)
    assert "AccountOpeningRequested" not in serialized


@pytest.mark.parametrize(
    ("fragment", "replacement"),
    [
        ('id = "confirm-account-opening"', 'id = "Confirm-Account"'),
        ('id = "confirm-account-opening"', f'id = "{"a" * 129}"'),
        ('title = "Confirm account opening"', 'title = "   "'),
        ('title = "Confirm account opening"', 'title = "bad\\u0000title"'),
        ('title = "Confirm account opening"', f'title = "{"x" * 251}"'),
        ('name = "AccountOpeningRequested"', 'name = "   "'),
        ('name = "AccountOpeningRequested"', 'name = "bad\\u0000name"'),
        ('name = "AccountOpeningRequested"', f'name = "{"é" * 513}"'),
        ('symbol = "accounts.Account.confirm"', f'symbol = "{"s" * 4097}"'),
    ],
)
def test_enforces_scalar_bounds(fragment, replacement):
    result = _parse(AGGREGATE_BLOCK.replace(fragment, replacement, 1))

    assert result.scenarios == ()
    assert result.findings[0]["type"] == "invalid_scenario"


@pytest.mark.parametrize(
    "block",
    [
        AGGREGATE_BLOCK.replace(
            'given = [{ role = "event", name = "AccountOpeningRequested" }]',
            'given = [{ role = "event", name = "AccountOpeningRequested" }, { role = "event", name = "AccountOpeningRequested" }]',
        ),
        AGGREGATE_BLOCK.replace(
            '  { relation = "verifies", file = "tests/accounts/test_opening.py" },',
            '  { relation = "implements", phase = "when", symbol = "accounts.Account.confirm" },\n  { relation = "verifies", file = "tests/accounts/test_opening.py" },',
        ),
    ],
)
def test_rejects_duplicate_phase_items_and_bindings(block):
    result = _parse(block)

    assert result.scenarios == ()
    assert result.findings[0]["type"] == "invalid_scenario"


@pytest.mark.parametrize(
    "selector",
    [
        'file = "../secrets.py"',
        'file = "/absolute.py"',
        'file = "src\\\\module.py"',
        'source_glob = "src/../**"',
        'symbol = "bad\\u0000symbol"',
    ],
)
def test_reuses_safe_selector_rules(selector):
    block = AGGREGATE_BLOCK.replace(
        'symbol = "accounts.Account.confirm"', selector,
    )

    result = _parse(block)

    assert result.scenarios == ()
    assert result.findings[0]["type"] == "invalid_scenario"


def test_reports_multiple_scenario_fences_in_one_h2():
    markdown = _page(AGGREGATE_BLOCK).replace(
        "```iwiki-gwt\n" + AGGREGATE_BLOCK + "```",
        "```iwiki-gwt\n" + AGGREGATE_BLOCK + "```\n\n"
        "```iwiki-gwt\n" + AGGREGATE_BLOCK.replace(
            "confirm-account-opening", "confirm-account-opening-again"
        ) + "```",
    )

    result = parse_specification_page("payments", "specification/two", markdown)

    assert result.scenarios == ()
    assert result.findings == ({
        "type": "invalid_scenario",
        "slug": "specification/two",
        "heading": "Scenario",
        "reason": "multiple_blocks_in_section",
    },)


def test_reports_block_outside_h2_and_missing_block_deterministically():
    outside = (
        "---\ntype: specification\n---\n# Behavior\n\n"
        f"```iwiki-gwt\n{AGGREGATE_BLOCK}```\n"
    )

    invalid = parse_specification_page("payments", "outside", outside)
    missing = parse_specification_page(
        "payments", "empty", "---\ntype: specification\n---\n# Empty\n\n## Notes\nText.\n"
    )

    assert invalid.findings[0]["reason"] == "block_outside_h2"
    assert missing.findings == ({"type": "missing_scenario", "slug": "empty"},)


@pytest.mark.parametrize(
    ("code_frontmatter", "type_after_code"),
    [
        ("code:\n  files:\n    - src/a.py\ncode:\n  files:\n    - src/b.py", False),
        ("code:\n  files:\n    malformed-selector-line", False),
        ("code:\n  files:\n    - src/a.py\ncode:\n  files:\n    - src/b.py", True),
    ],
)
def test_explicit_specification_rejects_ambiguous_code_frontmatter(
    code_frontmatter, type_after_code,
):
    markdown = _page(AGGREGATE_BLOCK).replace(
        "title: Behavior\n---",
        f"title: Behavior\n{code_frontmatter}\n---",
    )
    if type_after_code:
        markdown = markdown.replace("type: specification\n", "").replace(
            f"{code_frontmatter}\n---",
            f"{code_frontmatter}\ntype: specification\n---",
        )

    result = parse_specification_page("payments", "specification/frontmatter", markdown)

    assert result.scenarios == ()
    assert result.findings == ({
        "type": "invalid_scenario",
        "slug": "specification/frontmatter",
        "reason": "invalid_frontmatter",
    },)
    assert "src/" not in json.dumps(result.findings, sort_keys=True)


def test_lone_surrogate_in_toml_comment_returns_sanitized_invalid_finding():
    result = _parse(AGGREGATE_BLOCK + "# \ud800\n")

    assert result.scenarios == ()
    assert result.findings == ({
        "type": "invalid_scenario",
        "slug": "specification/open-account",
        "heading": "Scenario",
        "reason": "invalid_block_encoding",
    },)


@pytest.mark.parametrize("relation_to_remove", ["implements", "verifies"])
def test_returns_incomplete_scenario_with_sanitized_finding(relation_to_remove):
    lines = [line for line in AGGREGATE_BLOCK.splitlines(keepends=True)
             if f'relation = "{relation_to_remove}"' not in line]

    result = _parse("".join(lines))

    assert len(result.scenarios) == 1
    assert result.findings == ({
        "type": "incomplete_bindings",
        "slug": "specification/open-account",
        "heading": "Scenario",
        "scenario_id": "confirm-account-opening",
        "missing": (relation_to_remove,),
    },)


@pytest.mark.parametrize(
    "frontmatter_text",
    [
        "---\ntype: guide\n---\n",
        "",
        "---\ntype: [specification]\n---\n",
    ],
)
@pytest.mark.parametrize("closing_fence", ["```\n", ""])
def test_non_specification_pages_bypass_malformed_and_unclosed_gwt_fences(
    frontmatter_text, closing_fence,
):
    markdown = (
        frontmatter_text
        + "# Guide\n\n## Example\n"
        + "```iwiki-gwt\nid = \"duplicate\"\nid = \"duplicate\"\n"
        + closing_fence
    )

    result = parse_specification_page("payments", "guide", markdown)

    assert result.scenarios == ()
    assert result.findings == ()


def test_non_string_frontmatter_type_is_not_an_explicit_specification():
    markdown = _page(AGGREGATE_BLOCK).replace(
        "type: specification", "type: [specification]",
    )

    result = parse_specification_page("payments", "guide", markdown)

    assert result.scenarios == ()
    assert result.findings == ()


def test_public_records_are_frozen():
    phase = PhaseItem("given", "event", "Opened")
    binding = SpecificationBinding(
        "spec:binding:id", "implements", None, "symbol", "pkg.Type.method"
    )

    with pytest.raises(FrozenInstanceError):
        phase.name = "Changed"
    with pytest.raises(FrozenInstanceError):
        binding.selector = "other"
