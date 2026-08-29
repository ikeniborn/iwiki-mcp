from __future__ import annotations

import iwiki_mcp.specifications as specification_service
from dataclasses import replace
from iwiki_mcp.specification_store import (
    ResolutionAttempt,
    decode_jsonl,
    encode_jsonl,
)
from iwiki_mcp.specifications import (
    PageSnapshot,
    assemble_projection,
    context_freshness,
    evidence_freshness,
    graph_state_fingerprint,
    projection_context,
    search_projections,
)


def _page(slug: str, scenario_id: str, *, title: str = "Open account",
          implements: bool = True, verifies: bool = True,
          revision: str = "page-r1") -> PageSnapshot:
    bindings = []
    if implements:
        bindings.append(
            '{ relation = "implements", phase = "when", symbol = "Account.open" }'
        )
    if verifies:
        bindings.append(
            '{ relation = "verifies", symbol = "tests.test_open_account" }'
        )
    code = ",\n  ".join(bindings)
    markdown = f'''---
type: specification
title: Account behavior
---
# Account behavior

## Scenario heading

```iwiki-gwt
id = "{scenario_id}"
title = "{title}"
given = [{{ role = "state", name = "Account is pending" }}]
when = {{ role = "command", name = "OpenAccount" }}
then = [{{ role = "event", name = "AccountOpened" }}]
code = [
  {code}
]
```
'''
    return PageSnapshot(slug=slug, markdown=markdown, revision=revision)


def test_assembly_is_domain_deterministic_and_excludes_incomplete_scenarios():
    pages = (
        _page("zeta", "zeta-flow"),
        _page("incomplete", "needs-test", verifies=False),
        _page("alpha", "alpha-flow"),
    )

    projection = assemble_projection("payments", pages, markdown_revision="wiki-r1")

    assert [scenario.scenario_id for scenario in projection.scenarios] == [
        "alpha-flow",
        "zeta-flow",
    ]
    assert projection.scenario_count == 2
    assert projection.binding_count == 4
    assert projection.markdown_revision == "wiki-r1"
    actual_findings = [
        (finding.type, finding.scenario_id, finding.missing)
        for finding in projection.findings
    ]
    assert actual_findings == [
        ("incomplete_bindings", "needs-test", ("verifies",)),
    ]


def test_assembly_and_codec_preserve_page_slug_characters_and_empty_anchor():
    page = PageSnapshot(
        slug="specification/foo[bar]*?",
        revision="page-r1",
        markdown='''---
type: specification
---
# Account

## !!!

```iwiki-gwt
id = "punctuation-heading"
title = "Punctuation heading"
given = []
when = { role = "command", name = "OpenAccount" }
then = [{ role = "event", name = "AccountOpened" }]
code = [
  { relation = "implements", symbol = "Account.open" },
  { relation = "verifies", file = "tests/test_account.py" }
]
```
''',
    )

    projection = assemble_projection("payments", (page,))
    scenario = projection.scenarios[0]

    assert scenario.page_slug == "specification/foo[bar]*?"
    assert scenario.heading == "!!!"
    assert scenario.anchor == ""
    assert decode_jsonl(encode_jsonl(projection)) == projection


def test_domain_duplicates_exclude_every_instance_and_report_all_locations():
    projection = assemble_projection(
        "payments",
        (_page("z-page", "same-id"), _page("a-page", "same-id")),
    )

    assert projection.scenarios == ()
    assert projection.bindings == ()
    assert len(projection.findings) == 1
    finding = projection.findings[0]
    assert finding.type == "duplicate_scenario_id"
    assert finding.scenario_id == "same-id"
    assert [(location.slug, location.heading) for location in finding.locations] == [
        ("a-page", "Scenario heading"),
        ("z-page", "Scenario heading"),
    ]


def test_invalid_authored_page_is_preserved_as_deterministic_finding():
    invalid = PageSnapshot(
        slug="broken",
        revision=7,
        markdown="""---
type: specification
---
# Broken

## Broken scenario

```iwiki-gwt
not valid toml
```
""",
    )

    projection = assemble_projection("payments", (invalid,))

    assert projection.scenario_count == 0
    assert [(finding.type, finding.slug, finding.reason) for finding in projection.findings] == [
        ("invalid_scenario", "broken", "malformed_toml"),
    ]


