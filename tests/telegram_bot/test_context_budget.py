from iwiki_mcp.telegram_bot.context import (
    ContextBudget,
    Section,
    select_context,
)


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


def _section(slug="guide/deploy", heading="Rollback", body="body"):
    return Section(slug=slug, heading=heading, body=body)


def test_a_section_is_labelled_with_its_page_and_heading():
    text = select_context([_section()], 1000, "rollback")

    assert text == "## guide/deploy - Rollback\nbody"


def test_a_hit_without_a_heading_is_labelled_with_its_page_alone():
    text = select_context([_section(heading="", body="page")], 1000, "page")

    assert text == "## guide/deploy\npage"


def test_every_section_gets_an_even_share_of_the_budget():
    sections = [
        _section(slug="a", heading="A", body="alpha. " * 500),
        _section(slug="b", heading="B", body="beta. " * 500),
        _section(slug="c", heading="C", body="gamma. " * 500),
    ]

    text = select_context(sections, 1800, "alpha")

    assert len(text) <= 1800
    assert "## a - A" in text
    assert "## b - B" in text
    assert "## c - C" in text


def test_a_short_section_hands_its_remainder_to_the_next_one():
    sections = [
        _section(slug="a", heading="A", body="short"),
        _section(slug="b", heading="B", body="long. " * 400),
    ]

    text = select_context(sections, 1200, "long")

    assert len(text) <= 1200
    # The second section receives far more than a naive half of the budget.
    assert len(text.split("## b - B\n")[1]) > 700


def test_trimming_keeps_the_lead_and_the_paragraphs_that_match_the_query():
    body = "\n\n".join(
        [
            "Lead paragraph.",
            "Filler about unrelated matters. " * 10,
            "The rollback procedure restores the previous revision.",
            "More filler about unrelated matters. " * 10,
        ]
    )

    text = select_context([_section(body=body)], 500, "rollback procedure")

    assert len(text) <= 500
    assert "Lead paragraph." in text
    assert "The rollback procedure restores the previous revision." in text
    assert "[...]" in text


def test_a_tiny_allocation_degrades_the_section_to_a_card():
    body = "Lead paragraph about rollback.\n\n" + "filler. " * 200

    text = select_context([_section(body=body)], 120, "rollback")

    assert len(text) <= 120
    assert text.startswith("## guide/deploy - Rollback\n")
    assert "filler." not in text


def test_selection_never_exceeds_the_budget():
    sections = [
        _section(slug=f"page/{index}", heading="H", body="x" * 900)
        for index in range(5)
    ]

    for budget in (60, 200, 1000, 4000):
        assert len(select_context(sections, budget, "x")) <= budget


def test_an_empty_result_produces_no_context():
    assert select_context([], 1000, "anything") == ""
