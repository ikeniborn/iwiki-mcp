from __future__ import annotations

from dataclasses import replace
from hashlib import sha256

import pytest

from iwiki_mcp import server
from iwiki_mcp.specification_store import (
    BindingRecord,
    DomainProjection,
    FindingRecord,
    ProjectionStatus,
    ResolutionAttempt,
    ScenarioLocation,
)
from iwiki_mcp.specifications import (
    PageSnapshot,
    UnavailableSpecificationGraphResolver,
    assemble_projection,
    graph_state_fingerprint,
    projection_context,
)


def _binding(tmp_path, mode="optional"):
    wiki = tmp_path / "wiki"
    (wiki / "docs").mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    return server.base.Binding(
        base=str(wiki),
        read=("docs",),
        write=("docs",),
        primary="docs",
        project_dir=str(project),
        specification_mode=mode,
    )


def _projection():
    page = PageSnapshot(
        slug="specification/status",
        revision="page-r1",
        markdown='''---
type: specification
---
# Status

## Status behavior

```iwiki-gwt
id = "status-behavior"
title = "Status behavior"
given = []
when = { role = "command", name = "Inspect" }
then = [{ role = "event", name = "Inspected" }]
code = [
  { relation = "implements", symbol = "app.inspect" },
  { relation = "verifies", file = "tests/test_status.py" }
]
```
''',
    )
    base_projection = assemble_projection("docs", (page,))

    def extra_binding(relation, phase, selector_kind, selector):
        values = (
            "docs", "status-behavior", relation, phase or "",
            selector_kind, selector,
        )
        binding_id = "spec:binding:" + sha256(
            "\0".join(values).encode("utf-8")
        ).hexdigest()
        return BindingRecord(
            binding_id=binding_id,
            domain="docs",
            scenario_id="status-behavior",
            relation=relation,
            phase=phase,
            selector_kind=selector_kind,
            selector=selector,
        )

    first, second = base_projection.bindings
    bindings = (
        first,
        second,
        extra_binding("implements", "when", "symbol", "app.other"),
        extra_binding("verifies", None, "file", "tests/test_other.py"),
    )
    scenario = base_projection.scenarios[0]
    ready_fingerprint = graph_state_fingerprint({
        "state": "ready", "revision": "graph-r1", "reason": None,
    })
    unavailable_fingerprint = graph_state_fingerprint({
        "state": "not_configured",
        "revision": None,
        "reason": "not_configured",
    })
    evidence = (
        ResolutionAttempt(
            binding_id=bindings[0].binding_id,
            domain="docs",
            scenario_id=scenario.scenario_id,
            state="unresolved",
            targets=(),
            unresolved_reference=bindings[0].selector,
            graph_revision="graph-r1",
            graph_state_fingerprint=ready_fingerprint,
            specification_source_hash=scenario.source_hash,
            checked_at="2026-08-30T10:00:00Z",
            reason=None,
        ),
        ResolutionAttempt(
            binding_id=bindings[1].binding_id,
            domain="docs",
            scenario_id=scenario.scenario_id,
            state="ambiguous",
            targets=("symbol:a", "symbol:b"),
            unresolved_reference=None,
            graph_revision="graph-r1",
            graph_state_fingerprint=ready_fingerprint,
            specification_source_hash=scenario.source_hash,
            checked_at="2026-08-30T10:00:00Z",
            reason=None,
        ),
        ResolutionAttempt(
            binding_id=bindings[2].binding_id,
            domain="docs",
            scenario_id=scenario.scenario_id,
            state="graph_unavailable",
            targets=(),
            unresolved_reference=bindings[2].selector,
            graph_revision=None,
            graph_state_fingerprint=unavailable_fingerprint,
            specification_source_hash=scenario.source_hash,
            checked_at="2026-08-30T10:00:00Z",
            reason="not_configured",
        ),
    )
    findings = (
        FindingRecord(type="missing_scenario", slug="specification/missing"),
        FindingRecord(
            type="invalid_scenario",
            slug="specification/invalid",
            reason="malformed_toml",
        ),
        FindingRecord(
            type="duplicate_scenario_id",
            scenario_id="duplicate-id",
            locations=(
                ScenarioLocation("specification/a", "A", "a"),
                ScenarioLocation("specification/b", "B", "b"),
            ),
        ),
        FindingRecord(
            type="incomplete_bindings",
            slug="specification/incomplete",
            scenario_id="incomplete-id",
            missing=("verifies",),
        ),
    )
    return DomainProjection(
        domain="docs",
        markdown_revision=base_projection.markdown_revision,
        scenarios=base_projection.scenarios,
        bindings=bindings,
        evidence=evidence,
        findings=findings,
        state="stale",
        reason="projection_stale",
    )


