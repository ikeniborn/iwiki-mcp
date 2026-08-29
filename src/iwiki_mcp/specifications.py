"""Graph-free specification projection, search, context, and freshness helpers."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Iterable, Literal, Mapping, Sequence

from .engine import frontmatter
from .engine.specifications import parse_specification_page
from .specification_store import (
    BindingRecord,
    DomainProjection,
    FindingRecord,
    PhaseItemRecord,
    ResolutionAttempt,
    ScenarioContext,
    ScenarioLocation,
    ScenarioRecord,
)


_REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,255}\Z")
_GRAPH_STATE_CODES = frozenset({
    "ready",
    "not_configured",
    "disabled",
    "missing",
    "dirty",
    "rebuilding",
    "failed",
    "stale_graph",
    "source_unavailable",
    "not_primary",
})
_GRAPH_REASON_CODES = frozenset({
    *(_GRAPH_STATE_CODES - {"ready"}),
    "revision_changed",
})


@dataclass(frozen=True)
class PageSnapshot:
    slug: str
    markdown: str
    revision: str | int | None


def _markdown_revision(pages: Sequence[PageSnapshot]) -> str:
    digest = hashlib.sha256()
    for page in sorted(pages, key=lambda item: item.slug):
        digest.update(page.slug.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(page.revision).encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(page.markdown.encode("utf-8")).digest())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _finding(record: Mapping[str, object]) -> FindingRecord:
    missing = record.get("missing", ())
    return FindingRecord(
        type=str(record.get("type", "invalid_scenario")),
        slug=record.get("slug") if isinstance(record.get("slug"), str) else None,
        heading=(record.get("heading")
                 if isinstance(record.get("heading"), str) else None),
        scenario_id=(record.get("scenario_id")
                     if isinstance(record.get("scenario_id"), str) else None),
        reason=(record.get("reason")
                if isinstance(record.get("reason"), str) else None),
        missing=tuple(str(item) for item in missing)
        if isinstance(missing, (tuple, list)) else (),
    )


def _finding_key(record: FindingRecord) -> tuple[object, ...]:
    return (
        record.type,
        record.scenario_id or "",
        record.slug or "",
        record.heading or "",
        record.reason or "",
        record.missing,
        tuple((item.slug, item.heading, item.anchor) for item in record.locations),
    )


def _is_specification_page(markdown: str) -> bool:
    metadata, _ = frontmatter.split(markdown)
    page_type = metadata.get("type")
    return (
        isinstance(page_type, str)
        and frontmatter.normalize_type(page_type) == "specification"
    )


def assemble_projection(
    domain: str,
    pages: Sequence[PageSnapshot],
    previous_evidence: Iterable[ResolutionAttempt] = (),
    *,
    markdown_revision: str | None = None,
) -> DomainProjection:
    """Assemble one coherent domain projection without consulting a graph."""
    ordered_pages = tuple(sorted(pages, key=lambda item: item.slug))
    parsed_scenarios: list[tuple[object, str | int | None]] = []
    findings: list[FindingRecord] = []
    incomplete: set[tuple[str, str, str]] = set()
    for page in ordered_pages:
        if not _is_specification_page(page.markdown):
            continue
        parsed = parse_specification_page(domain, page.slug, page.markdown)
        for finding_value in parsed.findings:
            finding = _finding(finding_value)
            findings.append(finding)
            if finding.type == "incomplete_bindings" and finding.scenario_id:
                incomplete.add((
                    finding.scenario_id,
                    finding.slug or page.slug,
                    finding.heading or "",
                ))
        parsed_scenarios.extend((scenario, page.revision)
                                for scenario in parsed.scenarios)

    by_id: dict[str, list[tuple[object, str | int | None]]] = {}
    for scenario, revision in parsed_scenarios:
        by_id.setdefault(scenario.scenario_id, []).append((scenario, revision))
    duplicate_ids = {scenario_id for scenario_id, values in by_id.items()
                     if len(values) > 1}
    for scenario_id in sorted(duplicate_ids):
        locations = tuple(sorted((ScenarioLocation(
            slug=scenario.slug,
            heading=scenario.heading,
            anchor=scenario.anchor,
        ) for scenario, _ in by_id[scenario_id]), key=lambda item: (
            item.slug, item.heading, item.anchor
        )))
        findings.append(FindingRecord(
            type="duplicate_scenario_id",
            scenario_id=scenario_id,
            locations=locations,
        ))

    scenarios: list[ScenarioRecord] = []
    bindings: list[BindingRecord] = []
    for scenario, revision in parsed_scenarios:
        if scenario.scenario_id in duplicate_ids or (
            scenario.scenario_id, scenario.slug, scenario.heading
        ) in incomplete:
            continue
        scenarios.append(ScenarioRecord(
            domain=scenario.domain,
            scenario_id=scenario.scenario_id,
            title=scenario.title,
            page_slug=scenario.slug,
            heading=scenario.heading,
            anchor=scenario.anchor,
            source_hash=scenario.source_hash,
            items=tuple(PhaseItemRecord(item.phase, item.role, item.name)
                        for item in scenario.items),
            page_revision=revision,
        ))
        bindings.extend(BindingRecord(
            binding_id=binding.binding_id,
            domain=scenario.domain,
            scenario_id=scenario.scenario_id,
            relation=binding.relation,
            phase=binding.phase,
            selector_kind=binding.selector_kind,
            selector=binding.selector,
        ) for binding in scenario.bindings)

    scenarios.sort(key=lambda item: (
        item.domain, item.scenario_id, item.page_slug, item.heading
    ))
    bindings.sort(key=lambda item: (
        item.domain, item.scenario_id, item.binding_id
    ))
    scenario_hashes = {
        (scenario.domain, scenario.scenario_id): scenario.source_hash
        for scenario in scenarios
    }
    binding_keys = {
        (binding.domain, binding.scenario_id, binding.binding_id)
        for binding in bindings
    }
    latest: dict[tuple[str, str, str], ResolutionAttempt] = {}
    for attempt in previous_evidence:
        attempt_key = (
            attempt.domain, attempt.scenario_id, attempt.binding_id
        )
        if (
            attempt_key in binding_keys
            and scenario_hashes.get((attempt.domain, attempt.scenario_id))
            == attempt.specification_source_hash
            and (
                attempt_key not in latest
                or latest[attempt_key].checked_at < attempt.checked_at
            )
        ):
            latest[attempt_key] = attempt
    evidence = tuple(sorted(latest.values(), key=lambda item: (
        item.domain, item.scenario_id, item.binding_id, item.checked_at
    )))
    return DomainProjection(
        domain=domain,
        markdown_revision=(markdown_revision
                           if markdown_revision is not None
                           else _markdown_revision(ordered_pages)),
        scenarios=tuple(scenarios),
        bindings=tuple(bindings),
        evidence=evidence,
        findings=tuple(sorted(findings, key=_finding_key)),
    )


def _query_tokens(query: str) -> tuple[str, ...]:
    return tuple(sorted({token for token in query.casefold().split() if token}))


def _search_rank(
    scenario: ScenarioRecord,
    bindings: tuple[BindingRecord, ...],
    normalized: str,
    tokens: tuple[str, ...],
) -> tuple[int, int] | None:
    if not tokens:
        return None
    fields = (
        scenario.scenario_id.casefold(),
        scenario.title.casefold(),
        *(" ".join((item.phase, item.role, item.name)).casefold()
          for item in scenario.items),
        *(item.selector.casefold() for item in bindings),
    )
    if not all(any(token in field for field in fields) for token in tokens):
        return None
    coverage = sum(
        token in field for token in tokens for field in fields
    )
    if normalized == fields[0]:
        return 0, -coverage
    if normalized == fields[1]:
        return 1, -coverage
    return 2, -coverage


def search_projections(
    projections: Sequence[DomainProjection], query: str, limit: int
) -> tuple[ScenarioRecord, ...]:
    """Search persisted semantics with deterministic, graph-free ranking."""
    candidates: dict[
        tuple[str, str],
        tuple[tuple[object, ...], ScenarioRecord, tuple[BindingRecord, ...]],
    ] = {}
    for projection in projections:
        binding_index: dict[tuple[str, str], list[BindingRecord]] = {}
        for binding in projection.bindings:
            binding_index.setdefault(
                (binding.domain, binding.scenario_id), []
            ).append(binding)
        for scenario in projection.scenarios:
            identity = (scenario.domain, scenario.scenario_id)
            bindings = tuple(binding_index.get(identity, ()))
            choice = (
                scenario.page_slug,
                scenario.heading,
                scenario.source_hash,
                projection.markdown_revision,
                tuple(item.binding_id for item in bindings),
            )
            current = candidates.get(identity)
            if current is None or choice < current[0]:
                candidates[identity] = (choice, scenario, bindings)

    normalized = query.casefold().strip()
    tokens = _query_tokens(query)
    ranked: list[tuple[tuple[object, ...], ScenarioRecord]] = []
    for _, scenario, bindings in candidates.values():
        rank = _search_rank(scenario, bindings, normalized, tokens)
        if rank is not None:
            ranked.append(((*rank, scenario.domain, scenario.scenario_id,
                            scenario.page_slug), scenario))
    ranked.sort(key=lambda item: item[0])
    return tuple(item[1] for item in ranked[:max(0, limit)])


def projection_context(
    projection: DomainProjection, scenario_id: str
) -> ScenarioContext | None:
    """Return complete stored semantics and evidence without graph access."""
    scenario = next((item for item in projection.scenarios if (
        item.domain, item.scenario_id
    ) == (
        projection.domain, scenario_id
    )), None)
    if scenario is None:
        return None
    bindings = tuple(item for item in projection.bindings if (
        item.domain, item.scenario_id
    ) == (
        projection.domain, scenario_id
    ))
    binding_keys = {
        (item.domain, item.scenario_id, item.binding_id) for item in bindings
    }
    evidence = tuple(item for item in projection.evidence if (
        item.domain, item.scenario_id, item.binding_id
    ) in binding_keys)
    findings = tuple(item for item in projection.findings if (
        item.scenario_id == scenario_id
        or (item.slug == scenario.page_slug and item.scenario_id is None)
    ))
    return ScenarioContext(
        scenario=scenario,
        bindings=bindings,
        evidence=evidence,
        projection_state=projection.state,
        projection_revision=projection.markdown_revision,
        findings=findings,
    )


Freshness = Literal["not_checked", "fresh", "stale_spec", "stale_graph"]


def evidence_freshness(
    attempt: ResolutionAttempt | None,
    current_source_hash: str,
    current_graph_revision: str | None,
    current_graph_state_fingerprint: str,
    *,
    graph_ready: bool,
) -> Freshness:
    if attempt is None:
        return "not_checked"
    if attempt.specification_source_hash != current_source_hash:
        return "stale_spec"
    if attempt.state == "graph_unavailable":
        if graph_ready:
            return "stale_graph"
        if attempt.graph_state_fingerprint != current_graph_state_fingerprint:
            return "stale_graph"
        return "fresh"
    if not graph_ready or attempt.graph_revision != current_graph_revision:
        return "stale_graph"
    return "fresh"


def context_freshness(
    context: ScenarioContext,
    *,
    current_graph_revision: str | None,
    current_graph_state_fingerprint: str,
    graph_ready: bool,
) -> tuple[tuple[str, Freshness], ...]:
    """Return deterministic persisted-evidence freshness for every binding."""
    evidence = {
        (item.domain, item.scenario_id, item.binding_id): item
        for item in context.evidence
    }
    result: list[tuple[str, Freshness]] = []
    for binding in context.bindings:
        attempt = evidence.get((
            binding.domain, binding.scenario_id, binding.binding_id
        ))
        result.append((binding.binding_id, evidence_freshness(
            attempt,
            context.scenario.source_hash,
            current_graph_revision,
            current_graph_state_fingerprint,
            graph_ready=graph_ready,
        )))
    return tuple(result)


def graph_state_fingerprint(state: object) -> str:
    """Return a sanitized deterministic fingerprint without retaining raw state."""
    public = state if isinstance(state, Mapping) else {}
    state_value = public.get("state")
    normalized_state = (
        state_value
        if type(state_value) is str and state_value in _GRAPH_STATE_CODES
        else "failed"
    )
    reason_value = public.get("reason")
    normalized_reason = (
        None
        if reason_value is None
        else reason_value
        if type(reason_value) is str and reason_value in _GRAPH_REASON_CODES
        else "failed"
    )
    revision_value = public.get("revision")
    normalized_revision = (
        revision_value
        if isinstance(revision_value, str) and _REVISION.fullmatch(revision_value)
        else None
    )
    payload = json.dumps(
        {
            "reason": normalized_reason,
            "revision": normalized_revision,
            "state": normalized_state,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"
