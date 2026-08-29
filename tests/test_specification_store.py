from __future__ import annotations

import json
import os
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from iwiki_mcp import base
from iwiki_mcp.specification_store import (
    BindingRecord,
    DomainProjection,
    FindingRecord,
    GitSpecificationStore,
    PhaseItemRecord,
    ProjectionStatus,
    ResolutionAttempt,
    ScenarioContext,
    ScenarioLocation,
    decode_jsonl,
    encode_jsonl,
)
from iwiki_mcp.specifications import (
    PageSnapshot,
    assemble_projection,
    graph_state_fingerprint,
)


def _fingerprint(
    state: str = "ready",
    reason: str | None = None,
    revision: str | None = "graph-r1",
) -> str:
    return graph_state_fingerprint({
        "state": state,
        "reason": reason,
        "revision": revision,
    })


def _page(scenario_id: str = "open-account") -> PageSnapshot:
    return PageSnapshot(
        slug="account",
        revision="page-r1",
        markdown=f'''---
type: specification
---
# Account

## Open account

```iwiki-gwt
id = "{scenario_id}"
title = "Open account"
given = []
when = {{ role = "command", name = "OpenAccount" }}
then = [{{ role = "event", name = "AccountOpened" }}]
code = [
  {{ relation = "implements", symbol = "Account.open" }},
  {{ relation = "verifies", file = "tests/test_account.py" }}
]
```
''',
    )


def _projection():
    parsed = assemble_projection(
        "payments", (_page(),), markdown_revision="markdown-r1"
    )
    scenario = parsed.scenarios[0]
    binding = parsed.bindings[0]
    evidence = ResolutionAttempt(
        binding_id=binding.binding_id,
        domain="payments",
        scenario_id=scenario.scenario_id,
        state="resolved",
        targets=("symbol:Account.open",),
        unresolved_reference=None,
        graph_revision="graph-r1",
        graph_state_fingerprint=_fingerprint(),
        specification_source_hash=scenario.source_hash,
        checked_at="2026-08-29T10:00:00Z",
        reason=None,
    )
    return parsed.with_evidence((evidence,))