class _Store:
    def __init__(self, projection, status=None):
        self.projection = projection
        self._status = status or ProjectionStatus(
            domain="docs",
            state="stale",
            markdown_revision=projection.markdown_revision,
            scenario_count=projection.scenario_count,
            binding_count=projection.binding_count,
            reason="projection_stale",
        )
        self.calls = []

    def status(self, domain):
        self.calls.append(("status", domain))
        return self._status

    def projection_for_lint(self, domain):
        self.calls.append(("projection", domain))
        return self.projection

    def context(self, domain, scenario_id):
        self.calls.append(("context", domain, scenario_id))
        return projection_context(self.projection, scenario_id)


def test_status_adds_independent_specification_domain_block(tmp_path, monkeypatch):
    binding = _binding(tmp_path, "strict")
    (tmp_path / "project" / ".iwiki.toml").write_text(
        '[specifications]\nmode = "strict"\n', encoding="utf-8"
    )
    store = _Store(_projection(), ProjectionStatus(
        domain="docs",
        state="ready",
        markdown_revision="projection-r1",
        scenario_count=1,
        binding_count=4,
    ))
    monkeypatch.setattr(server, "_resolved_binding", lambda: binding)
    monkeypatch.setattr(server, "_specification_store", lambda _binding: store)

    result = server.wiki_status()

    assert {key: result[key] for key in (
        "base", "read", "write", "primary", "project_dir", "domains"
    )} == {
        "base": binding.base,
        "read": ["docs"],
        "write": ["docs"],
        "primary": "docs",
        "project_dir": binding.project_dir,
        "domains": ["docs"],
    }
    assert result["specifications"] == {"domains": [{
        "domain": "docs",
        "mode": "strict",
        "source": "project",
        "projection_state": "ready",
        "scenarios": 1,
        "bindings": 4,
    }]}


def test_status_specification_failure_is_sanitized_and_keeps_ordinary_fields(
    tmp_path, monkeypatch,
):
    binding = _binding(tmp_path)

    class FailingStore:
        def status(self, _domain):
            raise RuntimeError("postgresql://user:secret@private.invalid/wiki")

    monkeypatch.setattr(server, "_resolved_binding", lambda: binding)
    monkeypatch.setattr(server, "_specification_store", lambda _binding: FailingStore())

    result = server.wiki_status()

    assert result["domains"] == ["docs"]
    assert result["specifications"] == {"domains": [{
        "domain": "docs",
        "mode": "optional",
        "source": "built_in_default",
        "projection_state": "failed",
        "scenarios": 0,
        "bindings": 0,
    }]}
    assert "secret" not in repr(result)
    assert "private.invalid" not in repr(result)


@pytest.mark.parametrize("mode", ["optional", "strict"])
def test_lint_adds_all_specification_findings_with_mode_severity(
    tmp_path, monkeypatch, mode,
):
    binding = _binding(tmp_path, mode)
    (tmp_path / "project" / ".iwiki.toml").write_text(
        f'[specifications]\nmode = "{mode}"\n', encoding="utf-8"
    )
    projection = _projection()
    store = _Store(projection)
    ordinary = {"wiki_present": True, "pages": 7, "broken": [], "sections": []}
    monkeypatch.setattr(server, "_resolved_binding", lambda: binding)
    monkeypatch.setattr(server, "lint", lambda *_args, **_kwargs: dict(ordinary))
    monkeypatch.setattr(server, "_specification_store", lambda _binding: store)
    monkeypatch.setattr(
        server,
        "_specification_graph_resolver",
        lambda *_args: UnavailableSpecificationGraphResolver("missing"),
    )
    monkeypatch.setattr(
        server._codegraph_application,
        "code_runtime",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("graph failed")),
    )

    report = server.wiki_lint("docs")["reports"]["docs"]

    assert {key: report[key] for key in ordinary} == ordinary
    specification = report["specifications"]
    assert specification["mode"] == mode
    assert specification["source"] == "project"
    assert specification["state"] == "stale"
    assert specification["projection_revision"] == projection.markdown_revision
    assert specification["scenarios"] == 1
    assert specification["bindings"] == 4
    by_type = {}
    for finding in specification["findings"]:
        by_type.setdefault(finding["type"], []).append(finding)
    assert set(by_type) == {
        "missing_scenario",
        "invalid_scenario",
        "duplicate_scenario_id",
        "incomplete_bindings",
        "projection_stale",
        "binding_unresolved",
        "binding_ambiguous",
        "resolution_not_checked",
        "resolution_stale_spec",
        "resolution_stale_graph",
        "graph_unavailable",
    }
    authored = {
        "missing_scenario", "invalid_scenario",
        "duplicate_scenario_id", "incomplete_bindings",
    }
    assert {
        item["severity"]
        for finding_type in authored
        for item in by_type[finding_type]
    } == ({"block"} if mode == "strict" else {"advisory"})
    assert all(
        item["severity"] == "advisory"
        for finding_type, findings in by_type.items()
        if finding_type not in authored
        for item in findings
    )


