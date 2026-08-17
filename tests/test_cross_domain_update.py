from __future__ import annotations

from pathlib import Path
import json
import subprocess

from iwiki_mcp import base, indexer, server, sync
from iwiki_mcp.engine.lint import lint


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _setup(
    tmp_path,
    monkeypatch,
    *,
    write_scope=("target", "alpha", "beta"),
    read_scope=("target", "alpha", "beta"),
):
    wiki = tmp_path / "wiki"
    for domain in ("target", "alpha", "beta", "hidden"):
        (wiki / domain).mkdir(parents=True)
    (wiki / "target" / "concept").mkdir()
    (wiki / "target" / "concept" / "x.md").write_text(
        "---\n"
        "type: concept\n"
        "title: X\n"
        "description: Kept\n"
        "tags: [kept]\n"
        "status: stable\n"
        "timestamp: 2020-01-02\n"
        "---\n"
        "# X\n\n"
        "## Old Heading\nOld body.\n\n"
        "## Other\n[Self](#Old-Heading)\n",
        encoding="utf-8",
    )
    (wiki / "target" / "relative.md").write_text(
        "# Relative\n\n## Links\n"
        "[target](concept/x.md#Old-Heading) "
        "[other anchor](concept/x.md#Other) "
        "[other page](other.md#Old-Heading)\n",
        encoding="utf-8",
    )
    (wiki / "target" / "other.md").write_text(
        "# Other page\n\n## Old Heading\nKeep.\n", encoding="utf-8"
    )
    (wiki / "alpha" / "a.md").write_text(
        "# A\n\n## Links\n"
        "[target](iwiki://target/concept/x.md#Old-Heading) "
        "[other](iwiki://target/concept/x#Other)\n",
        encoding="utf-8",
    )
    (wiki / "beta" / "b.md").write_text(
        "# B\n\n## Links\n"
        "[one](iwiki://target/concept/x#old-heading) "
        "[two](iwiki://target/concept/x.md#Old-Heading)\n",
        encoding="utf-8",
    )
    (wiki / "hidden" / "h.md").write_text(
        "# H\n\n## Link\n[hidden](iwiki://target/concept/x#old-heading)\n",
        encoding="utf-8",
    )
    for domain in ("target", "alpha", "beta", "hidden"):
        (wiki / domain / "log.jsonl").write_text("", encoding="utf-8")
        (wiki / domain / "index.jsonl").write_text("", encoding="utf-8")
    _git(wiki, "init", "-q")
    _git(wiki, "config", "user.email", "test@example.com")
    _git(wiki, "config", "user.name", "Test User")
    _git(wiki, "add", "-A")
    _git(wiki, "commit", "-q", "-m", "seed")
    binding = base.Binding(
        str(wiki),
        read_scope,
        write_scope,
        str(tmp_path),
        "target",
    )
    monkeypatch.setattr(base, "resolve_binding", lambda: binding)
    monkeypatch.setattr(sync, "ensure_fresh", lambda _base: {"state": "fresh"})
    monkeypatch.setattr(
        sync,
        "sync",
        lambda _base: {"pushed": False, "sync_attempts": 0, "push_attempts": 0},
    )
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setenv("IWIKI_EMBED_DIMENSIONS", "2")
    monkeypatch.setattr(
        indexer, "embed_texts", lambda cfg, texts: [[1.0, 0.0] for _ in texts]
    )
    return wiki


def test_heading_rename_rewrites_relative_and_cross_domain_anchors(
    tmp_path, monkeypatch
):
    wiki = _setup(tmp_path, monkeypatch)

    result = server.wiki_update_page(
        "target",
        "concept/x",
        "Old Heading",
        "New body.",
        new_heading="New Heading",
    )

    assert result["page"] == "target/concept/x.md"
    assert result["heading"] == "Old Heading"
    assert result["rewritten_pages"] == [
        "alpha/a.md",
        "beta/b.md",
        "target/concept/x.md",
        "target/relative.md",
    ]
    assert result["affected_domains"] == ["alpha", "beta", "target"]
    assert result["rewritten_links"] == 5
    assert len(result["transaction_id"]) == 32
    target = (wiki / "target" / "concept" / "x.md").read_text()
    assert "## New Heading\n\nNew body." in target
    assert "[Self](#new-heading)" in target
    relative = (wiki / "target" / "relative.md").read_text()
    assert "concept/x.md#new-heading" in relative
    assert "concept/x.md#Other" in relative
    assert "other.md#Old-Heading" in relative
    alpha = (wiki / "alpha" / "a.md").read_text()
    assert "iwiki://target/concept/x.md#new-heading" in alpha
    assert "iwiki://target/concept/x#Other" in alpha
    beta = (wiki / "beta" / "b.md").read_text()
    assert beta.count("#new-heading") == 2
    assert "#old-heading" in (wiki / "hidden" / "h.md").read_text()
    changed = _git(
        wiki, "show", "--name-only", "--format=", "HEAD"
    ).splitlines()
    assert changed == [
        "alpha/a.md",
        "alpha/index.jsonl",
        "beta/b.md",
        "beta/index.jsonl",
        "target/concept/x.md",
        "target/index.jsonl",
        "target/relative.md",
    ]
    assert "Iwiki-Transaction: " in _git(wiki, "log", "-1", "--format=%B")

    visible = {
        domain: str(wiki / domain) for domain in ("target", "alpha", "beta")
    }
    for domain in visible:
        report = lint(
            visible[domain],
            domain=domain,
            base_dir=str(wiki),
            visible_domains=visible,
        )
        assert report["broken"] == []
        assert report["graph"]["state"] == "ready"
        assert report["graph"]["fingerprint_match"] is True
        for key in (
            "missing_pages",
            "extra_pages",
            "missing_edges",
            "extra_edges",
            "anchor_mismatches",
        ):
            assert report["graph"][key] == []


