from __future__ import annotations

from dataclasses import dataclass

import pytest

from iwiki_mcp.specification_store import ResolutionAttempt
from iwiki_mcp.specifications import (
    PageSnapshot,
    SpecificationGraphSnapshot,
    SpecificationService,
    UnavailableSpecificationGraphResolver,
    assemble_projection,
)


def _page() -> PageSnapshot:
    return PageSnapshot(
        slug="specification/open-account",
        revision="page-r1",
        markdown='''---
type: specification
---
# Account

## Open account

```iwiki-gwt
id = "open-account"
title = "Open account"
given = [{ role = "state", name = "Account is pending" }]
when = { role = "command", name = "OpenAccount" }
then = [{ role = "event", name = "AccountOpened" }]
code = [
  { relation = "implements", symbol = "accounts.Account.open" },
  { relation = "implements", file = "src/accounts.py" },
  { relation = "verifies", source_glob = "tests/test_accounts*.py" }
]
```
''',
    )


@dataclass
class MemoryStore:
    projection: object

    def __post_init__(self):
        self.writes = []

    def context(self, domain, scenario_id):
        from iwiki_mcp.specifications import projection_context

        return projection_context(self.projection, scenario_id)

    def record_resolutions(self, attempts):
        attempts = tuple(attempts)
        self.writes.append(attempts)
        self.projection = self.projection.with_evidence(attempts)


class SnapshotResolver:
    def __init__(self, snapshot, statuses=None):
        self.snapshot = snapshot
        self.statuses = list(statuses or ({
            "state": "ready", "revision": snapshot.revision,
        },))
        self.snapshot_calls = 0

    def status(self):
        if len(self.statuses) > 1:
            return self.statuses.pop(0)
        return self.statuses[0]

    def specification_snapshot(self):
        self.snapshot_calls += 1
        return self.snapshot


def _snapshot(revision="graph-r1"):
    return SpecificationGraphSnapshot(
        revision=revision,
        files=(
            {"file_id": "py:file:accounts", "path": "src/accounts.py"},
            {"file_id": "py:file:test-a", "path": "tests/test_accounts.py"},
            {"file_id": "py:file:test-b", "path": "tests/test_accounts_extra.py"},
        ),
        symbols=(
            {
                "symbol_id": "py:symbol:open-a",
                "qualified_name": "accounts.Account.open",
            },
            {
                "symbol_id": "py:symbol:open-b",
                "qualified_name": "accounts.Account.open",
            },
        ),
    )


def test_resolution_maps_symbol_file_and_glob_in_one_persisted_attempt():
    projection = assemble_projection("payments", (_page(),))
    store = MemoryStore(projection)
    service = SpecificationService(
        store,
        resolver=SnapshotResolver(_snapshot()),
        clock=lambda: "2026-08-29T12:00:00Z",
    )

    attempt = service.resolve("payments", "open-account")

    assert attempt.specification_hash == projection.scenarios[0].source_hash
    assert attempt.graph_revision == "graph-r1"
    assert [item.state for item in attempt.evidence] == [
        "ambiguous", "resolved", "ambiguous",
    ]
    assert len(store.writes) == 1
    assert store.writes[0] == attempt.evidence


def test_ready_graph_without_targets_records_unresolved_selectors():
    projection = assemble_projection("payments", (_page(),))
    store = MemoryStore(projection)
    empty = SpecificationGraphSnapshot(
        revision="graph-r1", files=(), symbols=()
    )

    attempt = SpecificationService(
        store,
        resolver=SnapshotResolver(empty),
        clock=lambda: "2026-08-29T12:00:00Z",
    ).resolve("payments", "open-account")

    assert {item.state for item in attempt.evidence} == {"unresolved"}
    assert all(item.unresolved_reference for item in attempt.evidence)
    assert all(item.graph_revision == "graph-r1" for item in attempt.evidence)


