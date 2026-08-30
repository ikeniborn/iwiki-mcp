from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from iwiki_mcp import server
from iwiki_mcp.specifications import PageSnapshot, assemble_projection


def _page(scenario_id="open-account", title="Open account"):
    return PageSnapshot(
        slug=f"specification/{scenario_id}",
        revision="page-r1",
        markdown=f'''---
type: specification
---
# Account

## Open account

```iwiki-gwt
id = "{scenario_id}"
title = "{title}"
given = [{{ role = "state", name = "Account pending" }}]
when = {{ role = "command", name = "OpenAccount" }}
then = [{{ role = "event", name = "AccountOpened" }}]
code = [
  {{ relation = "implements", symbol = "accounts.Account.open" }},
  {{ relation = "verifies", file = "tests/test_accounts.py" }}
]
```
''',
    )


class Store:
    def __init__(self):
        self.projections = {
            "payments": assemble_projection("payments", (_page(),)),
            "accounts": assemble_projection(
                "accounts", (_page("close-account", "Close account"),)
            ),
        }
        self.calls = []

    def search(self, domains, query, limit):
        from iwiki_mcp.specifications import search_projections

        self.calls.append(("search", domains, query, limit))
        return search_projections(tuple(self.projections[d] for d in domains), query, limit)

    def context(self, domain, scenario_id):
        from iwiki_mcp.specifications import projection_context

        self.calls.append(("context", domain, scenario_id))
        return projection_context(self.projections[domain], scenario_id)

    def status(self, domain):
        from iwiki_mcp.specification_store import ProjectionStatus

        self.calls.append(("status", domain))
        projection = self.projections[domain]
        return ProjectionStatus(
            domain=domain,
            state="ready",
            markdown_revision=projection.markdown_revision,
            scenario_count=projection.scenario_count,
            binding_count=projection.binding_count,
        )

    def duplicate_locations(self, domain, scenario_id):
        projection = self.projections[domain]
        finding = next((item for item in projection.findings if (
            item.type == "duplicate_scenario_id"
            and item.scenario_id == scenario_id
        )), None)
        return () if finding is None else finding.locations


@pytest.fixture
def bound(tmp_path, monkeypatch):
    binding = server.base.Binding(
        base=str(tmp_path / "wiki"),
        read=("payments", "accounts"),
        write=("payments",),
        primary="payments",
        project_dir=str(tmp_path),
    )
    Path(binding.base).mkdir()
    monkeypatch.setattr(server, "_resolved_binding", lambda: binding)
    store = Store()
    monkeypatch.setattr(server, "_specification_store", lambda _binding: store)
    monkeypatch.setattr(
        server,
        "_specification_graph_resolver",
        lambda _binding, _domain: server._specifications.UnavailableSpecificationGraphResolver(
            "not_configured"
        ),
    )
    return binding, store


@pytest.mark.parametrize("query", ["", "   ", "nul\0query", "x" * 4097])
def test_search_rejects_invalid_query_before_binding(monkeypatch, query):
    monkeypatch.setattr(
        server, "_resolved_binding", lambda: pytest.fail("binding must not be read")
    )

    assert server.wiki_spec_search(query)["error"] == "invalid_query"


@pytest.mark.parametrize("limit", [0, 101, True, 1.5])
def test_search_rejects_invalid_limit_before_storage(monkeypatch, limit):
    monkeypatch.setattr(
        server, "_resolved_binding", lambda: pytest.fail("binding must not be read")
    )

    assert server.wiki_spec_search("account", limit=limit)["error"] == "invalid_limit"


def test_search_uses_only_bound_read_scope_and_returns_declared_selectors(bound):
    _binding, store = bound

    result = server.wiki_spec_search("account")

    assert [item["identity"] for item in result["results"]] == [
        "accounts#close-account", "payments#open-account"
    ]
    assert result["domains"] == ["accounts", "payments"]
    assert all(item["bindings"] for item in result["results"])
    assert store.calls[0] == (
        "search", ("accounts", "payments"), "account", 20
    )