def test_disabled_lint_returns_no_findings_or_projection_calls(tmp_path, monkeypatch):
    binding = _binding(tmp_path, "disabled")
    ordinary = {"wiki_present": True, "pages": 0, "broken": []}
    monkeypatch.setattr(server, "_resolved_binding", lambda: binding)
    monkeypatch.setattr(server, "lint", lambda *_args, **_kwargs: dict(ordinary))
    monkeypatch.setattr(
        server,
        "_specification_store",
        lambda _binding: pytest.fail("disabled lint opened projection storage"),
    )
    monkeypatch.setattr(
        server._codegraph_application,
        "code_runtime",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("graph failed")),
    )

    report = server.wiki_lint("docs")["reports"]["docs"]

    assert {key: report[key] for key in ordinary} == ordinary
    assert report["specifications"] == {
        "mode": "disabled",
        "source": "project",
        "state": "disabled",
        "projection_revision": None,
        "scenarios": 0,
        "bindings": 0,
        "findings": [],
    }


def test_lint_projection_failure_is_sanitized_and_keeps_ordinary_fields(
    tmp_path, monkeypatch,
):
    binding = _binding(tmp_path)
    ordinary = {"wiki_present": True, "pages": 2, "broken": []}

    class FailingStore:
        def status(self, _domain):
            raise RuntimeError("postgresql://user:secret@private.invalid/wiki")

    monkeypatch.setattr(server, "_resolved_binding", lambda: binding)
    monkeypatch.setattr(server, "lint", lambda *_args, **_kwargs: dict(ordinary))
    monkeypatch.setattr(server, "_specification_store", lambda _binding: FailingStore())
    monkeypatch.setattr(
        server._codegraph_application,
        "code_runtime",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("graph failed")),
    )

    report = server.wiki_lint("docs")["reports"]["docs"]

    assert {key: report[key] for key in ordinary} == ordinary
    assert report["specifications"] == {
        "mode": "optional",
        "source": "built_in_default",
        "state": "failed",
        "projection_revision": None,
        "scenarios": 0,
        "bindings": 0,
        "findings": [{"type": "projection_failed", "severity": "advisory"}],
    }
    assert "secret" not in repr(report)
    assert "private.invalid" not in repr(report)


def test_hosted_policy_uses_exact_override_then_default_without_mutation(
    tmp_path, monkeypatch,
):
    from iwiki_mcp.postgres.config import (
        HostedSpecificationsConfig,
        SpecificationOverride,
    )

    binding = replace(
        _binding(tmp_path),
        read=("docs", "shared"),
    )
    policy = HostedSpecificationsConfig(
        default_mode="optional",
        overrides=(SpecificationOverride("wiki-a", "docs", "strict"),),
    )
    binding = server.base.PostgresBinding(
        host="db.invalid",
        port=5432,
        database="wiki",
        user="iwiki",
        sslmode="require",
        password="secret",
        iwiki_id="wiki-a",
        read=binding.read,
        write=("docs",),
        primary="docs",
        project_dir=binding.project_dir,
        embed_model="fixture",
        embed_dimensions=3,
        rerank_model="",
    )
    monkeypatch.setattr(server, "_HOSTED_SPECIFICATIONS", policy, raising=False)
    token = server._SESSION_BINDING.set(binding)
    try:
        assert server._specification_policy(binding, "docs") == (
            "strict", "hosted_override"
        )
        assert server._specification_policy(binding, "shared") == (
            "optional", "hosted_default"
        )
        assert policy.default_mode == "optional"
    finally:
        server._SESSION_BINDING.reset(token)
