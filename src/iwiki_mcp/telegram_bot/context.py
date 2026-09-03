"""Context budget arithmetic and selection for the chat model window."""

from dataclasses import dataclass
import math
import re


# Tokens per character. The initial value is the dense-Markdown worst case:
# hashes, JSON fragments and identifiers tokenize near 1.3 characters per
# token, while ordinary English prose runs near 3.8.
_INITIAL_RATIO = 0.75
_MIN_RATIO = 0.25
_MAX_RATIO = 1.5
# Calibration is an estimate over mixed content; the margin covers the spread.
_RATIO_SAFETY = 1.25
_OVERFLOW_FACTOR = 1.5
_EWMA_WEIGHT = 0.2
# The window also carries the chat template and the role scaffolding, which no
# character count of the prompt text sees.
_RESERVE_TOKENS = 512
_MIN_BUDGET_CHARS = 4000
# Keep in sync with config.BotConfig.context_window_tokens, .max_output_tokens
# and .context_budget_chars.
_DEFAULT_WINDOW_TOKENS = 32768
_DEFAULT_OUTPUT_TOKENS = 1024
_DEFAULT_CEILING_CHARS = 48000
# Authored section leads are capped at 250 characters, so an allocation below
# that cannot hold a useful excerpt and contributes a card instead.
_MIN_EXCERPT_CHARS = 250
_SEPARATOR = "\n\n"
_ELISION = "[...]"
_TERM = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True)
class Section:
    """One retrieved wiki section, with the page and heading that named it."""

    slug: str
    heading: str
    body: str


@dataclass(frozen=True)
class Selection:
    """The assembled context and how much of the retrieval reached it."""

    text: str
    total: int
    full: int

    @property
    def truncated(self) -> bool:
        return self.full < self.total


class ContextBudget:
    """Derive the retrieval budget from the window, calibrated by usage."""

    def __init__(
        self,
        *,
        window_tokens: int = _DEFAULT_WINDOW_TOKENS,
        output_tokens: int = _DEFAULT_OUTPUT_TOKENS,
        ceiling_chars: int = _DEFAULT_CEILING_CHARS,
    ) -> None:
        self._window_tokens = window_tokens
        self._output_tokens = output_tokens
        self._ceiling_chars = ceiling_chars
        self._ratio = _INITIAL_RATIO

    @property
    def ratio(self) -> float:
        return self._ratio

    def chars(self, fixed_chars: int) -> int:
        """Characters of wiki context that fit beside the prompt and output."""
        available = (
            self._window_tokens
            - self._output_tokens
            - _RESERVE_TOKENS
            - math.ceil(fixed_chars * self._ratio)
        )
        if available <= 0:
            return min(self._ceiling_chars, _MIN_BUDGET_CHARS)
        derived = int(available / (self._ratio * _RATIO_SAFETY))
        # The configured ceiling is a hard cap and wins over the floor.
        return min(self._ceiling_chars, max(_MIN_BUDGET_CHARS, derived))

    def observe(self, prompt_chars: int, prompt_tokens: int) -> None:
        """Calibrate from one completion the provider reported usage for."""
        if prompt_chars <= 0 or prompt_tokens <= 0:
            return
        blended = (1 - _EWMA_WEIGHT) * self._ratio + _EWMA_WEIGHT * (
            prompt_tokens / prompt_chars
        )
        self._ratio = min(_MAX_RATIO, max(_MIN_RATIO, blended))

    def escalate(self) -> None:
        """Raise the estimate after the provider refused an assembled prompt."""
        self._ratio = min(_MAX_RATIO, self._ratio * _OVERFLOW_FACTOR)


def _terms(query: str) -> frozenset[str]:
    return frozenset(match.group().casefold() for match in _TERM.finditer(query))


def _overlap(text: str, terms: frozenset[str]) -> int:
    if not terms:
        return 0
    return len(terms & _terms(text))


def _label(section: Section) -> str:
    if section.heading:
        return f"## {section.slug} - {section.heading}"
    return f"## {section.slug}"


def _paragraphs(body: str) -> list[str]:
    return [part.strip() for part in body.split(_SEPARATOR) if part.strip()]


def _trim(body: str, terms: frozenset[str], limit: int) -> str:
    """Keep the lead plus the paragraphs that answer the query, in order."""
    paragraphs = _paragraphs(body)
    if not paragraphs:
        return body[:limit]
    if len(paragraphs[0]) >= limit:
        return paragraphs[0][:limit]
    kept = {0}
    used = len(paragraphs[0])
    # An addition may need both a separator and an elision marker; charging
    # both keeps the estimate on the safe side of the allocation.
    overhead = 2 * len(_SEPARATOR) + len(_ELISION)
    ranked = sorted(
        range(1, len(paragraphs)),
        key=lambda index: (-_overlap(paragraphs[index], terms), index),
    )
    for index in ranked:
        if not _overlap(paragraphs[index], terms):
            break
        cost = len(paragraphs[index]) + overhead
        if used + cost > limit:
            continue
        kept.add(index)
        used += cost
    parts: list[str] = []
    previous = -1
    for index in sorted(kept):
        if index != previous + 1:
            parts.append(_ELISION)
        parts.append(paragraphs[index])
        previous = index
    if previous != len(paragraphs) - 1:
        parts.append(_ELISION)
    return _SEPARATOR.join(parts)[:limit]


def _render(
    section: Section, share: int, terms: frozenset[str]
) -> tuple[str, bool] | None:
    """Fit one labelled section into its share, and say whether it fit whole."""
    label = _label(section)
    body_budget = share - len(label) - 1
    if body_budget <= 0:
        return None
    if len(section.body) <= body_budget:
        return f"{label}\n{section.body}", True
    if body_budget < _MIN_EXCERPT_CHARS:
        # Too little room for an excerpt: contribute a card, so the model
        # still learns the section exists.
        lead = _paragraphs(section.body)[:1] or [section.body]
        return f"{label}\n{lead[0][:body_budget]}", False
    return f"{label}\n{_trim(section.body, terms, body_budget)}", False


def select_context(
    sections: list[Section], budget: int, query: str
) -> Selection:
    """Assemble labelled sections in rank order within an even share each.

    Each section is offered an even split of the budget still unspent, so a
    short section hands its remainder to the sections behind it and a long one
    cannot starve them.
    """
    terms = _terms(query)
    blocks: list[str] = []
    remaining = budget
    full = 0
    for index, section in enumerate(sections):
        separator = len(_SEPARATOR) if blocks else 0
        share = (remaining - separator) // (len(sections) - index)
        rendered = _render(section, share, terms)
        if rendered is None:
            continue
        block, complete = rendered
        blocks.append(block)
        full += 1 if complete else 0
        remaining -= separator + len(block)
    return Selection(
        text=_SEPARATOR.join(blocks), total=len(sections), full=full
    )
