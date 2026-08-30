from __future__ import annotations

import json
from time import perf_counter_ns

import pytest

from iwiki_mcp.specifications import (
    PageSnapshot,
    SpecificationService,
    UnavailableSpecificationGraphResolver,
    assemble_projection,
    projection_context,
    search_projections,
)


def _page(page_number: int) -> PageSnapshot:
    blocks = []
    for scenario_number in range(2):
        scenario_id = f"scenario-{page_number:03d}-{scenario_number}"
        blocks.append(f'''## Behavior {page_number:03d}-{scenario_number}

```iwiki-gwt
id = "{scenario_id}"
title = "Measured behavior {page_number:03d}-{scenario_number}"
given = []
when = {{ role = "command", name = "Measure{page_number:03d}{scenario_number}" }}
then = [{{ role = "event", name = "Measured{page_number:03d}{scenario_number}" }}]
code = [
  {{ relation = "implements", symbol = "app.measure_{page_number:03d}_{scenario_number}" }},
  {{ relation = "verifies", file = "tests/test_measure_{page_number:03d}_{scenario_number}.py" }}
]
```
''')
    return PageSnapshot(
        slug=f"specification/page-{page_number:03d}",
        revision=f"page-r{page_number:03d}",
        markdown="---\ntype: specification\n---\n# Measured page\n\n" + "\n".join(blocks),
    )


class _MemoryStore:
    def __init__(self, projection):
        self.projection = projection

    def context(self, domain, scenario_id):
        assert domain == self.projection.domain
        return projection_context(self.projection, scenario_id)

    def record_resolutions(self, attempts):
        self.projection = self.projection.with_evidence(attempts)


def _elapsed_ms(start: int) -> float:
    return (perf_counter_ns() - start) / 1_000_000


@pytest.mark.measurement
def test_deterministic_specification_path_measurements():
    pages = tuple(_page(number) for number in range(100))

    started = perf_counter_ns()
    projection = assemble_projection("docs", pages)
    projection_rebuild_ms = _elapsed_ms(started)

    started = perf_counter_ns()
    results = search_projections((projection,), "measured behavior", 100)
    search_ms = _elapsed_ms(started)

    started = perf_counter_ns()
    context = projection_context(projection, "scenario-000-0")
    context_ms = _elapsed_ms(started)

    store = _MemoryStore(projection)
    service = SpecificationService(
        store,
        resolver=UnavailableSpecificationGraphResolver("not_configured"),
        clock=lambda: "2026-08-30T12:00:00Z",
    )
    started = perf_counter_ns()
    resolution = service.resolve("docs", "scenario-000-0")
    resolution_ms = _elapsed_ms(started)

    record = {
        "pages": len(pages),
        "scenarios": projection.scenario_count,
        "bindings": projection.binding_count,
        "projection_rebuild_ms": projection_rebuild_ms,
        "search_ms": search_ms,
        "context_ms": context_ms,
        "resolution_ms": resolution_ms,
    }
    print(json.dumps(record, sort_keys=True, separators=(",", ":")))

    assert context is not None
    assert len(results) == 100
    assert len(resolution.evidence) == 2
    assert set(record) == {
        "pages",
        "scenarios",
        "bindings",
        "projection_rebuild_ms",
        "search_ms",
        "context_ms",
        "resolution_ms",
    }
    assert (record["pages"], record["scenarios"], record["bindings"]) == (
        100, 200, 400,
    )
    assert all(
        record[key] >= 0
        for key in (
            "projection_rebuild_ms",
            "search_ms",
            "context_ms",
            "resolution_ms",
        )
    )
