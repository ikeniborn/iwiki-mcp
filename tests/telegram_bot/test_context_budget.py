from iwiki_mcp.telegram_bot.context import ContextBudget


_QUESTION_CHARS = 200
_RESERVE_TOKENS = 512


def test_budget_fits_the_window_under_the_initial_ratio():
    budget = ContextBudget()

    chars = budget.chars(_QUESTION_CHARS)

    prompt_tokens = (chars + _QUESTION_CHARS) * budget.ratio
    assert prompt_tokens + 1024 + _RESERVE_TOKENS <= 32768


def test_budget_never_exceeds_the_configured_ceiling():
    budget = ContextBudget(ceiling_chars=5000)

    assert budget.chars(_QUESTION_CHARS) == 5000


def test_budget_falls_back_to_the_floor_when_the_window_is_exhausted():
    budget = ContextBudget(window_tokens=1000, output_tokens=1024)

    assert budget.chars(_QUESTION_CHARS) == 4000


def test_calibration_from_usage_grows_the_budget_for_light_content():
    budget = ContextBudget()
    before = budget.chars(_QUESTION_CHARS)

    for _ in range(30):
        budget.observe(10000, 2500)

    assert budget.chars(_QUESTION_CHARS) > before


def test_calibration_never_drops_below_the_ratio_floor():
    budget = ContextBudget()

    for _ in range(50):
        budget.observe(10000, 1)

    assert budget.ratio == 0.25


def test_usage_without_a_reported_prompt_leaves_the_ratio_unchanged():
    budget = ContextBudget()

    budget.observe(0, 100)
    budget.observe(10000, 0)

    assert budget.ratio == 0.75


def test_an_overflow_escalates_the_ratio_and_shrinks_the_budget():
    budget = ContextBudget()
    before = budget.chars(_QUESTION_CHARS)

    budget.escalate()

    assert budget.ratio > 0.75
    assert budget.chars(_QUESTION_CHARS) < before


def test_escalation_is_capped():
    budget = ContextBudget()

    for _ in range(10):
        budget.escalate()

    assert budget.ratio == 1.5