def test_search_rejects_domain_outside_read_scope_before_storage(bound):
    _binding, store = bound

    result = server.wiki_spec_search("account", domains=["secret"])

    assert result["error"] == "access_denied"
    assert store.calls == []


@pytest.mark.parametrize("domains", [["../secret"], ["nul\0domain"]])
def test_search_sanitizes_malformed_domains_before_storage(bound, domains):
    _binding, store = bound

    result = server.wiki_spec_search("account", domains=domains)

    assert result["error"] == "invalid_domains"
    assert "secret" not in repr(result)
    assert store.calls == []


def test_search_returns_disabled_domain_state_without_store_or_graph(bound):
    binding, store = bound
    server._resolved_binding = lambda: replace(
        binding, specification_mode="disabled"
    )

    result = server.wiki_spec_search("account")

    assert result["results"] == []
    assert result["domain_states"] == [
        {"domain": "accounts", "state": "disabled", "mode": "disabled"},
        {"domain": "payments", "state": "disabled", "mode": "disabled"},
    ]
    assert store.calls == []


def test_context_is_read_only_and_reports_not_checked_without_graph(bound):
    _binding, store = bound

    result = server.wiki_spec_context("payments", "open-account")

    assert result["identity"] == "payments#open-account"
    assert {item["freshness"] for item in result["bindings"]} == {"not_checked"}
    assert all("selector" in item for item in result["bindings"])
    assert not any(call[0] == "record" for call in store.calls)


def test_context_rejects_read_scope_before_storage(bound):
    binding, store = bound
    server._resolved_binding = lambda: replace(binding, read=("accounts",))

    result = server.wiki_spec_context("payments", "open-account")

    assert result["error"] == "access_denied"
    assert store.calls == []


def test_context_reports_authorized_duplicate_locations(bound):
    _binding, store = bound
    store.projections["payments"] = assemble_projection(
        "payments", (_page(), replace(_page(), slug="specification/duplicate"))
    )

    result = server.wiki_spec_context("payments", "open-account")

    assert result["error"] == "ambiguous_scenario_id"
    assert [item["slug"] for item in result["locations"]] == [
        "specification/duplicate", "specification/open-account"
    ]


def test_postgres_duplicate_lookup_uses_only_persisted_projection_findings():
    from iwiki_mcp.specification_store import (
        DomainProjection,
        FindingRecord,
        ScenarioLocation,
    )

    class Store:
        specification_mode = "optional"

        def _specification_projection(self, domain):
            return DomainProjection(
                domain=domain,
                markdown_revision="sha256:" + "0" * 64,
                scenarios=(),
                bindings=(),
                evidence=(),
                findings=(FindingRecord(
                    type="duplicate_scenario_id",
                    scenario_id="open-account",
                    locations=(
                        ScenarioLocation(
                            "specification/z-second",
                            "Open account",
                            "open-account",
                        ),
                        ScenarioLocation(
                            "specification/a-first",
                            "Open account",
                            "open-account",
                        ),
                    ),
                ),),
            )

    locations = server._PostgresSpecificationQueryStore(
        Store()
    ).duplicate_locations("payments", "open-account")

    assert [item.slug for item in locations] == [
        "specification/a-first", "specification/z-second"
    ]


def test_resolve_requires_write_scope_before_store_or_graph(bound):
    binding, store = bound
    server._resolved_binding = lambda: replace(binding, write=())

    result = server.wiki_spec_resolve("payments", "open-account")

    assert result["error"] == "access_denied"
    assert store.calls == []


@pytest.mark.parametrize(
    "call",
    [
        lambda: server.wiki_spec_search("account"),
        lambda: server.wiki_spec_context("payments", "open-account"),
        lambda: server.wiki_spec_resolve("payments", "open-account"),
    ],
)
def test_tool_storage_failures_never_expose_raw_private_details(
    bound, monkeypatch, call
):
    monkeypatch.setattr(
        server,
        "_specification_store",
        lambda _binding: (_ for _ in ()).throw(
            RuntimeError("postgres://user:secret@private.example/database")
        ),
    )

    result = call()

    assert result["error"] in {"specification_failed", "resolution_failed"}
    assert "secret" not in repr(result)
    assert "private.example" not in repr(result)
