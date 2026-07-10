import json
import os

from iwiki_mcp import base, server
from iwiki_mcp.engine import lint as lint_engine


def _seed(tmp_path, monkeypatch):
    b = tmp_path / "wiki"
    (b / "backend" / ".iwiki").mkdir(parents=True)
    (b / "backend" / "auth.md").write_text("# Auth\n## Overview\no\n## Flow\nx\n")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text('read = ["backend"]\nwrite = "backend"\n')
    monkeypatch.setenv("IWIKI_BASE_DIR", str(b))
    monkeypatch.setenv("IWIKI_PROJECT_DIR", str(proj))
    return str(b)


def test_lint_one_domain(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_lint("backend")
    assert "backend" in out["domains"]


def test_sync_no_repo(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_sync()
    assert "error" in out or "warning" in out


def _seed_remediation(tmp_path, monkeypatch):
    b = tmp_path / "wiki"
    domain = b / "backend"
    (domain / ".iwiki").mkdir(parents=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text(
        'read = ["backend"]\nwrite = "backend"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("IWIKI_BASE_DIR", str(b))
    monkeypatch.setenv("IWIKI_PROJECT_DIR", str(proj))
    return b, domain, proj


def _write_log(domain_path, records):
    log = domain_path / ".iwiki" / "log.jsonl"
    with open(log, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def test_remediation_plan_empty_when_lint_has_no_candidates(tmp_path, monkeypatch):
    _seed_remediation(tmp_path, monkeypatch)

    out = server.wiki_remediation_plan()

    assert out["domain"] == "backend"
    assert out["update_candidates"] == []
    assert out["delete_candidates"] == []
    assert out["blocked_candidates"] == []
    assert out["lint"]["wiki_present"] is False
    assert "use wiki_update_page" in " ".join(out["next_steps"]).lower()
    assert "description" in out["authoring_rules"]


def test_remediation_plan_rejects_non_write_domain(tmp_path, monkeypatch):
    b, _, proj = _seed_remediation(tmp_path, monkeypatch)
    (b / "other" / ".iwiki").mkdir(parents=True)
    (proj / ".iwiki.toml").write_text(
        'read = ["backend", "other"]\nwrite = "backend"\n',
        encoding="utf-8",
    )

    out = server.wiki_remediation_plan("other")

    assert "error" in out
    assert "bound write domain" in out["hint"]


def test_remediation_plan_rejects_missing_write_domain(tmp_path, monkeypatch):
    b, _, proj = _seed_remediation(tmp_path, monkeypatch)
    (proj / ".iwiki.toml").write_text('read = ["backend"]\n', encoding="utf-8")
    monkeypatch.setenv("IWIKI_BASE_DIR", str(b))
    monkeypatch.setenv("IWIKI_PROJECT_DIR", str(proj))

    out = server.wiki_remediation_plan()

    assert out["error"] == "no write domain bound"
    assert "wiki_bind" in out["hint"]


def test_remediation_plan_does_not_mutate_page_or_log(tmp_path, monkeypatch):
    b, domain, _ = _seed_remediation(tmp_path, monkeypatch)
    page = domain / "auth.md"
    page.write_text("# Auth\n## Overview\nold\n## Flow\nold body\n", encoding="utf-8")
    src = tmp_path / "auth.py"
    src.write_text("new source\n", encoding="utf-8")
    _write_log(domain, [{
        "op": "ingest",
        "source": str(src),
        "page": "auth.md",
        "src_hash": "oldhash",
    }])
    log_before = (domain / ".iwiki" / "log.jsonl").read_text(encoding="utf-8")
    page_before = page.read_text(encoding="utf-8")
    index_path = base.index_path(str(b), "backend")
    assert not os.path.exists(index_path)

    out = server.wiki_remediation_plan()

    assert "error" not in out
    assert page.read_text(encoding="utf-8") == page_before
    assert (domain / ".iwiki" / "log.jsonl").read_text(encoding="utf-8") == log_before
    assert not os.path.exists(index_path)


def test_remediation_plan_returns_stale_update_candidate(tmp_path, monkeypatch):
    _, domain, _ = _seed_remediation(tmp_path, monkeypatch)
    page = domain / "auth.md"
    page.write_text("# Auth\n## Overview\nold\n## Flow\nold body\n", encoding="utf-8")
    src = tmp_path / "auth.py"
    src.write_text("def login():\n    return 'token'\n", encoding="utf-8")
    _write_log(domain, [{
        "op": "ingest",
        "source": str(src),
        "page": "auth.md",
        "src_hash": "oldhash",
    }])

    out = server.wiki_remediation_plan()

    assert out["blocked_candidates"] == []
    cand = out["update_candidates"][0]
    assert cand["domain"] == "backend"
    assert cand["slug"] == "auth"
    assert cand["page"] == str(page)
    assert cand["source"] == str(src)
    assert cand["current_markdown"].startswith("# Auth")
    assert "def login" in cand["source_content"]
    assert cand["source_bytes"] == len(src.read_bytes())
    assert cand["source_truncated"] is False
    assert cand["current_headings"] == ["Overview", "Flow"]
    assert cand["recommended_tools"] == [
        "wiki_update_page",
        "wiki_delete_page",
        "wiki_write_page",
        "wiki_lint",
    ]


def test_remediation_plan_returns_missing_source_delete_candidate(tmp_path, monkeypatch):
    _, domain, _ = _seed_remediation(tmp_path, monkeypatch)
    page = domain / "old.md"
    page.write_text("# Old\n## Overview\nold\n", encoding="utf-8")
    missing = tmp_path / "deleted.py"
    _write_log(domain, [{
        "op": "ingest",
        "source": str(missing),
        "page": "old.md",
        "src_hash": None,
    }])

    out = server.wiki_remediation_plan()

    assert out["update_candidates"] == []
    cand = out["delete_candidates"][0]
    assert cand == {
        "domain": "backend",
        "slug": "old",
        "page": str(page),
        "source": str(missing),
        "recommended_tools": ["wiki_delete_page", "wiki_lint"],
    }


def test_remediation_plan_blocks_ignored_source_content(tmp_path, monkeypatch):
    _, domain, proj = _seed_remediation(tmp_path, monkeypatch)
    (proj / ".iwikiignore").write_text("secret.py\n", encoding="utf-8")
    page = domain / "secret.md"
    page.write_text("# Secret\n## Overview\nold\n", encoding="utf-8")
    src = proj / "secret.py"
    src.write_text("token = 'secret'\n", encoding="utf-8")
    _write_log(domain, [{
        "op": "ingest",
        "source": str(src),
        "page": "secret.md",
        "src_hash": "oldhash",
    }])

    out = server.wiki_remediation_plan()

    assert out["update_candidates"] == []
    blocked = out["blocked_candidates"][0]
    assert blocked["reason"] == "source_ignored"
    assert blocked["source"] == str(src)
    assert "source_content" not in blocked


def test_remediation_plan_truncates_large_source(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "SOURCE_CONTENT_MAX_BYTES", 12)
    _, domain, _ = _seed_remediation(tmp_path, monkeypatch)
    page = domain / "large.md"
    page.write_text("# Large\n## Overview\nold\n", encoding="utf-8")
    src = tmp_path / "large.py"
    src.write_text("abcdefghijklmnop", encoding="utf-8")
    _write_log(domain, [{
        "op": "ingest",
        "source": str(src),
        "page": "large.md",
        "src_hash": "oldhash",
    }])

    out = server.wiki_remediation_plan()

    cand = out["update_candidates"][0]
    assert cand["source_content"] == "abcdefghijkl"
    assert cand["source_bytes"] == 16
    assert cand["source_truncated"] is True


def test_remediation_plan_blocks_unreadable_stale_inputs(tmp_path, monkeypatch):
    _, domain, _ = _seed_remediation(tmp_path, monkeypatch)
    page = domain / "auth.md"
    page.write_text("# Auth\n## Overview\nold\n", encoding="utf-8")
    missing_page = domain / "missing.md"
    missing_source = tmp_path / "missing.py"
    src = tmp_path / "auth.py"
    src.write_text("new source\n", encoding="utf-8")

    def fake_lint(_wiki_dir, project_dir=None):
        return {
            "wiki_present": True,
            "stale": [
                {"page": str(page), "source": str(missing_source)},
                {"page": str(missing_page), "source": str(src)},
            ],
            "missing_source": [],
        }

    monkeypatch.setattr(lint_engine, "lint", fake_lint)

    out = server.wiki_remediation_plan()

    reasons = {candidate["reason"] for candidate in out["blocked_candidates"]}
    assert reasons == {"source_unreadable", "page_unreadable"}
    assert out["update_candidates"] == []


def test_remediation_plan_blocks_missing_page_for_stale_record(tmp_path, monkeypatch):
    _, domain, _ = _seed_remediation(tmp_path, monkeypatch)
    (domain / "other.md").write_text("# Other\n## Overview\nold\n", encoding="utf-8")
    src = tmp_path / "auth.py"
    src.write_text("new source\n", encoding="utf-8")
    _write_log(domain, [{
        "op": "ingest",
        "source": str(src),
        "page": "auth.md",
        "src_hash": "oldhash",
    }])

    out = server.wiki_remediation_plan()

    assert out["update_candidates"] == []
    assert out["blocked_candidates"] == []
    assert out["lint"]["stale"] == []