def test_heading_rename_rejects_visible_read_only_referrer(tmp_path, monkeypatch):
    wiki = _setup(tmp_path, monkeypatch, write_scope=("target", "alpha"))
    head = _git(wiki, "rev-parse", "HEAD")
    before = {
        path.relative_to(wiki).as_posix(): path.read_bytes()
        for path in wiki.rglob("*")
        if path.is_file() and ".git" not in path.parts and ".iwiki" not in path.parts
    }

    result = server.wiki_update_page(
        "target",
        "concept/x",
        "Old Heading",
        "New body.",
        new_heading="New Heading",
    )

    assert result["code"] == "write_scope_blocked"
    assert _git(wiki, "rev-parse", "HEAD") == head
    after = {
        path.relative_to(wiki).as_posix(): path.read_bytes()
        for path in wiki.rglob("*")
        if path.is_file() and ".git" not in path.parts and ".iwiki" not in path.parts
    }
    assert after == before


def test_heading_rename_rejects_anchor_collision_before_write(tmp_path, monkeypatch):
    wiki = _setup(tmp_path, monkeypatch)
    head = _git(wiki, "rev-parse", "HEAD")

    result = server.wiki_update_page(
        "target",
        "concept/x",
        "Old Heading",
        "New body.",
        new_heading="Other!",
    )

    assert result["code"] == "heading_collision"
    assert _git(wiki, "rev-parse", "HEAD") == head
    assert "## Old Heading\nOld body." in (
        wiki / "target" / "concept" / "x.md"
    ).read_text()


def test_same_normalized_anchor_renames_heading_without_referrer_changes(
    tmp_path, monkeypatch
):
    wiki = _setup(tmp_path, monkeypatch)

    result = server.wiki_update_page(
        "target",
        "concept/x",
        "Old Heading",
        "New body.",
        new_heading="Old Heading!",
    )

    assert result["rewritten_pages"] == []
    assert result["rewritten_links"] == 0
    assert result["affected_domains"] == ["target"]
    assert "## Old Heading!\n\nNew body." in (
        wiki / "target" / "concept" / "x.md"
    ).read_text()
    assert "#Old-Heading" in (wiki / "target" / "relative.md").read_text()


def test_heading_rename_updates_source_log_inside_transaction(tmp_path, monkeypatch):
    wiki = _setup(tmp_path, monkeypatch)
    source = tmp_path / "source.txt"
    source.write_text("current source\n")

    result = server.wiki_update_page(
        "target",
        "concept/x",
        "Old Heading",
        "New body.",
        source=str(source),
        new_heading="Old Heading!",
    )

    assert "error" not in result
    records = [
        json.loads(line)
        for line in (wiki / "target" / "log.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert len(records) == 1
    assert records[0]["page"] == "concept/x.md"
    assert records[0]["source"] == "source.txt"


def test_heading_rename_reports_source_changed_on_preimage_race(
    tmp_path, monkeypatch
):
    wiki = _setup(tmp_path, monkeypatch)
    real_execute = server.cross_domain.execute_plan

    def race(base_dir, binding, plan, **kwargs):
        (wiki / "target" / "concept" / "x.md").write_text(
            "# External change\n", encoding="utf-8"
        )
        return real_execute(base_dir, binding, plan, **kwargs)

    monkeypatch.setattr(server.cross_domain, "execute_plan", race)

    result = server.wiki_update_page(
        "target",
        "concept/x",
        "Old Heading",
        "New body.",
        new_heading="New Heading",
    )

    assert result["code"] == "source_changed"


def test_heading_rename_rejects_empty_normalized_heading(tmp_path, monkeypatch):
    wiki = _setup(tmp_path, monkeypatch)
    head = _git(wiki, "rev-parse", "HEAD")

    result = server.wiki_update_page(
        "target",
        "concept/x",
        "Old Heading",
        "New body.",
        new_heading="!!!",
    )

    assert "empty normalized heading" in result["error"]
    assert _git(wiki, "rev-parse", "HEAD") == head


def test_heading_rename_resolves_empty_read_as_all_domains(tmp_path, monkeypatch):
    wiki = _setup(
        tmp_path,
        monkeypatch,
        read_scope=(),
        write_scope=("target", "alpha", "beta", "hidden"),
    )

    result = server.wiki_update_page(
        "target",
        "concept/x",
        "Old Heading",
        "New body.",
        new_heading="New Heading",
    )

    assert "error" not in result
    assert result["rewritten_pages"] == [
        "alpha/a.md",
        "beta/b.md",
        "hidden/h.md",
        "target/concept/x.md",
        "target/relative.md",
    ]
    assert "#new-heading" in (
        wiki / "hidden" / "h.md"
    ).read_text(encoding="utf-8")
