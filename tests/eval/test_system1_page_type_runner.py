import json

import pytest

from eval.system1_page_type.runner import (
    BenchmarkCase,
    BenchmarkError,
    load_corpus,
    run_benchmark,
)
from iwiki_mcp.engine.system1 import PageTypeDecision


def _probabilities(page_type):
    return {
        candidate: (0.90 if candidate == page_type else 0.02)
        for candidate in (
            "architecture",
            "api",
            "guide",
            "reference",
            "runbook",
            "concept",
        )
    }


def test_runner_captures_baseline_before_system1_and_returns_aggregate_only():
    cases = [
        BenchmarkCase("one", "guide", "private guide body"),
        BenchmarkCase("two", "api", "private api body"),
    ]
    events = []

    def baseline(_cfg, body):
        events.append(("baseline", body))
        return ("guide" if "guide" in body else "concept", 20.0)

    def candidate(_cfg, body):
        events.append(("system1", body))
        page_type = "guide" if "guide" in body else "api"
        return PageTypeDecision(
            status="ok",
            page_type=page_type,
            probabilities=_probabilities(page_type),
            latency_ms=10.0,
            model="MultilingualSystem1",
        )

    report = run_benchmark(
        cases,
        object(),
        baseline_decider=baseline,
        system1_decider=candidate,
    )

    assert [kind for kind, _body in events] == [
        "baseline",
        "baseline",
        "system1",
        "system1",
    ]
    assert report["frozen_system1_p95_threshold_ms"] == 20.0
    assert report["recommendation"] == "go"
    assert report["corpus"] == {
        "count": 2,
        "labels": {"api": 1, "guide": 1},
    }
    serialized = json.dumps(report)
    assert "private guide body" not in serialized
    assert "private api body" not in serialized
    assert "one" not in serialized
    assert "two" not in serialized


@pytest.mark.parametrize("status", ["unavailable", "invalid"])
def test_runner_stops_without_scoring_failed_system1_decision(status):
    cases = [BenchmarkCase("one", "guide", "private")]

    def candidate(_cfg, _body):
        return PageTypeDecision(status, None, {}, 5.0)

    with pytest.raises(BenchmarkError, match="System One decision unavailable or invalid"):
        run_benchmark(
            cases,
            object(),
            baseline_decider=lambda _cfg, _body: ("guide", 10.0),
            system1_decider=candidate,
        )


def test_load_corpus_rejects_non_string_fields_without_echoing_body(tmp_path):
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(
        json.dumps({"id": ["not-hashable"], "label": "guide", "body": "private"})
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(BenchmarkError, match="invalid corpus record") as caught:
        load_corpus(corpus)

    assert "private" not in str(caught.value)