def test_assembly_calls_engine_parser_only_for_explicit_specification_pages(
    monkeypatch,
):
    ordinary = PageSnapshot(
        slug="guide",
        revision="r1",
        markdown="""---
type: guide
---
# Guide

```iwiki-gwt
not a specification
```
""",
    )
    calls = []
    real_parse = specification_service.parse_specification_page

    def observe_parse(domain, slug, markdown):
        calls.append(slug)
        return real_parse(domain, slug, markdown)

    monkeypatch.setattr(
        specification_service, "parse_specification_page", observe_parse
    )

    projection = assemble_projection(
        "payments", (ordinary, _page("spec", "valid-scenario"))
    )

    assert calls == ["spec"]
    assert projection.scenario_count == 1


def test_stable_move_preserves_evidence_only_for_unchanged_source_and_binding():
    original = assemble_projection("payments", (_page("old", "open-account"),))
    binding = original.bindings[0]
    evidence = ResolutionAttempt(
        binding_id=binding.binding_id,
        domain="payments",
        scenario_id="open-account",
        state="resolved",
        targets=("symbol:Account.open",),
        unresolved_reference=None,
        graph_revision="graph-r1",
        graph_state_fingerprint=graph_state_fingerprint({
            "state": "ready", "reason": None, "revision": "graph-r1",
        }),
        specification_source_hash=original.scenarios[0].source_hash,
        checked_at="2026-08-29T10:00:00Z",
        reason=None,
    )

    moved = assemble_projection(
        "payments",
        (_page("new", "open-account", revision="page-r2"),),
        (evidence,),
    )
    changed = assemble_projection(
        "payments",
        (_page("new", "open-account", title="Open account now"),),
        (evidence,),
    )

    assert moved.bindings[0].binding_id == binding.binding_id
    assert moved.evidence == (evidence,)
    assert changed.evidence == ()


def test_projection_search_is_graph_free_and_ranks_semantic_fields():
    projection = assemble_projection(
        "payments",
        (
            _page("selector", "other", title="Other"),
            _page("title", "title-hit", title="OpenAccount"),
            _page("id", "openaccount", title="Last alphabetically"),
        ),
    )

    results = search_projections((projection,), "OpenAccount", 10)

    assert [scenario.scenario_id for scenario in results] == [
        "openaccount",
        "title-hit",
        "other",
    ]


def test_search_ranks_title_before_partial_scenario_id_match():
    projection = assemble_projection(
        "payments",
        (
            _page("partial", "account-partial", title="Unrelated"),
            _page("title", "title-wins", title="Account"),
        ),
    )

    results = search_projections((projection,), "account", 10)

    assert [scenario.scenario_id for scenario in results] == [
        "title-wins",
        "account-partial",
    ]


