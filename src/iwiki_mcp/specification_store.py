"""Backend-neutral specification records and the Git JSONL adapter."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import tempfile
from typing import Callable, Literal, Protocol

from . import base


ProjectionState = Literal["disabled", "absent", "ready", "stale", "failed"]
ResolutionState = Literal[
    "resolved", "ambiguous", "unresolved", "graph_unavailable"
]
_PHASES = frozenset({"given", "when", "then"})
_ROLES = {
    "given": frozenset({"event", "state", "fact"}),
    "when": frozenset({"command", "request", "action"}),
    "then": frozenset({"event", "response", "outcome", "exception"}),
}
_RELATIONS = frozenset({"implements", "verifies"})
_SELECTOR_KINDS = frozenset({"symbol", "file", "source_glob"})
_RESOLUTION_STATES = frozenset({
    "resolved", "ambiguous", "unresolved", "graph_unavailable",
})
_PROJECTION_STATES = frozenset({"ready", "stale", "failed"})
_STATUS_STATES = frozenset({"disabled", "absent", *_PROJECTION_STATES})
_SCENARIO_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_CANONICAL_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GRAPH_UNAVAILABLE_REASONS = frozenset({
    "not_configured",
    "disabled",
    "missing",
    "dirty",
    "rebuilding",
    "failed",
    "stale_graph",
    "source_unavailable",
    "not_primary",
    "revision_changed",
})
_PROJECTION_REASONS = frozenset({
    "projection_stale",
    "projection_failed",
    "preparation_failed",
    "publication_failed",
    "out_of_band_change",
})
_FINDING_REASONS = frozenset({
    "invalid_frontmatter",
    "block_outside_h2",
    "multiple_blocks_in_section",
    "unclosed_block",
    "invalid_block_encoding",
    "malformed_toml",
    "invalid_top_level_keys",
    "invalid_id",
    "invalid_title",
    "given_must_be_list",
    "when_must_be_table",
    "then_must_be_nonempty_list",
    "invalid_given_item",
    "invalid_given_role",
    "invalid_given_name",
    "invalid_when_item",
    "invalid_when_role",
    "invalid_when_name",
    "invalid_then_item",
    "invalid_then_role",
    "invalid_then_name",
    "duplicate_phase_item",
    "exception_not_exclusive",
    "code_must_be_nonempty_list",
    "too_many_bindings",
    "invalid_binding",
    "invalid_binding_relation",
    "invalid_binding_phase",
    "binding_requires_one_selector",
    "duplicate_binding",
    "invalid_symbol_selector",
    "invalid_file_selector",
    "unsafe_file_selector",
    "invalid_source_glob_selector",
    "unsafe_source_glob_selector",
    *_PROJECTION_REASONS,
})
_MAX_SELECTOR_BYTES = 4096
_MAX_SELECTOR_SEGMENTS = 256
_MAX_BINDINGS = 256


def _allowlisted_reason(
    reason: object, allowed: frozenset[str], fallback: str
) -> str:
    if type(reason) is str and reason in allowed:
        return reason
    return fallback


def _sanitized_fingerprint(value: str) -> str:
    if type(value) is str and _CANONICAL_FINGERPRINT.fullmatch(value):
        return value
    raise ValueError("invalid graph state fingerprint")


def _collection(value: object, label: str) -> tuple:
    if isinstance(value, (str, bytes, bytearray, dict)):
        raise ValueError(f"invalid {label}")
    try:
        return tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ValueError(f"invalid {label}") from exc


def _text(
    value: object,
    label: str,
    *,
    max_bytes: int | None = None,
    max_codepoints: int | None = None,
) -> str:
    if type(value) is not str or not value.strip() or "\0" in value:
        raise ValueError(f"invalid {label}")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"invalid {label}") from exc
    if max_bytes is not None and len(encoded) > max_bytes:
        raise ValueError(f"invalid {label}")
    if max_codepoints is not None and len(value) > max_codepoints:
        raise ValueError(f"invalid {label}")
    return value


def _safe_relative(value: object, label: str, *, glob: bool = False) -> str:
    text = _text(value, label, max_bytes=_MAX_SELECTOR_BYTES)
    posix = PurePosixPath(text)
    windows = PureWindowsPath(text)
    if (
        text != text.strip()
        or "\\" in text
        or posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or not posix.parts
        or len(posix.parts) > _MAX_SELECTOR_SEGMENTS
        or posix.as_posix() != text
        or any(part in {"", ".", ".."} for part in posix.parts)
        or (not glob and any(character in text for character in "*?["))
    ):
        raise ValueError(f"invalid {label}")
    return text


def _safe_page_slug(value: object, label: str) -> str:
    if type(value) is not str or not value or "\0" in value or "\\" in value:
        raise ValueError(f"invalid {label}")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or not posix.parts
        or any(part in {".", ".."} for part in value.split("/"))
    ):
        raise ValueError(f"invalid {label}")
    return value


def _safe_anchor(value: object, label: str) -> str:
    if type(value) is not str or "\0" in value:
        raise ValueError(f"invalid {label}")
    if (
        "/" in value
        or "\\" in value
        or PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
        or PureWindowsPath(value).drive
        or value in {".", ".."}
    ):
        raise ValueError(f"invalid {label}")
    return value


def _domain(value: object) -> str:
    try:
        return base.validate_domain_identifier(value)  # type: ignore[arg-type]
    except base.BaseError as exc:
        raise ValueError("invalid record domain") from exc


def _scenario_identity(value: object, label: str) -> str:
    if (
        type(value) is not str
        or _SCENARIO_ID.fullmatch(value) is None
        or len(value.encode("utf-8")) > 128
    ):
        raise ValueError(f"invalid {label}")
    return value


@dataclass(frozen=True)
class PhaseItemRecord:
    phase: str
    role: str
    name: str

    def __post_init__(self) -> None:
        if (
            type(self.phase) is not str
            or type(self.role) is not str
            or self.phase not in _PHASES
            or self.role not in _ROLES.get(self.phase, ())
        ):
            raise ValueError("invalid specification phase item")
        _text(self.name, "phase item name", max_bytes=1024)


@dataclass(frozen=True)
class ScenarioLocation:
    slug: str
    heading: str
    anchor: str

    def __post_init__(self) -> None:
        _safe_page_slug(self.slug, "scenario location slug")
        _text(self.heading, "scenario location heading")
        _safe_anchor(self.anchor, "scenario location anchor")


@dataclass(frozen=True)
class ScenarioRecord:
    domain: str
    scenario_id: str
    title: str
    page_slug: str
    heading: str
    anchor: str
    source_hash: str
    items: tuple[PhaseItemRecord, ...]
    page_revision: str | int | None

    def __post_init__(self) -> None:
        _domain(self.domain)
        _scenario_identity(self.scenario_id, "scenario identity")
        _text(self.title, "scenario title", max_codepoints=250)
        _safe_page_slug(self.page_slug, "scenario page slug")
        _text(self.heading, "scenario heading")
        _safe_anchor(self.anchor, "scenario anchor")
        if type(self.source_hash) is not str or _HEX_DIGEST.fullmatch(
            self.source_hash
        ) is None:
            raise ValueError("invalid scenario source hash")
        items = _collection(self.items, "scenario items")
        if not all(isinstance(item, PhaseItemRecord) for item in items):
            raise ValueError("invalid scenario items")
        object.__setattr__(self, "items", items)
        when_items = tuple(item for item in items if item.phase == "when")
        then_items = tuple(item for item in items if item.phase == "then")
        identities = tuple((item.phase, item.role, item.name) for item in items)
        if (
            len(when_items) != 1
            or not then_items
            or len(identities) != len(set(identities))
            or (
                any(item.role == "exception" for item in then_items)
                and len(then_items) != 1
            )
        ):
            raise ValueError("invalid scenario semantics")
        if self.page_revision is not None and type(self.page_revision) not in {str, int}:
            raise ValueError("invalid scenario page revision")

    @property
    def identity(self) -> str:
        return f"{self.domain}#{self.scenario_id}"


@dataclass(frozen=True)
class BindingRecord:
    binding_id: str
    domain: str
    scenario_id: str
    relation: str
    phase: str | None
    selector_kind: str
    selector: str

    def __post_init__(self) -> None:
        _text(self.binding_id, "binding identity")
        _domain(self.domain)
        _scenario_identity(self.scenario_id, "binding scenario identity")
        if type(self.relation) is not str or self.relation not in _RELATIONS:
            raise ValueError("invalid binding relation")
        if self.phase is not None and (
            type(self.phase) is not str or self.phase not in _PHASES
        ):
            raise ValueError("invalid binding phase")
        if (
            type(self.selector_kind) is not str
            or self.selector_kind not in _SELECTOR_KINDS
        ):
            raise ValueError("invalid binding selector kind")
        if self.selector_kind == "symbol":
            if type(self.selector) is not str or not self.selector:
                raise ValueError("invalid binding selector")
            try:
                selector_bytes = self.selector.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("invalid binding selector") from exc
            if len(selector_bytes) > _MAX_SELECTOR_BYTES or "\0" in self.selector:
                raise ValueError("invalid binding selector")
        else:
            _safe_relative(
                self.selector,
                "binding selector",
                glob=self.selector_kind == "source_glob",
            )
        digest = hashlib.sha256("\0".join((
            self.domain,
            self.scenario_id,
            self.relation,
            self.phase or "",
            self.selector_kind,
            self.selector,
        )).encode("utf-8")).hexdigest()
        if self.binding_id != f"spec:binding:{digest}":
            raise ValueError("invalid binding identity")

    @property
    def scenario_identity(self) -> str:
        return f"{self.domain}#{self.scenario_id}"


@dataclass(frozen=True)
class ResolutionAttempt:
    binding_id: str
    domain: str
    scenario_id: str
    state: ResolutionState
    targets: tuple[str, ...]
    unresolved_reference: str | None
    graph_revision: str | None
    graph_state_fingerprint: str
    specification_source_hash: str
    checked_at: str
    reason: str | None

    def __post_init__(self) -> None:
        _text(self.binding_id, "resolution binding identity")
        _domain(self.domain)
        _scenario_identity(self.scenario_id, "resolution scenario identity")
        if type(self.state) is not str or self.state not in _RESOLUTION_STATES:
            raise ValueError("invalid resolution state")
        targets = _collection(self.targets, "resolution targets")
        if not all(type(target) is str and target for target in targets):
            raise ValueError("invalid resolution targets")
        object.__setattr__(self, "targets", tuple(sorted(set(targets))))
        if self.unresolved_reference is not None:
            _text(self.unresolved_reference, "unresolved reference")
        if self.graph_revision is not None:
            _text(self.graph_revision, "graph revision")
        _text(self.specification_source_hash, "specification source hash")
        _text(self.checked_at, "resolution checked time")
        object.__setattr__(
            self,
            "graph_state_fingerprint",
            _sanitized_fingerprint(self.graph_state_fingerprint),
        )
        if self.reason is not None:
            object.__setattr__(
                self,
                "reason",
                _allowlisted_reason(
                    self.reason, _GRAPH_UNAVAILABLE_REASONS, "failed"
                ),
            )
        target_count = len(self.targets)
        ready_state = self.state in {"resolved", "ambiguous", "unresolved"}
        invalid = (
            (self.state == "resolved" and target_count != 1)
            or (self.state == "ambiguous" and target_count <= 1)
            or (self.state in {"unresolved", "graph_unavailable"} and target_count)
            or (ready_state and self.graph_revision is None)
            or (
                self.state in {"resolved", "ambiguous"}
                and self.unresolved_reference is not None
            )
            or (
                self.state in {"unresolved", "graph_unavailable"}
                and self.unresolved_reference is None
            )
            or (self.state == "graph_unavailable" and self.reason is None)
        )
        if invalid:
            raise ValueError("invalid resolution attempt")


@dataclass(frozen=True)
class FindingRecord:
    type: str
    slug: str | None = None
    heading: str | None = None
    scenario_id: str | None = None
    reason: str | None = None
    missing: tuple[str, ...] = ()
    locations: tuple[ScenarioLocation, ...] = ()

    def __post_init__(self) -> None:
        _text(self.type, "finding type")
        if self.slug is not None:
            _safe_page_slug(self.slug, "finding slug")
        if self.heading is not None:
            _text(self.heading, "finding heading")
        if self.scenario_id is not None:
            _scenario_identity(self.scenario_id, "finding scenario identity")
        if self.reason is not None and type(self.reason) is not str:
            raise ValueError("invalid finding reason")
        missing = _collection(self.missing, "finding missing relations")
        if not all(item in _RELATIONS for item in missing):
            raise ValueError("invalid finding missing relations")
        locations = _collection(self.locations, "finding locations")
        if not all(isinstance(item, ScenarioLocation) for item in locations):
            raise ValueError("invalid finding locations")
        object.__setattr__(self, "missing", tuple(sorted(set(missing))))
        object.__setattr__(
            self,
            "locations",
            tuple(sorted(set(locations), key=lambda item: (
                item.slug, item.heading, item.anchor
            ))),
        )
        if self.reason is not None:
            object.__setattr__(
                self,
                "reason",
                _allowlisted_reason(
                    self.reason, _FINDING_REASONS, "invalid_finding_reason"
                ),
            )


@dataclass(frozen=True)
class DomainProjection:
    domain: str
    markdown_revision: str
    scenarios: tuple[ScenarioRecord, ...]
    bindings: tuple[BindingRecord, ...]
    evidence: tuple[ResolutionAttempt, ...]
    findings: tuple[FindingRecord, ...]
    state: ProjectionState = "ready"
    reason: str | None = None

    def __post_init__(self) -> None:
        _domain(self.domain)
        if type(self.markdown_revision) is not str or not self.markdown_revision:
            raise ValueError("invalid specification projection revision")
        if self.state not in _PROJECTION_STATES:
            raise ValueError("invalid specification projection state")
        if self.state == "ready" and self.reason is not None:
            raise ValueError("invalid specification projection reason")
        if self.state in {"stale", "failed"}:
            fallback = (
                "projection_stale" if self.state == "stale" else "projection_failed"
            )
            object.__setattr__(
                self,
                "reason",
                _allowlisted_reason(self.reason, _PROJECTION_REASONS, fallback),
            )
        scenarios = _collection(self.scenarios, "specification scenarios")
        bindings = _collection(self.bindings, "specification bindings")
        evidence = _collection(self.evidence, "specification evidence")
        findings = _collection(self.findings, "specification findings")
        if not all(isinstance(item, ScenarioRecord) for item in scenarios):
            raise ValueError("invalid specification projection scenarios")
        if not all(isinstance(item, BindingRecord) for item in bindings):
            raise ValueError("invalid specification projection bindings")
        if not all(isinstance(item, ResolutionAttempt) for item in evidence):
            raise ValueError("invalid specification projection evidence")
        if not all(isinstance(item, FindingRecord) for item in findings):
            raise ValueError("invalid specification projection findings")
        object.__setattr__(
            self, "scenarios", tuple(sorted(scenarios, key=_scenario_key))
        )
        object.__setattr__(
            self, "bindings", tuple(sorted(bindings, key=_binding_key))
        )
        object.__setattr__(
            self, "evidence", tuple(sorted(evidence, key=_evidence_key))
        )
        object.__setattr__(
            self,
            "findings",
            tuple(sorted(findings, key=lambda item: (
                item.type,
                item.scenario_id or "",
                item.slug or "",
                item.heading or "",
                item.reason or "",
                item.missing,
                tuple((location.slug, location.heading, location.anchor)
                      for location in item.locations),
            ))),
        )
        self._validate_associations()

    def _validate_associations(self) -> None:
        scenarios = {
            (item.domain, item.scenario_id): item for item in self.scenarios
        }
        if (
            len(scenarios) != len(self.scenarios)
            or any(item.domain != self.domain for item in self.scenarios)
        ):
            raise ValueError("invalid specification projection scenarios")
        bindings = {item.binding_id: item for item in self.bindings}
        if (
            len(bindings) != len(self.bindings)
            or any(item.domain != self.domain for item in self.bindings)
            or any((item.domain, item.scenario_id) not in scenarios
                   for item in self.bindings)
        ):
            raise ValueError("invalid specification projection bindings")
        scenario_bindings: dict[tuple[str, str], list[BindingRecord]] = {
            identity: [] for identity in scenarios
        }
        for item in self.bindings:
            scenario_bindings[(item.domain, item.scenario_id)].append(item)
        if any(
            len(items) > _MAX_BINDINGS
            or {item.relation for item in items} != _RELATIONS
            for items in scenario_bindings.values()
        ):
            raise ValueError("invalid specification projection bindings")
        evidence = {item.binding_id: item for item in self.evidence}
        if len(evidence) != len(self.evidence):
            raise ValueError("invalid specification projection evidence")
        for item in self.evidence:
            binding = bindings.get(item.binding_id)
            scenario = scenarios.get((item.domain, item.scenario_id))
            if (
                item.domain != self.domain
                or binding is None
                or scenario is None
                or (binding.domain, binding.scenario_id)
                != (item.domain, item.scenario_id)
                or item.specification_source_hash != scenario.source_hash
            ):
                raise ValueError("invalid specification projection evidence")

    @property
    def scenario_count(self) -> int:
        return len(self.scenarios)

    @property
    def binding_count(self) -> int:
        return len(self.bindings)

    def with_evidence(
        self, evidence: tuple[ResolutionAttempt, ...]
    ) -> "DomainProjection":
        return replace(self, evidence=tuple(sorted(evidence, key=_evidence_key)))


@dataclass(frozen=True)
class ProjectionStatus:
    domain: str
    state: ProjectionState
    markdown_revision: str | None = None
    scenario_count: int = 0
    binding_count: int = 0
    reason: str | None = None

    def __post_init__(self) -> None:
        _domain(self.domain)
        if self.state not in _STATUS_STATES:
            raise ValueError("invalid specification projection status")
        if (
            type(self.scenario_count) is not int
            or self.scenario_count < 0
            or type(self.binding_count) is not int
            or self.binding_count < 0
        ):
            raise ValueError("invalid specification projection status counts")
        if self.state in {"stale", "failed"}:
            fallback = (
                "projection_stale" if self.state == "stale" else "projection_failed"
            )
            object.__setattr__(
                self,
                "reason",
                _allowlisted_reason(self.reason, _PROJECTION_REASONS, fallback),
            )
        elif self.reason is not None:
            raise ValueError("invalid specification projection status reason")
        if self.state in {"disabled", "absent"} and (
            self.markdown_revision is not None
            or self.scenario_count
            or self.binding_count
        ):
            raise ValueError("invalid specification projection status")
        if self.state == "ready" and (
            type(self.markdown_revision) is not str or not self.markdown_revision
        ):
            raise ValueError("invalid specification projection status")


@dataclass(frozen=True)
class ScenarioContext:
    scenario: ScenarioRecord
    bindings: tuple[BindingRecord, ...]
    evidence: tuple[ResolutionAttempt, ...]
    projection_state: ProjectionState
    projection_revision: str
    findings: tuple[FindingRecord, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.scenario, ScenarioRecord):
            raise ValueError("invalid scenario context scenario")
        bindings = _collection(self.bindings, "scenario context bindings")
        evidence = _collection(self.evidence, "scenario context evidence")
        findings = _collection(self.findings, "scenario context findings")
        if not all(isinstance(item, BindingRecord) for item in bindings):
            raise ValueError("invalid scenario context bindings")
        if not all(isinstance(item, ResolutionAttempt) for item in evidence):
            raise ValueError("invalid scenario context evidence")
        if not all(isinstance(item, FindingRecord) for item in findings):
            raise ValueError("invalid scenario context findings")
        if self.projection_state not in _PROJECTION_STATES:
            raise ValueError("invalid scenario context projection state")
        if type(self.projection_revision) is not str or not self.projection_revision:
            raise ValueError("invalid scenario context projection revision")
        object.__setattr__(self, "bindings", tuple(bindings))
        object.__setattr__(self, "evidence", tuple(evidence))
        object.__setattr__(self, "findings", tuple(findings))
        identity = (self.scenario.domain, self.scenario.scenario_id)
        binding_ids = set()
        for binding in self.bindings:
            if (binding.domain, binding.scenario_id) != identity:
                raise ValueError("invalid scenario context binding association")
            binding_ids.add(binding.binding_id)
        if len(binding_ids) != len(self.bindings):
            raise ValueError("invalid scenario context duplicate binding")
        evidence_ids = set()
        for attempt in self.evidence:
            if (
                (attempt.domain, attempt.scenario_id) != identity
                or attempt.binding_id not in binding_ids
                or attempt.specification_source_hash != self.scenario.source_hash
            ):
                raise ValueError("invalid scenario context evidence association")
            evidence_ids.add(attempt.binding_id)
        if len(evidence_ids) != len(self.evidence):
            raise ValueError("invalid scenario context duplicate evidence")


class SpecificationStore(Protocol):
    def replace_projection(self, projection: DomainProjection) -> dict[str, object]: ...

    def search(
        self, domains: tuple[str, ...], query: str, limit: int
    ) -> tuple[ScenarioRecord, ...]: ...

    def context(self, domain: str, scenario_id: str) -> ScenarioContext | None: ...

    def record_resolution(self, attempt: ResolutionAttempt) -> None: ...

    def status(self, domain: str) -> ProjectionStatus: ...


def _scenario_key(record: ScenarioRecord) -> tuple[str, str, str, str]:
    return record.domain, record.scenario_id, record.page_slug, record.heading


def _binding_key(record: BindingRecord) -> tuple[str, str, str]:
    return record.domain, record.scenario_id, record.binding_id


def _evidence_key(record: ResolutionAttempt) -> tuple[str, str, str, str]:
    return record.domain, record.scenario_id, record.binding_id, record.checked_at


def _location_dict(location: ScenarioLocation) -> dict[str, object]:
    return {
        "anchor": location.anchor,
        "heading": location.heading,
        "slug": location.slug,
    }


def _finding_dict(finding: FindingRecord) -> dict[str, object]:
    result: dict[str, object] = {"type": finding.type}
    for name in ("slug", "heading", "scenario_id", "reason"):
        value = getattr(finding, name)
        if value is not None:
            result[name] = value
    if finding.missing:
        result["missing"] = list(finding.missing)
    if finding.locations:
        result["locations"] = [
            _location_dict(location) for location in finding.locations
        ]
    return result


def _scenario_dict(record: ScenarioRecord) -> dict[str, object]:
    return {
        "anchor": record.anchor,
        "domain": record.domain,
        "heading": record.heading,
        "items": [
            {"name": item.name, "phase": item.phase, "role": item.role}
            for item in record.items
        ],
        "page_revision": record.page_revision,
        "page_slug": record.page_slug,
        "record": "scenario",
        "scenario_id": record.scenario_id,
        "source_hash": record.source_hash,
        "title": record.title,
    }


def _binding_dict(record: BindingRecord) -> dict[str, object]:
    return {
        "binding_id": record.binding_id,
        "domain": record.domain,
        "phase": record.phase,
        "record": "binding",
        "relation": record.relation,
        "scenario_id": record.scenario_id,
        "selector": record.selector,
        "selector_kind": record.selector_kind,
    }


def _evidence_dict(record: ResolutionAttempt) -> dict[str, object]:
    return {
        "binding_id": record.binding_id,
        "checked_at": record.checked_at,
        "domain": record.domain,
        "graph_revision": record.graph_revision,
        "graph_state_fingerprint": record.graph_state_fingerprint,
        "reason": record.reason,
        "record": "evidence",
        "scenario_id": record.scenario_id,
        "specification_source_hash": record.specification_source_hash,
        "state": record.state,
        "targets": list(record.targets),
        "unresolved_reference": record.unresolved_reference,
    }


def _json_line(value: dict[str, object]) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) + "\n").encode("utf-8")


def encode_jsonl(projection: DomainProjection) -> bytes:
    """Encode one projection using the canonical format-version-1 order."""
    metadata: dict[str, object] = {
        "binding_count": projection.binding_count,
        "domain": projection.domain,
        "findings": [_finding_dict(item) for item in projection.findings],
        "format_version": 1,
        "markdown_revision": projection.markdown_revision,
        "reason": projection.reason,
        "record": "metadata",
        "scenario_count": projection.scenario_count,
        "state": projection.state,
    }
    rows = [metadata]
    rows.extend(_scenario_dict(item) for item in sorted(
        projection.scenarios, key=_scenario_key
    ))
    rows.extend(_binding_dict(item) for item in sorted(
        projection.bindings, key=_binding_key
    ))
    rows.extend(_evidence_dict(item) for item in sorted(
        projection.evidence, key=_evidence_key
    ))
    return b"".join(_json_line(row) for row in rows)


def _required(row: dict[str, object], name: str, expected: type) -> object:
    value = row.get(name)
    if type(value) is not expected:
        raise ValueError("invalid specification projection")
    return value


def _optional_text(row: dict[str, object], name: str) -> str | None:
    if name not in row:
        return None
    value = row[name]
    if type(value) is not str:
        raise ValueError("invalid specification projection")
    return value


def _finding_record(value: object) -> FindingRecord:
    if not isinstance(value, dict):
        raise ValueError("invalid specification projection")
    locations = value.get("locations", [])
    if not isinstance(locations, list):
        raise ValueError("invalid specification projection")
    missing = value.get("missing", [])
    if not isinstance(missing, list) or not all(
        type(item) is str for item in missing
    ):
        raise ValueError("invalid specification projection")
    location_records: list[ScenarioLocation] = []
    for item in locations:
        if not isinstance(item, dict):
            raise ValueError("invalid specification projection")
        location_records.append(ScenarioLocation(
            slug=str(_required(item, "slug", str)),
            heading=str(_required(item, "heading", str)),
            anchor=str(_required(item, "anchor", str)),
        ))
    return FindingRecord(
        type=str(_required(value, "type", str)),
        slug=_optional_text(value, "slug"),
        heading=_optional_text(value, "heading"),
        scenario_id=_optional_text(value, "scenario_id"),
        reason=_optional_text(value, "reason"),
        missing=tuple(missing),
        locations=tuple(location_records),
    )


def _decode_jsonl(payload: bytes | str) -> DomainProjection:
    """Decode and validate one canonical logical projection."""
    try:
        text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        rows = [json.loads(line) for line in text.splitlines() if line]
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid specification projection") from exc
    if not rows or not all(isinstance(row, dict) for row in rows):
        raise ValueError("invalid specification projection")
    metadata = rows[0]
    if metadata.get("record") != "metadata" or metadata.get("format_version") != 1:
        raise ValueError("unsupported specification projection format")
    domain = str(_required(metadata, "domain", str))
    revision = str(_required(metadata, "markdown_revision", str))
    scenarios: list[ScenarioRecord] = []
    bindings: list[BindingRecord] = []
    evidence: list[ResolutionAttempt] = []
    record_order = {"scenario": 0, "binding": 1, "evidence": 2}
    previous_order = -1
    for row in rows[1:]:
        kind = row.get("record")
        if kind not in record_order or record_order[kind] < previous_order:
            raise ValueError("invalid specification projection record order")
        previous_order = record_order[kind]
        if kind == "scenario":
            items = row.get("items")
            if not isinstance(items, list):
                raise ValueError("invalid specification projection")
            scenarios.append(ScenarioRecord(
                domain=str(_required(row, "domain", str)),
                scenario_id=str(_required(row, "scenario_id", str)),
                title=str(_required(row, "title", str)),
                page_slug=str(_required(row, "page_slug", str)),
                heading=str(_required(row, "heading", str)),
                anchor=str(_required(row, "anchor", str)),
                source_hash=str(_required(row, "source_hash", str)),
                items=tuple(PhaseItemRecord(
                    phase=str(_required(item, "phase", str)),
                    role=str(_required(item, "role", str)),
                    name=str(_required(item, "name", str)),
                ) for item in items if isinstance(item, dict)),
                page_revision=row.get("page_revision"),
            ))
        elif kind == "binding":
            bindings.append(BindingRecord(
                binding_id=str(_required(row, "binding_id", str)),
                domain=str(_required(row, "domain", str)),
                scenario_id=str(_required(row, "scenario_id", str)),
                relation=str(_required(row, "relation", str)),
                phase=row.get("phase") if isinstance(row.get("phase"), str) else None,
                selector_kind=str(_required(row, "selector_kind", str)),
                selector=str(_required(row, "selector", str)),
            ))
        else:
            targets = row.get("targets")
            if not isinstance(targets, list):
                raise ValueError("invalid specification projection")
            evidence.append(ResolutionAttempt(
                binding_id=str(_required(row, "binding_id", str)),
                domain=str(_required(row, "domain", str)),
                scenario_id=str(_required(row, "scenario_id", str)),
                state=str(_required(row, "state", str)),  # type: ignore[arg-type]
                targets=tuple(str(item) for item in targets),
                unresolved_reference=(
                    row.get("unresolved_reference")
                    if isinstance(row.get("unresolved_reference"), str) else None
                ),
                graph_revision=(
                    row.get("graph_revision")
                    if isinstance(row.get("graph_revision"), str) else None
                ),
                graph_state_fingerprint=str(
                    _required(row, "graph_state_fingerprint", str)
                ),
                specification_source_hash=str(
                    _required(row, "specification_source_hash", str)
                ),
                checked_at=str(_required(row, "checked_at", str)),
                reason=row.get("reason") if isinstance(row.get("reason"), str) else None,
            ))
    findings_value = metadata.get("findings", [])
    if not isinstance(findings_value, list):
        raise ValueError("invalid specification projection")
    projection = DomainProjection(
        domain=domain,
        markdown_revision=revision,
        scenarios=tuple(scenarios),
        bindings=tuple(bindings),
        evidence=tuple(evidence),
        findings=tuple(_finding_record(item) for item in findings_value),
        state=str(metadata.get("state", "ready")),  # type: ignore[arg-type]
        reason=(metadata.get("reason")
                if isinstance(metadata.get("reason"), str) else None),
    )
    if (
        projection.scenario_count != metadata.get("scenario_count")
        or projection.binding_count != metadata.get("binding_count")
        or any(item.domain != domain for item in (
            *projection.scenarios, *projection.bindings, *projection.evidence
        ))
    ):
        raise ValueError("invalid specification projection counts")
    if encode_jsonl(projection) != (
        payload if isinstance(payload, bytes) else payload.encode("utf-8")
    ):
        raise ValueError("noncanonical specification projection")
    return projection


def decode_jsonl(payload: bytes | str) -> DomainProjection:
    """Decode one projection without exposing payload-specific validation errors."""
    try:
        return _decode_jsonl(payload)
    except (AttributeError, TypeError, UnicodeError, ValueError):
        raise ValueError("invalid specification projection") from None


@dataclass
class PreparedProjectionReplace:
    target_path: Path
    temporary_path: Path
    _on_publish: Callable[[], None]
    _on_failure: Callable[[], None]
    _state: Literal["pending", "published", "failed", "aborted"] = "pending"

    @property
    def state(self) -> str:
        return self._state

    def _unlink(self) -> None:
        try:
            self.temporary_path.unlink()
        except FileNotFoundError:
            pass

    def publish(self) -> None:
        if self._state != "pending":
            return
        try:
            os.replace(self.temporary_path, self.target_path)
        except BaseException:
            self._state = "failed"
            try:
                self._unlink()
            finally:
                self._on_failure()
            raise
        self._state = "published"
        self._on_publish()

    def abort(self) -> None:
        if self._state != "pending":
            return
        self._state = "aborted"
        self._unlink()

    def cleanup(self) -> None:
        self.abort()


class GitSpecificationStore:
    """Read and prepare canonical Git projection files without owning Git locks."""

    def __init__(self, wiki_base: str, mode: str = "optional"):
        if mode not in {"disabled", "optional", "strict"}:
            raise ValueError("invalid specification mode")
        self.base = wiki_base
        self.mode = mode
        self._status: dict[str, tuple[ProjectionState, str | None]] = {}

    def _path(self, domain: str) -> Path:
        return Path(base.specifications_path(self.base, domain))

    def _failure_state(self) -> ProjectionState:
        return "stale" if self.mode == "optional" else "failed"

    def _load(self, domain: str) -> DomainProjection | None:
        if self.mode == "disabled":
            return None
        path = self._path(domain)
        try:
            with open(path, "rb") as handle:
                return decode_jsonl(handle.read())
        except FileNotFoundError:
            return None

    def prepare(self, projection: DomainProjection) -> PreparedProjectionReplace:
        if self.mode == "disabled":
            raise RuntimeError("specifications are disabled")
        temporary_name: str | None = None
        descriptor: int | None = None
        try:
            target = self._path(projection.domain)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".specifications-", suffix=".tmp", dir=target.parent
            )
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(encode_jsonl(projection))
                handle.flush()
                os.fsync(handle.fileno())
            return PreparedProjectionReplace(
                target,
                Path(temporary_name),
                lambda: self._status.pop(projection.domain, None),
                lambda: self._status.__setitem__(
                    projection.domain,
                    (self._failure_state(), "publication_failed"),
                ),
            )
        except BaseException:
            self._status[projection.domain] = (
                self._failure_state(), "preparation_failed"
            )
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
            raise

    def replace_projection(self, projection: DomainProjection) -> dict[str, object]:
        if self.mode == "disabled":
            return {"state": "disabled"}
        prepared = self.prepare(projection)
        try:
            prepared.publish()
        except BaseException:
            prepared.abort()
            raise
        self._status.pop(projection.domain, None)
        return {
            "state": "ready",
            "scenarios": projection.scenario_count,
            "bindings": projection.binding_count,
        }

    def search(
        self, domains: tuple[str, ...], query: str, limit: int
    ) -> tuple[ScenarioRecord, ...]:
        if self.mode == "disabled":
            return ()
        from .specifications import search_projections

        projections = tuple(
            projection for domain in dict.fromkeys(domains)
            if (projection := self._load(domain)) is not None
        )
        return search_projections(projections, query, limit)

    def context(self, domain: str, scenario_id: str) -> ScenarioContext | None:
        if self.mode == "disabled":
            return None
        from .specifications import projection_context

        projection = self._load(domain)
        return None if projection is None else projection_context(
            projection, scenario_id
        )

    def record_resolution(self, attempt: ResolutionAttempt) -> None:
        if self.mode == "disabled":
            return
        projection = self._load(attempt.domain)
        if projection is None:
            raise ValueError("specification projection not found")
        binding = next((item for item in projection.bindings
                        if (
                            item.domain,
                            item.scenario_id,
                            item.binding_id,
                        ) == (
                            attempt.domain,
                            attempt.scenario_id,
                            attempt.binding_id,
                        )), None)
        scenario = next((item for item in projection.scenarios
                         if (
                             item.domain,
                             item.scenario_id,
                         ) == (
                             attempt.domain,
                             attempt.scenario_id,
                         )), None)
        if (
            binding is None
            or scenario is None
            or attempt.specification_source_hash != scenario.source_hash
        ):
            raise ValueError("resolution attempt does not match projection")
        evidence = {
            (item.domain, item.scenario_id, item.binding_id): item
            for item in projection.evidence
            if (
                item.domain, item.scenario_id, item.binding_id
            ) != (
                attempt.domain, attempt.scenario_id, attempt.binding_id
            )
        }
        evidence[(attempt.domain, attempt.scenario_id, attempt.binding_id)] = attempt
        self.replace_projection(projection.with_evidence(tuple(evidence.values())))

    def status(self, domain: str) -> ProjectionStatus:
        if self.mode == "disabled":
            try:
                return ProjectionStatus(domain=domain, state="disabled")
            except (TypeError, ValueError):
                return ProjectionStatus(domain="invalid", state="disabled")
        try:
            override = self._status.get(domain)
            safe_domain = base.validate_domain_identifier(domain)
        except (base.BaseError, TypeError, ValueError):
            return ProjectionStatus(
                domain="invalid", state="failed", reason="projection_failed"
            )
        projection: DomainProjection | None = None
        try:
            projection = self._load(domain)
        except (base.BaseError, OSError, TypeError, ValueError):
            if override is not None:
                state, reason = override
                return ProjectionStatus(
                    domain=safe_domain,
                    state=state,
                    reason=reason,
                )
            return ProjectionStatus(
                domain=safe_domain, state="failed", reason="projection_failed"
            )
        if override is not None:
            state, reason = override
            return ProjectionStatus(
                domain=safe_domain,
                state=state,
                markdown_revision=(projection.markdown_revision
                                   if projection is not None else None),
                scenario_count=(projection.scenario_count if projection else 0),
                binding_count=(projection.binding_count if projection else 0),
                reason=reason,
            )
        if projection is None:
            return ProjectionStatus(domain=safe_domain, state="absent")
        return ProjectionStatus(
            domain=safe_domain,
            state=projection.state,
            markdown_revision=projection.markdown_revision,
            scenario_count=projection.scenario_count,
            binding_count=projection.binding_count,
            reason=projection.reason,
        )

    def mark_stale(self, domain: str, reason: str = "projection_stale") -> None:
        if self.mode != "disabled":
            self._status[domain] = (
                "stale",
                _allowlisted_reason(
                    reason, _PROJECTION_REASONS, "projection_stale"
                ),
            )

    def mark_failed(self, domain: str, reason: str = "projection_failed") -> None:
        if self.mode != "disabled":
            self._status[domain] = (
                "failed",
                _allowlisted_reason(
                    reason, _PROJECTION_REASONS, "projection_failed"
                ),
            )
