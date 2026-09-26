import json

from eval.system1_decisions import report
from eval.system1_decisions.__main__ import main


def _rec(ts, identity, *, requested, final, source, predicted, warned, status="ok",
         domain="d"):
    return {"ts": ts, "domain": domain, "identity": identity, "requested_type": requested,
            "final_type": final, "type_source": source, "predicted": predicted,
            "warned": warned, "status": status, "latency_ms": 100.0}


RECORDS = [
    _rec("2026-09-27T10:00:00+00:00", "concept/a", requested="concept", final="concept",
         source="explicit", predicted="reference", warned=True),
    _rec("2026-09-27T10:01:00+00:00", "concept/b", requested="concept", final="concept",
         source="explicit", predicted="reference", warned=True),
    _rec("2026-09-27T10:02:00+00:00", "reference/c", requested="reference",
         final="reference", source="explicit", predicted="reference", warned=False),
    _rec("2026-09-27T10:03:00+00:00", "architecture/e", requested=None,
         final="architecture", source="system1", predicted="architecture", warned=False),
    _rec("2026-09-27T10:04:00+00:00", "concept/f", requested=None, final="concept",
         source="default", predicted=None, warned=False, status="unavailable"),
]
# a was moved to the suggested type; b was kept; c, e exist
PAGE_IDS = {"d/reference/a", "d/concept/b", "d/reference/c", "d/architecture/e"}


def test_summary_counts_warnings_and_acceptance():
    summary = report.summarize(RECORDS, PAGE_IDS)

    assert summary["decisions"] == 5
    assert summary["by_status"] == {"ok": 4, "unavailable": 1}
    assert summary["by_type_source"] == {"default": 1, "explicit": 3, "system1": 1}
    assert summary["system1_assigned_types"] == {"architecture": 1}
    assert summary["warnings"] == 2
    assert summary["warning_outcomes"] == {"accepted": 1, "kept": 1}
    assert summary["warning_acceptance"] == 0.5
    assert summary["explicit_agreement"] == round(1 / 3, 4)


def test_training_candidates_use_reviewed_labels_only():
    rows = report.training_candidates(RECORDS, PAGE_IDS)

    assert rows == [
        {"id": "d/reference/a", "label": "reference", "label_source": "accepted_warning",
         "predicted": "reference"},
        {"id": "d/concept/b", "label": "concept", "label_source": "kept_after_warning",
         "predicted": "reference"},
        {"id": "d/reference/c", "label": "reference", "label_source": "explicit",
         "predicted": "reference"},
    ]


def test_latest_decision_per_page_wins():
    older = dict(RECORDS[1], ts="2026-09-27T09:00:00+00:00", warned=False)
    rows = report.training_candidates([older, RECORDS[1]], PAGE_IDS)
    assert [r["label_source"] for r in rows] == ["kept_after_warning"]


def test_cli_writes_summary_and_private_training_file(tmp_path, capsys):
    log = tmp_path / "log.jsonl"
    log.write_text("".join(json.dumps(r) + "\n" for r in RECORDS))
    ids = tmp_path / "ids.txt"
    ids.write_text("\n".join(sorted(PAGE_IDS)))
    out = tmp_path / "train.jsonl"

    assert main(["--log", str(log), "--page-ids", str(ids), "--training-out", str(out)]) == 0

    assert json.loads(capsys.readouterr().out)["warnings"] == 2
    assert len(out.read_text().splitlines()) == 3
    assert oct(out.stat().st_mode & 0o777) == "0o600"