def test_context_returns_complete_persisted_semantics_and_freshness_without_graph():
    projection = assemble_projection("payments", (_page("account", "open-account"),))
    scenario = projection.scenarios[0]
    binding = projection.bindings[0]
    evidence = ResolutionAttempt(
        binding_id=binding.binding_id,
        domain="payments",
        scenario_id=scenario.scenario_id,
        state="graph_unavailable",
        targets=(),
        unresolved_reference=binding.selector,
        graph_revision=None,
        graph_state_fingerprint=graph_state_fingerprint({
            "state": "not_primary", "reason": "not_primary", "revision": None,
        }),
        specification_source_hash=scenario.source_hash,
        checked_at="2026-08-29T10:00:00Z",
        reason="not_primary",
    )
    with_evidence = projection.with_evidence((evidence,))

    context = projection_context(with_evidence, "open-account")

    assert context is not None
    assert context.scenario == scenario
    assert context.bindings == tuple(
        item for item in projection.bindings if item.scenario_id == "open-account"
    )
    assert context.evidence == (evidence,)
    unavailable_fingerprint = evidence.graph_state_fingerprint
    assert evidence_freshness(
        evidence,
        scenario.source_hash,
        None,
        unavailable_fingerprint,
        graph_ready=False,
    ) == "fresh"
    assert evidence_freshness(
        evidence, "changed", None, unavailable_fingerprint, graph_ready=False
    ) == "stale_spec"
    assert evidence_freshness(
        evidence, scenario.source_hash, "new", unavailable_fingerprint,
        graph_ready=False
    ) == "fresh"
    assert evidence_freshness(
        evidence,
        scenario.source_hash,
        None,
        graph_state_fingerprint({
            "state": "missing", "reason": "missing", "revision": None,
        }),
        graph_ready=False,
    ) == "stale_graph"
    assert evidence_freshness(
        evidence, scenario.source_hash, None, unavailable_fingerprint,
        graph_ready=True
    ) == "stale_graph"
    assert context_freshness(
        context,
        current_graph_revision=None,
        current_graph_state_fingerprint=unavailable_fingerprint,
        graph_ready=False,
    ) == tuple(
        (binding.binding_id, "fresh" if binding.binding_id == evidence.binding_id
         else "not_checked")
        for binding in context.bindings
    )


def test_ready_resolution_freshness_uses_readiness_and_revision_not_fingerprint():
    projection = assemble_projection("payments", (_page("account", "open-account"),))
    scenario = projection.scenarios[0]
    binding = projection.bindings[0]
    evidence = ResolutionAttempt(
        binding_id=binding.binding_id,
        domain="payments",
        scenario_id=scenario.scenario_id,
        state="resolved",
        targets=("symbol:Account.open",),
        unresolved_reference=None,
        graph_revision="graph-r1",
        graph_state_fingerprint=graph_state_fingerprint({
            "state": "ready", "reason": None, "revision": "graph-r1",
        }),
        specification_source_hash=scenario.source_hash,
        checked_at="2026-08-29T10:00:00Z",
        reason=None,
    )

    assert evidence_freshness(
        evidence,
        scenario.source_hash,
        "graph-r1",
        graph_state_fingerprint({
            "state": "ready", "reason": "different", "revision": "graph-r1",
        }),
        graph_ready=True,
    ) == "fresh"
    assert evidence_freshness(
        evidence,
        scenario.source_hash,
        "graph-r2",
        evidence.graph_state_fingerprint,
        graph_ready=True,
    ) == "stale_graph"
    assert evidence_freshness(
        evidence,
        scenario.source_hash,
        "graph-r1",
        evidence.graph_state_fingerprint,
        graph_ready=False,
    ) == "stale_graph"


def test_search_matches_phase_role_and_keeps_multi_domain_associations_isolated():
    payments = assemble_projection(
        "payments", (_page("pay", "same-id", title="Payments"),)
    )
    accounts = assemble_projection(
        "accounts", (_page("account", "same-id", title="Accounts"),)
    )
    accounts_binding = accounts.bindings[0]
    accounts = accounts.with_evidence((ResolutionAttempt(
        binding_id=accounts_binding.binding_id,
        domain="accounts",
        scenario_id="same-id",
        state="resolved",
        targets=("symbol:accounts",),
        unresolved_reference=None,
        graph_revision="g1",
        graph_state_fingerprint=graph_state_fingerprint({
            "state": "ready", "reason": None, "revision": "g1",
        }),
        specification_source_hash=accounts.scenarios[0].source_hash,
        checked_at="2026-08-29T10:00:00Z",
        reason=None,
    ),))

    assert [item.domain for item in search_projections(
        (payments, accounts), "command", 10
    )] == ["accounts", "payments"]
    assert [item.domain for item in search_projections(
        (payments, accounts), "when", 10
    )] == ["accounts", "payments"]
    assert projection_context(payments, "same-id").evidence == ()
    assert projection_context(accounts, "same-id").evidence == accounts.evidence


