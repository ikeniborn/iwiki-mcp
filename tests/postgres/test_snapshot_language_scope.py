"""Language scope a hosted read derives from the published snapshot header."""
from __future__ import annotations

from iwiki_mcp.postgres.codegraph import snapshot_language_scope


def test_header_languages_are_intersected_with_known_languages():
    assert snapshot_language_scope(
        {"languages": ["javascript", "python"]}
    ) == (("javascript", "python"), ())


def test_language_unknown_to_this_build_is_dropped_and_reported():
    known, unknown = snapshot_language_scope(
        {"languages": ["python", "brainfuck"]}
    )

    assert known == ("python",)
    assert unknown == ("brainfuck",)


def test_absent_or_malformed_header_languages_yield_no_scope():
    assert snapshot_language_scope({}) == ((), ())
    assert snapshot_language_scope({"languages": "python"}) == ((), ())
    assert snapshot_language_scope(None) == ((), ())
