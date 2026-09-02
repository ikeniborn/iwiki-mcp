"""Context budget arithmetic for the chat model window."""

import math


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