def test_evidence_retention_requires_exact_domain_scenario_binding_tuple():
    projection = assemble_projection("payments", (_page("pay", "open-account"),))
    scenario = projection.scenarios[0]
    binding = projection.bindings[0]
    base_attempt = ResolutionAttempt(
        binding_id=binding.binding_id,
        domain="payments",
        scenario_id="open-account",
        state="resolved",
        targets=("symbol:Account.open",),
        unresolved_reference=None,
        graph_revision="g1",
        graph_state_fingerprint=graph_state_fingerprint({
            "state": "ready", "reason": None, "revision": "g1",
        }),
        specification_source_hash=scenario.source_hash,
        checked_at="2026-08-29T10:00:00Z",
        reason=None,
    )

    rebuilt = assemble_projection(
        "payments",
        (_page("pay", "open-account"),),
        (
            base_attempt,
            base_attempt.__class__(
                **{**base_attempt.__dict__, "domain": "other"}
            ),
            base_attempt.__class__(
                **{**base_attempt.__dict__, "scenario_id": "other"}
            ),
        ),
    )

    assert rebuilt.evidence == (base_attempt,)


def test_graph_state_fingerprint_uses_only_normalized_public_fields():
    public = {
        "state": "failed",
        "reason": "source_unavailable",
        "revision": "graph-r1",
    }
    first = graph_state_fingerprint({
        **public,
        "error": "secret https://private.example/path",
        "detail": {"password": "secret"},
    })
    second = graph_state_fingerprint({
        **public,
        "error": "different raw exception",
        "private_path": "/another/private/path",
    })

    assert first == second
    assert first != graph_state_fingerprint({**public, "state": "dirty"})
    assert first != graph_state_fingerprint({**public, "reason": "missing"})
    assert first != graph_state_fingerprint({**public, "revision": "graph-r2"})


def test_graph_state_fingerprint_normalizes_unsafe_and_wrong_typed_fields():
    unsafe_one = graph_state_fingerprint({
        "state": ["failed", "/secret"],
        "reason": "https://private.example/error",
        "revision": {"path": "/secret"},
        "extra": object(),
    })
    unsafe_two = graph_state_fingerprint({
        "state": {"different": "raw"},
        "reason": "/another/private/path",
        "revision": ["not", "a", "revision"],
    })

    assert unsafe_one == unsafe_two
    assert unsafe_one.startswith("sha256:")
    assert "secret" not in unsafe_one
    assert "private" not in unsafe_one
    assert graph_state_fingerprint(None) == graph_state_fingerprint({})


def test_graph_state_fingerprint_allowlists_safe_shaped_state_and_reason_codes():
    revision = "graph-r1"
    fallback = graph_state_fingerprint({
        "state": "failed", "reason": "failed", "revision": revision,
    })

    assert graph_state_fingerprint({
        "state": "database_password",
        "reason": "database_password",
        "revision": revision,
    }) == fallback


def test_search_deduplicates_repeated_projections_and_uses_field_coverage():
    sparse = assemble_projection(
        "payments", (_page("z-page", "coverage-id", title="Other"),)
    )
    rich = assemble_projection(
        "accounts", (_page("a-page", "rich-id", title="Coverage case"),)
    )
    rich_scenario = replace(
        rich.scenarios[0],
        items=tuple(
            replace(item, name="Coverage command")
            if item.phase == "when" else item
            for item in rich.scenarios[0].items
        ),
    )
    rich = replace(rich, scenarios=(rich_scenario,))

    results = search_projections((sparse, rich, sparse, rich), "coverage", 10)

    assert [(item.domain, item.scenario_id) for item in results] == [
        ("accounts", "rich-id"),
        ("payments", "coverage-id"),
    ]


def test_search_repeated_domain_projection_location_choice_is_deterministic():
    zeta = assemble_projection(
        "payments", (_page("z-page", "same-id", title="Target"),)
    )
    alpha = assemble_projection(
        "payments", (_page("a-page", "same-id", title="Target"),)
    )

    first = search_projections((zeta, alpha), "same-id", 10)
    second = search_projections((alpha, zeta), "same-id", 10)

    assert len(first) == 1
    assert first == second
    assert first[0].page_slug == "a-page"
