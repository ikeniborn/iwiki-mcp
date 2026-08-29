"""Parse explicit ``iwiki-gwt`` specification pages into immutable records."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import PurePosixPath, PureWindowsPath
import re
from typing import Literal, Mapping

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 only
    import tomli as tomllib

from . import frontmatter
from .links import slugify_heading


Phase = Literal["given", "when", "then"]
Relation = Literal["implements", "verifies"]
SelectorKind = Literal["symbol", "file", "source_glob"]

_SCENARIO_KEYS = frozenset({"id", "title", "given", "when", "then", "code"})
_ITEM_KEYS = frozenset({"role", "name"})
_BINDING_KEYS = frozenset({
    "relation", "phase", "symbol", "file", "source_glob",
})
_SELECTOR_KEYS = ("symbol", "file", "source_glob")
_ROLES = {
    "given": frozenset({"event", "state", "fact"}),
    "when": frozenset({"command", "request", "action"}),
    "then": frozenset({"event", "response", "outcome", "exception"}),
}
_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_H2 = re.compile(r"^ {0,3}##(?!#)[ \t]+(.+?)[ \t]*(?:\r?\n)?$")
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)(?:\r?\n)?$")
_MAX_SELECTOR_BYTES = 4096
_MAX_SELECTOR_SEGMENTS = 256
_MAX_BINDINGS = 256


@dataclass(frozen=True)
class PhaseItem:
    phase: Phase
    role: str
    name: str


@dataclass(frozen=True)
class SpecificationBinding:
    binding_id: str
    relation: Relation
    phase: Phase | None
    selector_kind: SelectorKind
    selector: str


@dataclass(frozen=True)
class Scenario:
    domain: str
    scenario_id: str
    title: str
    slug: str
    heading: str
    anchor: str
    source_hash: str
    items: tuple[PhaseItem, ...]
    bindings: tuple[SpecificationBinding, ...]

    @property
    def identity(self) -> str:
        return f"{self.domain}#{self.scenario_id}"


@dataclass(frozen=True)
class ParseResult:
    scenarios: tuple[Scenario, ...]
    findings: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class _ScenarioFence:
    section: int | None
    heading: str | None
    text: str
    closed: bool


class _GrammarError(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _invalid(slug: str, heading: str | None, reason: str) -> dict[str, object]:
    finding: dict[str, object] = {
        "type": "invalid_scenario",
        "slug": slug,
    }
    if heading is not None:
        finding["heading"] = heading
    finding["reason"] = reason
    return finding


def _declared_page_type(markdown: str) -> object:
    match = _FRONTMATTER.match(markdown)
    if match is None:
        return None
    declared: object = None
    for line in match.group(1).splitlines():
        if not line or line.startswith((" ", "\t")) or ":" not in line:
            continue
        key, _, value = line.partition(":")
        if key.strip() != "type":
            continue
        parsed, _ = frontmatter.split(f"---\ntype: {value.strip()}\n---\n")
        declared = parsed.get("type")
    return declared


def _closing_fence(line: str, marker: str) -> bool:
    character = re.escape(marker[0])
    return re.fullmatch(
        rf" {{0,3}}{character}{{{len(marker)},}}[ \t]*(?:\r?\n)?",
        line,
    ) is not None


def _scenario_fences(body: str) -> tuple[_ScenarioFence, ...]:
    fences: list[_ScenarioFence] = []
    heading: str | None = None
    section: int | None = None
    section_count = 0
    marker: str | None = None
    target = False
    block_lines: list[str] = []
    target_section: int | None = None
    target_heading: str | None = None

    for line in body.splitlines(keepends=True):
        if marker is not None:
            if _closing_fence(line, marker):
                if target:
                    fences.append(_ScenarioFence(
                        target_section,
                        target_heading,
                        "".join(block_lines).replace("\r\n", "\n").replace("\r", "\n"),
                        True,
                    ))
                marker = None
                target = False
                block_lines = []
                continue
            if target:
                block_lines.append(line)
            continue

        heading_match = _H2.fullmatch(line)
        if heading_match is not None:
            section_count += 1
            section = section_count
            heading = heading_match.group(1).strip()
            continue

        fence_match = _FENCE_OPEN.fullmatch(line)
        if fence_match is None:
            continue
        marker = fence_match.group(1)
        target = fence_match.group(2).strip() == "iwiki-gwt"
        if target:
            target_section = section
            target_heading = heading
            block_lines = []

    if marker is not None and target:
        fences.append(_ScenarioFence(
            target_section,
            target_heading,
            "".join(block_lines).replace("\r\n", "\n").replace("\r", "\n"),
            False,
        ))
    return tuple(fences)


def _string(value: object, reason: str, *, max_bytes: int | None = None,
            max_codepoints: int | None = None) -> str:
    if type(value) is not str or not value.strip() or "\0" in value:
        raise _GrammarError(reason)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _GrammarError(reason) from exc
    if max_bytes is not None and len(encoded) > max_bytes:
        raise _GrammarError(reason)
    if max_codepoints is not None and len(value) > max_codepoints:
        raise _GrammarError(reason)
    return value


def _phase_item(value: object, phase: Phase) -> PhaseItem:
    if not isinstance(value, Mapping) or set(value) != _ITEM_KEYS:
        raise _GrammarError(f"invalid_{phase}_item")
    role = value["role"]
    if type(role) is not str or role not in _ROLES[phase]:
        raise _GrammarError(f"invalid_{phase}_role")
    name = _string(value["name"], f"invalid_{phase}_name", max_bytes=1024)
    return PhaseItem(phase, role, name)


def _phase_items(data: Mapping[str, object]) -> tuple[PhaseItem, ...]:
    given = data["given"]
    when = data["when"]
    then = data["then"]
    if type(given) is not list:
        raise _GrammarError("given_must_be_list")
    if not isinstance(when, Mapping):
        raise _GrammarError("when_must_be_table")
    if type(then) is not list or not then:
        raise _GrammarError("then_must_be_nonempty_list")

    items = [*(_phase_item(item, "given") for item in given)]
    items.append(_phase_item(when, "when"))
    items.extend(_phase_item(item, "then") for item in then)
    identities = [(item.phase, item.role, item.name) for item in items]
    if len(identities) != len(set(identities)):
        raise _GrammarError("duplicate_phase_item")
    then_items = [item for item in items if item.phase == "then"]
    if any(item.role == "exception" for item in then_items) and len(then_items) != 1:
        raise _GrammarError("exception_not_exclusive")
    return tuple(items)


def _selector_text(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise _GrammarError(f"invalid_{label}_selector")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _GrammarError(f"invalid_{label}_selector") from exc
    if len(encoded) > _MAX_SELECTOR_BYTES or "\0" in value:
        raise _GrammarError(f"invalid_{label}_selector")
    return value


def _relative_selector(value: object, label: str, *, glob: bool = False) -> str:
    text = _selector_text(value, label)
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
    ):
        raise _GrammarError(f"unsafe_{label}_selector")
    if not glob and any(character in text for character in "*?["):
        raise _GrammarError(f"invalid_{label}_selector")
    return posix.as_posix()


def _binding_id(domain: str, scenario_id: str, relation: str,
                phase: str | None, selector_kind: str, selector: str) -> str:
    digest = hashlib.sha256("\0".join((
        domain,
        scenario_id,
        relation,
        phase or "",
        selector_kind,
        selector,
    )).encode("utf-8")).hexdigest()
    return f"spec:binding:{digest}"


def _bindings(data: Mapping[str, object], domain: str,
              scenario_id: str) -> tuple[SpecificationBinding, ...]:
    code = data["code"]
    if type(code) is not list or not code:
        raise _GrammarError("code_must_be_nonempty_list")
    if len(code) > _MAX_BINDINGS:
        raise _GrammarError("too_many_bindings")
    bindings: list[SpecificationBinding] = []
    identities: set[tuple[str, str | None, str, str]] = set()
    for value in code:
        if not isinstance(value, Mapping) or set(value) - _BINDING_KEYS:
            raise _GrammarError("invalid_binding")
        relation = value.get("relation")
        if type(relation) is not str or relation not in {"implements", "verifies"}:
            raise _GrammarError("invalid_binding_relation")
        phase = value.get("phase")
        if phase is not None and (
            type(phase) is not str or phase not in {"given", "when", "then"}
        ):
            raise _GrammarError("invalid_binding_phase")
        selector_keys = [key for key in _SELECTOR_KEYS if key in value]
        if len(selector_keys) != 1:
            raise _GrammarError("binding_requires_one_selector")
        selector_kind = selector_keys[0]
        if set(value) != {"relation", selector_kind} | (
            {"phase"} if "phase" in value else set()
        ):
            raise _GrammarError("invalid_binding")
        selector = (
            _selector_text(value[selector_kind], "symbol")
            if selector_kind == "symbol"
            else _relative_selector(
                value[selector_kind],
                selector_kind,
                glob=selector_kind == "source_glob",
            )
        )
        identity = (relation, phase, selector_kind, selector)
        if identity in identities:
            raise _GrammarError("duplicate_binding")
        identities.add(identity)
        bindings.append(SpecificationBinding(
            _binding_id(
                domain, scenario_id, relation, phase, selector_kind, selector,
            ),
            relation,
            phase,
            selector_kind,
            selector,
        ))
    return tuple(bindings)


def _parse_scenario(domain: str, slug: str, fence: _ScenarioFence) -> Scenario:
    try:
        block_bytes = fence.text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _GrammarError("invalid_block_encoding") from exc
    try:
        data = tomllib.loads(fence.text)
    except (tomllib.TOMLDecodeError, UnicodeError) as exc:
        raise _GrammarError("malformed_toml") from exc
    if not isinstance(data, Mapping) or set(data) != _SCENARIO_KEYS:
        raise _GrammarError("invalid_top_level_keys")
    scenario_id = _string(data["id"], "invalid_id", max_bytes=128)
    if _ID.fullmatch(scenario_id) is None:
        raise _GrammarError("invalid_id")
    title = _string(data["title"], "invalid_title", max_codepoints=250)
    items = _phase_items(data)
    bindings = _bindings(data, domain, scenario_id)
    heading = fence.heading or ""
    return Scenario(
        domain=domain,
        scenario_id=scenario_id,
        title=title,
        slug=slug,
        heading=heading,
        anchor=slugify_heading(heading),
        source_hash=hashlib.sha256(block_bytes).hexdigest(),
        items=items,
        bindings=bindings,
    )


def parse_specification_page(domain: str, slug: str, markdown: str) -> ParseResult:
    """Return scenarios and sanitized findings for one explicit specification page."""
    page_type = _declared_page_type(markdown)
    if (
        type(page_type) is not str
        or frontmatter.normalize_type(page_type) != "specification"
    ):
        return ParseResult((), ())
    try:
        _, body = frontmatter.split(markdown, strict_code=True)
    except frontmatter.FrontmatterError:
        return ParseResult((), ({
            "type": "invalid_scenario",
            "slug": slug,
            "reason": "invalid_frontmatter",
        },))

    fences = _scenario_fences(body)
    if not fences:
        return ParseResult((), ({"type": "missing_scenario", "slug": slug},))

    findings: list[dict[str, object]] = []
    scenarios: list[Scenario] = []
    blocked_sections = {
        section
        for section in {fence.section for fence in fences if fence.section is not None}
        if sum(fence.section == section for fence in fences) > 1
    }
    reported_sections: set[int] = set()
    for fence in fences:
        if fence.section is None:
            findings.append(_invalid(slug, None, "block_outside_h2"))
            continue
        if fence.section in blocked_sections:
            if fence.section not in reported_sections:
                findings.append(_invalid(
                    slug, fence.heading, "multiple_blocks_in_section",
                ))
                reported_sections.add(fence.section)
            continue
        if not fence.closed:
            findings.append(_invalid(slug, fence.heading, "unclosed_block"))
            continue
        try:
            scenario = _parse_scenario(domain, slug, fence)
        except _GrammarError as exc:
            findings.append(_invalid(slug, fence.heading, exc.reason))
            continue
        scenarios.append(scenario)
        relations = {binding.relation for binding in scenario.bindings}
        missing = tuple(
            relation for relation in ("implements", "verifies")
            if relation not in relations
        )
        if missing:
            findings.append({
                "type": "incomplete_bindings",
                "slug": slug,
                "heading": scenario.heading,
                "scenario_id": scenario.scenario_id,
                "missing": missing,
            })
    return ParseResult(tuple(scenarios), tuple(findings))
