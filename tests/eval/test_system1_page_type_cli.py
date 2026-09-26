import json

from eval.system1_page_type import __main__ as cli


def test_cli_writes_aggregate_report(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(
        json.dumps({"id": "one", "label": "guide", "body": "private"}) + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "evidence" / "report.json"
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://llm")
    monkeypatch.setenv("IWIKI_LLM_KEY", "llm-key")
    monkeypatch.setenv("IWIKI_CHAT_MODEL", "baseline")
    monkeypatch.setenv("IWIKI_SYSTEM1_SHADOW", "true")
    monkeypatch.setenv("IWIKI_SYSTEM1_BASE_URL", "http://system1/v1")
    monkeypatch.setenv("IWIKI_SYSTEM1_KEY", "system1-key")
    monkeypatch.setattr(
        cli,
        "run_benchmark",
        lambda cases, cfg: {
            "corpus": {"count": len(cases), "labels": {"guide": 1}},
            "recommendation": "go",
        },
    )

    exit_code = cli.main(["--corpus", str(corpus), "--output", str(output)])

    assert exit_code == 0
    assert json.loads(output.read_text(encoding="utf-8"))["recommendation"] == "go"