def _canonical_rows(rows) -> bytes:
    return b"".join(
        (
            json.dumps(
                row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            + "\n"
        ).encode("utf-8")
        for row in rows
    )


def _binding_with_scenario(binding: BindingRecord, scenario_id: str) -> BindingRecord:
    digest = hashlib.sha256("\0".join((
        binding.domain,
        scenario_id,
        binding.relation,
        binding.phase or "",
        binding.selector_kind,
        binding.selector,
    )).encode("utf-8")).hexdigest()
    return replace(
        binding,
        scenario_id=scenario_id,
        binding_id=f"spec:binding:{digest}",
    )


def _numbered_binding(
    scenario, relation: str, number: int
) -> BindingRecord:
    selector = f"Account.target_{relation}_{number}"
    digest = hashlib.sha256("\0".join((
        scenario.domain,
        scenario.scenario_id,
        relation,
        "",
        "symbol",
        selector,
    )).encode("utf-8")).hexdigest()
    return BindingRecord(
        binding_id=f"spec:binding:{digest}",
        domain=scenario.domain,
        scenario_id=scenario.scenario_id,
        relation=relation,
        phase=None,
        selector_kind="symbol",
        selector=selector,
    )


def _binding_row(binding: BindingRecord) -> dict[str, object]:
    return {
        "binding_id": binding.binding_id,
        "domain": binding.domain,
        "phase": binding.phase,
        "record": "binding",
        "relation": binding.relation,
        "scenario_id": binding.scenario_id,
        "selector": binding.selector,
        "selector_kind": binding.selector_kind,
    }


def test_jsonl_metadata_version_order_and_exact_logical_round_trip():
    projection = _projection()

    encoded = encode_jsonl(projection)
    rows = [json.loads(line) for line in encoded.decode().splitlines()]

    assert rows[0] == {
        "binding_count": 2,
        "domain": "payments",
        "findings": [],
        "format_version": 1,
        "markdown_revision": "markdown-r1",
        "reason": None,
        "record": "metadata",
        "scenario_count": 1,
        "state": "ready",
    }
    assert [row["record"] for row in rows[1:]] == [
        "scenario",
        "binding",
        "binding",
        "evidence",
    ]
    assert decode_jsonl(encoded) == projection
    assert encode_jsonl(decode_jsonl(encoded)) == encoded


@pytest.mark.parametrize(
    "checked_at",
    [
        "2026-08-29T12:00:00Z",
        "2026-08-29T14:00:00+02:00",
        "2026-08-29T07:00:00-05:00",
        "2026-08-29T12:00:00+00:00",
    ],
)
def test_resolution_timestamp_normalizes_equivalent_offsets(checked_at):
    attempt = replace(_projection().evidence[0], checked_at=checked_at)

    assert attempt.checked_at == "2026-08-29T12:00:00Z"


def test_projection_normalizes_logical_record_order_before_serialization():
    projection = _projection()
    second_binding = projection.bindings[1]
    second_evidence = replace(
        projection.evidence[0],
        binding_id=second_binding.binding_id,
        targets=("file:tests/test_account.py",),
    )
    projection = projection.with_evidence((
        projection.evidence[0], second_evidence,
    ))

    reordered = replace(
        projection,
        bindings=tuple(reversed(projection.bindings)),
        evidence=tuple(reversed(projection.evidence)),
    )

    assert reordered.bindings == projection.bindings
    assert reordered.evidence == projection.evidence
    assert len(reordered.evidence) == 2
    assert decode_jsonl(encode_jsonl(reordered)) == reordered


def test_zero_scenarios_publish_metadata_only_after_projection_exists(tmp_path):
    (tmp_path / "payments").mkdir()
    store = GitSpecificationStore(str(tmp_path))
    store.replace_projection(_projection())
    empty = assemble_projection("payments", (), markdown_revision="markdown-r2")

    result = store.replace_projection(empty)
    path = Path(base.specifications_path(str(tmp_path), "payments"))

    assert result == {"state": "ready", "scenarios": 0, "bindings": 0}
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    assert decode_jsonl(path.read_bytes()) == empty


def test_status_reports_ready_stale_and_failed_with_sanitized_reason(tmp_path):
    (tmp_path / "payments").mkdir()
    store = GitSpecificationStore(str(tmp_path))
    store.replace_projection(_projection())
    assert store.status("payments") == ProjectionStatus(
        domain="payments",
        state="ready",
        markdown_revision="markdown-r1",
        scenario_count=1,
        binding_count=2,
        reason=None,
    )

    store.mark_stale("payments", "out_of_band_change")
    assert store.status("payments").state == "stale"
    store.mark_failed("payments", "secret /tmp/private projection error")
    assert store.status("payments").state == "failed"
    assert store.status("payments").reason == "projection_failed"


def test_prepare_fsyncs_same_directory_and_defers_publication(tmp_path, monkeypatch):
    (tmp_path / "payments").mkdir()
    target = Path(base.specifications_path(str(tmp_path), "payments"))
    fsynced = []
    real_fsync = os.fsync

    def observe_fsync(fd):
        fsynced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr("iwiki_mcp.specification_store.os.fsync", observe_fsync)
    prepared = GitSpecificationStore(str(tmp_path)).prepare(_projection())

    assert fsynced
    assert prepared.temporary_path.parent == target.parent
    assert prepared.temporary_path.exists()
    assert not target.exists()

    prepared.publish()
    assert target.exists()
    assert not prepared.temporary_path.exists()


def test_prepared_abort_cleans_temporary_file_without_publication(tmp_path):
    (tmp_path / "payments").mkdir()
    prepared = GitSpecificationStore(str(tmp_path)).prepare(_projection())
    temporary = prepared.temporary_path

    prepared.abort()

    assert not temporary.exists()
    assert not Path(base.specifications_path(str(tmp_path), "payments")).exists()


def test_direct_prepared_publication_clears_previous_failure_status(tmp_path):
    (tmp_path / "payments").mkdir()
    store = GitSpecificationStore(str(tmp_path))
    store.mark_failed("payments")

    prepared = store.prepare(_projection())
    prepared.publish()

    assert store.status("payments").state == "ready"


def test_disabled_mode_never_opens_reads_or_creates_projection_path(tmp_path, monkeypatch):
    touched = []

    def forbidden_open(*args, **kwargs):
        touched.append(args[0] if args else None)
        raise AssertionError("disabled mode accessed filesystem")

    monkeypatch.setattr("builtins.open", forbidden_open)
    store = GitSpecificationStore(str(tmp_path), mode="disabled")

    assert store.status("payments").state == "disabled"
    assert store.search(("payments",), "account", 10) == ()
    assert store.context("payments", "open-account") is None
    assert store.replace_projection(_projection()) == {"state": "disabled"}
    assert touched == []
    assert not (tmp_path / "payments").exists()


def test_preparation_failure_keeps_old_bytes_and_removes_temporary_artifacts(
    tmp_path, monkeypatch
):
    domain_dir = tmp_path / "payments"
    domain_dir.mkdir()
    store = GitSpecificationStore(str(tmp_path))
    store.replace_projection(_projection())
    target = Path(base.specifications_path(str(tmp_path), "payments"))
    before = target.read_bytes()

    def fail_fsync(_fd):
        raise OSError("private path and secret")

    monkeypatch.setattr("iwiki_mcp.specification_store.os.fsync", fail_fsync)
    with pytest.raises(OSError):
        store.prepare(
            assemble_projection("payments", (_page("next"),), markdown_revision="r2")
        )

    assert target.read_bytes() == before
    assert list(domain_dir.glob(".specifications-*.tmp")) == []
    assert store.status("payments").state == "stale"
    assert store.status("payments").reason == "preparation_failed"


def test_store_search_context_and_resolution_round_trip_without_graph(tmp_path):
    (tmp_path / "payments").mkdir()
    projection = _projection()
    store = GitSpecificationStore(str(tmp_path))
    store.replace_projection(projection.with_evidence(()))

    store.record_resolution(projection.evidence[0])

    assert store.search(("payments",), "open-account", 10) == projection.scenarios
    context = store.context("payments", "open-account")
    assert context is not None
    assert context.evidence == projection.evidence


def test_resolution_record_sanitizes_operational_text_and_orders_targets():
    projection = _projection()
    original = projection.evidence[0]

    attempt = replace(
        original,
        state="ambiguous",
        targets=("symbol:z", "symbol:a", "symbol:a"),
        graph_state_fingerprint=_fingerprint(),
        reason="failed at /private/path with secret",
    )
    encoded = encode_jsonl(projection.with_evidence((attempt,)))

    assert attempt.targets == ("symbol:a", "symbol:z")
    assert attempt.graph_state_fingerprint.startswith("sha256:")
    assert attempt.reason == "failed"
    assert b"secret" not in encoded
    assert b"/private/path" not in encoded


@pytest.mark.parametrize(
    "domain",
    (
        "", ".hidden", ".", "..", "../outside", "/absolute",
        "a/b", "a\\b", "C:\\x", "bad\0domain",
    ),
)
def test_specifications_path_rejects_unsafe_domain_identifiers(tmp_path, domain):
    with pytest.raises(base.BaseError, match="domain identifier is invalid"):
        base.specifications_path(str(tmp_path), domain)


def test_specifications_path_and_prepare_reject_symlinked_domain_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    try:
        (wiki / "payments").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unsupported: {exc}")

    with pytest.raises(base.BaseError, match="domain path escapes wiki base"):
        base.specifications_path(str(wiki), "payments")
    with pytest.raises(base.BaseError, match="domain path escapes wiki base"):
        GitSpecificationStore(str(wiki)).prepare(_projection())

    assert list(outside.iterdir()) == []


def test_public_records_tuple_normalize_collection_fields_deeply():
    projection = _projection()
    scenario = replace(
        projection.scenarios[0], items=list(projection.scenarios[0].items)
    )
    finding = FindingRecord(
        type="duplicate_scenario_id",
        missing=["verifies"],
        locations=[ScenarioLocation("page", "Heading", "heading")],
    )
    attempt = replace(
        projection.evidence[0],
        state="ambiguous",
        targets=["symbol:z", "symbol:a"],
    )
    normalized = DomainProjection(
        domain="payments",
        markdown_revision="r1",
        scenarios=[scenario],
        bindings=list(projection.bindings),
        evidence=[attempt],
        findings=[finding],
    )
    context = ScenarioContext(
        scenario=scenario,
        bindings=list(normalized.bindings),
        evidence=list(normalized.evidence),
        projection_state="ready",
        projection_revision="r1",
        findings=list(normalized.findings),
    )

    assert isinstance(scenario.items, tuple)
    assert isinstance(finding.missing, tuple)
    assert isinstance(finding.locations, tuple)
    assert attempt.targets == ("symbol:a", "symbol:z")
    assert isinstance(normalized.scenarios, tuple)
    assert isinstance(context.bindings, tuple)
    assert isinstance(context.evidence, tuple)
    assert isinstance(context.findings, tuple)


def test_projection_rejects_contradictory_record_associations_and_duplicates():
    projection = _projection()
    scenario = projection.scenarios[0]
    binding = projection.bindings[0]
    evidence = projection.evidence[0]

    invalid_values = (
        {"state": "absent"},
        {"state": "ready", "reason": "projection_failed"},
        {"scenarios": (scenario, scenario)},
        {"bindings": (binding, binding)},
        {"evidence": (evidence, evidence)},
        {"bindings": (_binding_with_scenario(binding, "missing"),)},
        {"evidence": (replace(evidence, scenario_id="other"),)},
        {"scenarios": (replace(scenario, domain="other"),)},
    )
    for values in invalid_values:
        with pytest.raises(ValueError, match="invalid specification projection"):
            replace(projection, **values)


def test_public_records_reject_invalid_enum_values():
    projection = _projection()

    invalid_records = (
        lambda: replace(projection.scenarios[0].items[0], phase="during"),
        lambda: replace(projection.bindings[0], relation="calls"),
        lambda: replace(projection.bindings[0], phase="during"),
        lambda: replace(projection.bindings[0], selector_kind="query"),
        lambda: replace(projection.evidence[0], state="ready"),
        lambda: ProjectionStatus(domain="payments", state="unknown"),
    )
    for create in invalid_records:
        with pytest.raises(ValueError):
            create()


def test_context_and_terminal_status_reject_contradictory_collections():
    projection = _projection()
    context = ScenarioContext(
        scenario=projection.scenarios[0],
        bindings=projection.bindings,
        evidence=projection.evidence,
        projection_state="ready",
        projection_revision=projection.markdown_revision,
        findings=(),
    )

    with pytest.raises(ValueError, match="invalid scenario context"):
        replace(context, bindings=(context.bindings[0], context.bindings[0]))
    with pytest.raises(ValueError, match="invalid scenario context"):
        replace(context, scenario="not-a-scenario")
    with pytest.raises(ValueError, match="invalid specification projection status"):
        ProjectionStatus(domain="payments", state="absent", scenario_count=1)


def test_decoder_rejects_invalid_evidence_or_association_without_raw_payload():
    rows = [json.loads(line) for line in encode_jsonl(_projection()).splitlines()]
    evidence_index = next(
        index for index, row in enumerate(rows) if row["record"] == "evidence"
    )
    binding_index = next(
        index for index, row in enumerate(rows) if row["record"] == "binding"
    )
    payloads = []
    invalid_state = [dict(row) for row in rows]
    invalid_state[evidence_index] = {
        **invalid_state[evidence_index],
        "state": "ready",
    }
    payloads.append(_canonical_rows(invalid_state))
    orphan = [dict(row) for row in rows]
    orphan[binding_index] = {
        **orphan[binding_index],
        "scenario_id": "missing",
    }
    payloads.append(_canonical_rows(orphan))
    cross_scenario = [dict(row) for row in rows]
    cross_scenario[evidence_index] = {
        **cross_scenario[evidence_index],
        "scenario_id": "other",
    }
    payloads.append(_canonical_rows(cross_scenario))
    malformed_targets = [dict(row) for row in rows]
    malformed_targets[evidence_index] = {
        **malformed_targets[evidence_index],
        "targets": {"secret": "/private/path"},
    }
    payloads.append(_canonical_rows(malformed_targets))

    for payload in payloads:
        with pytest.raises(ValueError) as caught:
            decode_jsonl(payload)
        assert "private" not in str(caught.value)
        assert "secret" not in str(caught.value)


def test_direct_publish_failure_cleans_temp_and_optional_status_is_stale(
    tmp_path, monkeypatch
):
    domain = tmp_path / "payments"
    domain.mkdir()
    store = GitSpecificationStore(str(tmp_path))
    store.replace_projection(_projection())
    target = Path(base.specifications_path(str(tmp_path), "payments"))
    before = target.read_bytes()
    prepared = store.prepare(
        assemble_projection("payments", (_page("next"),), markdown_revision="r2")
    )
    temporary = prepared.temporary_path

    monkeypatch.setattr(
        "iwiki_mcp.specification_store.os.replace",
        lambda *_args: (_ for _ in ()).throw(OSError("secret /private/path")),
    )
    with pytest.raises(OSError):
        prepared.publish()

    assert not temporary.exists()
    assert target.read_bytes() == before
    assert store.status("payments") == ProjectionStatus(
        domain="payments",
        state="stale",
        markdown_revision="markdown-r1",
        scenario_count=1,
        binding_count=2,
        reason="publication_failed",
    )


@pytest.mark.parametrize(
    ("mode", "expected_state"),
    (("optional", "stale"), ("strict", "failed")),
)
def test_preparation_failure_status_is_mode_aware_without_previous_projection(
    tmp_path, monkeypatch, mode, expected_state
):
    (tmp_path / "payments").mkdir()
    store = GitSpecificationStore(str(tmp_path), mode=mode)
    monkeypatch.setattr(
        "iwiki_mcp.specification_store.os.fsync",
        lambda _fd: (_ for _ in ()).throw(OSError("secret /private/path")),
    )

    with pytest.raises(OSError):
        store.prepare(_projection())

    assert store.status("payments") == ProjectionStatus(
        domain="payments",
        state=expected_state,
        markdown_revision=None,
        scenario_count=0,
        binding_count=0,
        reason="preparation_failed",
    )


def test_scenario_record_enforces_complete_nonduplicated_phase_semantics():
    scenario = _projection().scenarios[0]
    when = next(item for item in scenario.items if item.phase == "when")
    then = next(item for item in scenario.items if item.phase == "then")
    given = PhaseItemRecord("given", "state", "Pending")
    exception = PhaseItemRecord("then", "exception", "Rejected")

    invalid_items = (
        (then,),
        (when,),
        (when, PhaseItemRecord("when", "action", "Retry"), then),
        (when, then, exception),
        (given, given, when, then),
    )
    for items in invalid_items:
        with pytest.raises(ValueError, match="invalid scenario semantics"):
            replace(scenario, items=items)

    normalized = replace(scenario, items=list(scenario.items))
    assert isinstance(normalized.items, tuple)


def test_resolution_attempt_enforces_state_cardinality_and_coherent_fields():
    attempt = _projection().evidence[0]
    selector = _projection().bindings[0].selector
    invalid = (
        {"targets": ()},
        {"targets": ("a", "b")},
        {"state": "ambiguous", "targets": ("a",)},
        {"state": "unresolved", "targets": ("a",),
         "unresolved_reference": selector},
        {"state": "unresolved", "targets": (),
         "unresolved_reference": selector, "graph_revision": None},
        {"state": "unresolved", "targets": (),
         "unresolved_reference": None},
        {"state": "graph_unavailable", "targets": ("a",),
         "unresolved_reference": selector, "reason": "missing"},
        {"state": "graph_unavailable", "targets": (),
         "unresolved_reference": selector, "reason": None},
    )
    for values in invalid:
        with pytest.raises(ValueError, match="invalid resolution attempt"):
            replace(attempt, **values)


def test_valid_resolution_states_round_trip_exactly():
    projection = _projection()
    attempt = projection.evidence[0]
    selector = projection.bindings[0].selector
    attempts = (
        attempt,
        replace(attempt, state="ambiguous", targets=("a", "b")),
        replace(
            attempt,
            state="unresolved",
            targets=(),
            unresolved_reference=selector,
        ),
        replace(
            attempt,
            state="graph_unavailable",
            targets=(),
            unresolved_reference=selector,
            graph_revision=None,
            reason="missing",
        ),
    )

    for value in attempts:
        current = projection.with_evidence((value,))
        assert decode_jsonl(encode_jsonl(current)) == current


def test_decoder_rejects_scenario_and_resolution_semantic_contradictions():
    rows = [json.loads(line) for line in encode_jsonl(_projection()).splitlines()]
    scenario_index = next(
        index for index, row in enumerate(rows) if row["record"] == "scenario"
    )
    evidence_index = next(
        index for index, row in enumerate(rows) if row["record"] == "evidence"
    )
    no_when = [dict(row) for row in rows]
    no_when[scenario_index] = {
        **no_when[scenario_index],
        "items": [
            item for item in no_when[scenario_index]["items"]
            if item["phase"] != "when"
        ],
    }
    resolved_many = [dict(row) for row in rows]
    resolved_many[evidence_index] = {
        **resolved_many[evidence_index],
        "targets": ["a", "b"],
    }

    for payload in (_canonical_rows(no_when), _canonical_rows(resolved_many)):
        with pytest.raises(ValueError, match="invalid"):
            decode_jsonl(payload)


def test_finding_reason_is_sanitized_at_constructor_and_jsonl_boundary():
    projection = _projection()
    safe = FindingRecord(type="invalid_scenario", reason="malformed_toml")
    unsafe = FindingRecord(
        type="invalid_scenario",
        reason="failed at https://private.example /secret/path",
    )
    projected = replace(projection, findings=(safe, unsafe))
    encoded = encode_jsonl(projected)

    assert safe.reason == "malformed_toml"
    assert unsafe.reason == "invalid_finding_reason"
    assert b"private.example" not in encoded
    assert b"secret/path" not in encoded
    assert decode_jsonl(encoded) == projected


def test_context_reason_allowlists_reject_safe_shaped_secrets():
    projection = _projection()
    selector = projection.bindings[0].selector
    unavailable = replace(
        projection.evidence[0],
        state="graph_unavailable",
        targets=(),
        unresolved_reference=selector,
        graph_revision=None,
        graph_state_fingerprint=_fingerprint(
            "failed", "failed", None
        ),
        reason="database_password",
    )
    finding = FindingRecord(
        type="invalid_scenario", reason="database_password"
    )
    status = ProjectionStatus(
        domain="payments", state="failed", reason="database_password"
    )

    assert unavailable.reason == "failed"
    assert finding.reason == "invalid_finding_reason"
    assert status.reason == "projection_failed"


@pytest.mark.parametrize(
    "reason",
    (
        "not_configured", "disabled", "missing", "dirty", "rebuilding",
        "failed", "stale_graph", "source_unavailable", "not_primary",
        "revision_changed",
    ),
)
def test_graph_unavailable_approved_reason_codes_are_preserved(reason):
    projection = _projection()
    attempt = replace(
        projection.evidence[0],
        state="graph_unavailable",
        targets=(),
        unresolved_reference=projection.bindings[0].selector,
        graph_revision=None,
        graph_state_fingerprint=_fingerprint("failed", reason, None),
        reason=reason,
    )

    assert attempt.reason == reason


@pytest.mark.parametrize(
    "fingerprint",
    ("ready", "sha256:abc", "sha256:" + "A" * 64, "x" * 64),
)
def test_resolution_fingerprint_requires_canonical_sha256(fingerprint):
    with pytest.raises(ValueError, match="invalid graph state fingerprint"):
        replace(_projection().evidence[0], graph_state_fingerprint=fingerprint)


def test_scenario_scalar_bounds_match_engine_contract():
    scenario = _projection().scenarios[0]
    when = next(item for item in scenario.items if item.phase == "when")
    invalid = (
        lambda: replace(scenario, scenario_id="a" * 129),
        lambda: replace(scenario, title="x" * 251),
        lambda: replace(scenario, items=tuple(
            replace(item, name="é" * 513) if item == when else item
            for item in scenario.items
        )),
        lambda: replace(scenario, page_slug="../secret"),
        lambda: replace(scenario, page_slug="specification/./secret"),
        lambda: replace(scenario, anchor="bad/anchor"),
        lambda: replace(scenario, source_hash="not-a-source-hash"),
    )

    for create in invalid:
        with pytest.raises(ValueError):
            create()


def test_binding_digest_and_selector_safety_match_engine_contract():
    binding = _projection().bindings[0]
    invalid = (
        {"binding_id": "spec:binding:" + "0" * 64},
        {"selector": "x" * 4097},
        {"selector_kind": "file", "selector": "../secret.py"},
        {"selector_kind": "file", "selector": "src/*.py"},
        {"selector_kind": "source_glob", "selector": "/src/*.py"},
        {"selector_kind": "source_glob", "selector": "src\\*.py"},
    )
    for values in invalid:
        with pytest.raises(ValueError):
            replace(binding, **values)

    copied = BindingRecord(**binding.__dict__)
    assert copied == binding


def test_shared_identity_and_enum_fields_reject_invalid_public_values():
    projection = _projection()
    binding = projection.bindings[0]
    attempt = projection.evidence[0]

    for create in (
        lambda: replace(binding, scenario_id="a" * 129),
        lambda: replace(attempt, scenario_id="a" * 129),
        lambda: PhaseItemRecord(phase=[], role="event", name="Opened"),
        lambda: replace(binding, relation=[]),
        lambda: replace(attempt, state=[]),
    ):
        with pytest.raises(ValueError):
            create()


def test_projection_rejects_incomplete_scenario_binding_sets_directly_and_on_decode():
    projection = _projection()
    implements = tuple(
        item for item in projection.bindings if item.relation == "implements"
    )

    for bindings in ((), implements):
        with pytest.raises(ValueError, match="bindings"):
            replace(projection, bindings=bindings, evidence=())

    rows = [json.loads(line) for line in encode_jsonl(projection).splitlines()]
    for relations in (set(), {"implements"}):
        invalid = [rows[0], rows[1], *(
            row for row in rows[2:]
            if row["record"] == "binding" and row["relation"] in relations
        )]
        invalid[0]["binding_count"] = len(invalid) - 2
        with pytest.raises(ValueError, match="invalid specification projection"):
            decode_jsonl(_canonical_rows(invalid))


def test_projection_accepts_256_complete_bindings_and_rejects_257():
    projection = _projection()
    scenario = projection.scenarios[0]
    bindings = tuple(
        _numbered_binding(scenario, relation, number)
        for relation in ("implements", "verifies")
        for number in range(128)
    )
    boundary = replace(projection, bindings=bindings, evidence=())

    assert boundary.binding_count == 256
    assert decode_jsonl(encode_jsonl(boundary)) == boundary

    extra = _numbered_binding(scenario, "implements", 128)
    with pytest.raises(ValueError, match="bindings"):
        replace(boundary, bindings=(*bindings, extra))

    rows = [json.loads(line) for line in encode_jsonl(boundary).splitlines()]
    invalid = [rows[0], rows[1], *sorted(
        [*rows[2:], _binding_row(extra)], key=lambda row: row["binding_id"]
    )]
    invalid[0]["binding_count"] = 257
    with pytest.raises(ValueError, match="invalid specification projection"):
        decode_jsonl(_canonical_rows(invalid))


def test_finding_optional_fields_reject_wrong_types_and_decoder_is_stable():
    for values in (
        {"slug": 7},
        {"heading": []},
        {"scenario_id": object()},
        {"reason": {"secret": "/private/path"}},
    ):
        with pytest.raises(ValueError):
            FindingRecord(type="invalid_scenario", **values)

    rows = [json.loads(line) for line in encode_jsonl(_projection()).splitlines()]
    rows[0]["findings"] = [{
        "type": "invalid_scenario",
        "slug": {"secret": "/private/path"},
    }]
    with pytest.raises(ValueError) as caught:
        decode_jsonl(_canonical_rows(rows))
    assert str(caught.value) == "invalid specification projection"


def test_status_and_prepare_contain_path_resolution_failures(tmp_path, monkeypatch):
    (tmp_path / "payments").mkdir()
    store = GitSpecificationStore(str(tmp_path))

    monkeypatch.setattr(
        store, "_path", lambda _domain: (_ for _ in ()).throw(TypeError("secret"))
    )

    assert store.status("payments") == ProjectionStatus(
        domain="payments",
        state="failed",
        reason="projection_failed",
    )
    with pytest.raises(TypeError):
        store.prepare(_projection())
    assert store.status("payments").state == "stale"
    assert store.status("payments").reason == "preparation_failed"
    assert list((tmp_path / "payments").iterdir()) == []


def test_prepared_terminal_failure_does_not_retry_or_repeat_callbacks(
    tmp_path, monkeypatch
):
    (tmp_path / "payments").mkdir()
    store = GitSpecificationStore(str(tmp_path))
    prepared = store.prepare(_projection())
    calls = []

    def fail_replace(*_args):
        calls.append("replace")
        raise OSError("private")

    monkeypatch.setattr("iwiki_mcp.specification_store.os.replace", fail_replace)
    with pytest.raises(OSError):
        prepared.publish()
    prepared.publish()
    prepared.abort()
    prepared.cleanup()

    assert prepared.state == "failed"
    assert calls == ["replace"]
    assert not prepared.temporary_path.exists()


def test_prepared_success_and_abort_are_terminal_and_idempotent(tmp_path, monkeypatch):
    (tmp_path / "payments").mkdir()
    store = GitSpecificationStore(str(tmp_path))
    prepared = store.prepare(_projection())
    real_replace = os.replace
    calls = []

    def observe_replace(*args):
        calls.append("replace")
        real_replace(*args)

    monkeypatch.setattr("iwiki_mcp.specification_store.os.replace", observe_replace)
    prepared.publish()
    prepared.publish()
    prepared.abort()
    assert prepared.state == "published"
    assert calls == ["replace"]

    aborted = store.prepare(_projection())
    aborted.abort()
    aborted.abort()
    aborted.cleanup()
    aborted.publish()
    assert aborted.state == "aborted"
    assert not aborted.temporary_path.exists()


def test_git_store_search_reads_each_repeated_domain_once(monkeypatch):
    projection = _projection()
    store = GitSpecificationStore("/unused")
    calls = []

    def load(domain):
        calls.append(domain)
        return projection

    monkeypatch.setattr(store, "_load", load)

    assert store.search(("payments", "payments"), "open-account", 10)
    assert calls == ["payments"]