@pytest.mark.parametrize(
    "reason",
    ["not_configured", "disabled", "missing", "failed", "source_unavailable", "not_primary"],
)
def test_unavailable_graph_records_sanitized_fail_soft_evidence(reason):
    projection = assemble_projection("payments", (_page(),))
    store = MemoryStore(projection)
    resolver = UnavailableSpecificationGraphResolver(reason)

    attempt = SpecificationService(
        store, resolver=resolver, clock=lambda: "2026-08-29T12:00:00Z"
    ).resolve("payments", "open-account")

    assert {item.state for item in attempt.evidence} == {"graph_unavailable"}
    assert {item.reason for item in attempt.evidence} == {reason}
    assert all(item.graph_revision is None for item in attempt.evidence)
    assert len(store.writes) == 1


def test_revision_change_discards_every_target_and_records_only_revision_changed():
    projection = assemble_projection("payments", (_page(),))
    store = MemoryStore(projection)
    resolver = SnapshotResolver(
        _snapshot(),
        statuses=(
            {"state": "ready", "revision": "graph-r1"},
            {"state": "ready", "revision": "graph-r2"},
        ),
    )

    attempt = SpecificationService(
        store, resolver=resolver, clock=lambda: "2026-08-29T12:00:00Z"
    ).resolve("payments", "open-account")

    assert {item.state for item in attempt.evidence} == {"graph_unavailable"}
    assert {item.reason for item in attempt.evidence} == {"revision_changed"}
    assert all(item.targets == () for item in attempt.evidence)
    assert len(store.writes) == 1


def test_source_change_aborts_without_persisting_partial_evidence():
    projection = assemble_projection("payments", (_page(),))
    store = MemoryStore(projection)
    original_context = store.context
    calls = 0

    def changing_context(domain, scenario_id):
        nonlocal calls
        calls += 1
        context = original_context(domain, scenario_id)
        if calls == 1:
            return context
        from dataclasses import replace

        return replace(context, scenario=replace(context.scenario, source_hash="f" * 64))

    store.context = changing_context
    service = SpecificationService(
        store,
        resolver=SnapshotResolver(_snapshot()),
        clock=lambda: "2026-08-29T12:00:00Z",
    )

    with pytest.raises(ValueError, match="source_changed"):
        service.resolve("payments", "open-account")

    assert store.writes == []


def test_context_freshness_reads_status_but_never_captures_or_writes_graph():
    projection = assemble_projection("payments", (_page(),))
    binding = projection.bindings[0]
    scenario = projection.scenarios[0]
    evidence = ResolutionAttempt(
        binding_id=binding.binding_id,
        domain=binding.domain,
        scenario_id=binding.scenario_id,
        state="resolved",
        targets=("py:symbol:open-a",),
        unresolved_reference=None,
        graph_revision="graph-r1",
        graph_state_fingerprint=("sha256:" + "0" * 64),
        specification_source_hash=scenario.source_hash,
        checked_at="2026-08-29T11:00:00Z",
        reason=None,
    )
    store = MemoryStore(projection.with_evidence((evidence,)))
    resolver = SnapshotResolver(_snapshot())

    result = SpecificationService(store, resolver=resolver).context(
        "payments", "open-account"
    )

    assert result is not None
    assert dict(result.freshness)[binding.binding_id] == "fresh"
    assert resolver.snapshot_calls == 0
    assert store.writes == []


def test_raw_graph_failures_are_sanitized_and_do_not_escape_resolution():
    projection = assemble_projection("payments", (_page(),))
    store = MemoryStore(projection)

    class FailingResolver:
        def status(self):
            raise RuntimeError("postgres://user:secret@private.example/database")

        def specification_snapshot(self):
            raise AssertionError("non-ready resolver must not capture")

    attempt = SpecificationService(
        store,
        resolver=FailingResolver(),
        clock=lambda: "2026-08-29T12:00:00Z",
    ).resolve("payments", "open-account")

    assert {item.reason for item in attempt.evidence} == {"failed"}
    assert "secret" not in repr(attempt)
