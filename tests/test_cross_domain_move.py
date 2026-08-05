from __future__ import annotations

from pathlib import Path
import subprocess

from iwiki_mcp import base, graph, indexer, server, sync
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
        "description: Kept description\n"
        "resource: /src/x.py\n"
        "tags: [kept]\n"
        "status: draft\n"
        "timestamp: 2020-01-02\n"
        "---\n"
        "# X\n\n## Anchor\nBody.\n\n## Other\nMore.\n",
        encoding="utf-8",
    )
    (wiki / "target" / "relative.md").write_text(
        "# Relative\n\n## Link\n[X](concept/x.md#Anchor)\n", encoding="utf-8"
    )
    (wiki / "alpha" / "a.md").write_text(
        "# A\n\n## Links\n"
        "[one](iwiki://target/concept/x.md#Anchor) "
        "[two](iwiki://target/concept/x#Other)\n",
        encoding="utf-8",
    )
    (wiki / "beta" / "b.md").write_text(
        "# B\n\n## Links\n"
        "[one](iwiki://target/concept/x) "
        "[duplicate](iwiki://target/concept/x)\n",
        encoding="utf-8",
    )
    (wiki / "hidden" / "h.md").write_text(
        "# H\n\n## Link\n[hidden](iwiki://target/concept/x)\n",
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
        "target",
        str(tmp_path),
        write_scope,
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


def test_apply_okf_move_rewrites_visible_writable_domains_in_one_commit(
    tmp_path, monkeypatch
):
    wiki = _setup(tmp_path, monkeypatch)
    unrelated = wiki / "operator-notes.md"
    unrelated.write_text("local operator note\n", encoding="utf-8")

    result = server.wiki_apply_okf("target", "concept/x", type="architecture")

    assert result["page"] == "target/architecture/x.md"
    assert result["rewritten_pages"] == [
        "alpha/a.md",
        "beta/b.md",
        "target/relative.md",
    ]
    assert result["affected_domains"] == ["alpha", "beta", "target"]
    assert result["rewritten_links"] == 5
    assert len(result["transaction_id"]) == 32
    assert not (wiki / "target" / "concept" / "x.md").exists()
    moved = (wiki / "target" / "architecture" / "x.md").read_text()
    assert "type: architecture" in moved
    assert "description: Kept description" in moved
    assert "resource: /src/x.py" in moved
    assert "tags: [kept]" in moved
    assert "status: draft" in moved
    original_date = _git(
        wiki, "log", "-1", "--format=%cs", "HEAD^", "--", "target/concept/x.md"
    )
    assert f"timestamp: {original_date}" in moved
    assert "architecture/x.md#Anchor" in (
        wiki / "target" / "relative.md"
    ).read_text()
    assert "iwiki://target/architecture/x.md#Anchor" in (
        wiki / "alpha" / "a.md"
    ).read_text()
    beta = (wiki / "beta" / "b.md").read_text()
    assert beta.count("iwiki://target/architecture/x") == 2
    assert "iwiki://target/concept/x" in (wiki / "hidden" / "h.md").read_text()
    changed = _git(
        wiki, "show", "--no-renames", "--name-only", "--format=", "HEAD"
    ).splitlines()
    assert changed == [
        "alpha/a.md",
        "alpha/index.jsonl",
        "beta/b.md",
        "beta/index.jsonl",
        "target/architecture/x.md",
        "target/concept/x.md",
        "target/index.jsonl",
        "target/relative.md",
    ]
    assert unrelated.read_text(encoding="utf-8") == "local operator note\n"
    assert "?? operator-notes.md" in _git(wiki, "status", "--short")
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


def test_apply_okf_move_counts_relative_links_with_ready_graph(
    tmp_path, monkeypatch
):
    wiki = _setup(tmp_path, monkeypatch)
    assert graph.scoped_graph(
        str(wiki), ("target", "alpha", "beta")
    ) is not None

    result = server.wiki_apply_okf("target", "concept/x", type="architecture")

    assert result["rewritten_links"] == 5


def test_apply_okf_move_rejects_visible_read_only_referrer(tmp_path, monkeypatch):
    wiki = _setup(tmp_path, monkeypatch, write_scope=("target", "alpha"))
    head = _git(wiki, "rev-parse", "HEAD")
    before = {
        path.relative_to(wiki).as_posix(): path.read_bytes()
        for path in wiki.rglob("*")
        if path.is_file() and ".git" not in path.parts and ".iwiki" not in path.parts
    }

    result = server.wiki_apply_okf("target", "concept/x", type="architecture")

    assert result["code"] == "write_scope_blocked"
    assert _git(wiki, "rev-parse", "HEAD") == head
    after = {
        path.relative_to(wiki).as_posix(): path.read_bytes()
        for path in wiki.rglob("*")
        if path.is_file() and ".git" not in path.parts and ".iwiki" not in path.parts
    }
    assert after == before


def test_apply_okf_move_without_referrers_affects_only_target(tmp_path, monkeypatch):
    wiki = _setup(tmp_path, monkeypatch)
    for domain, file in (
        ("target", "relative.md"),
        ("alpha", "a.md"),
        ("beta", "b.md"),
    ):
        (wiki / domain / file).write_text(f"# {domain}\n\n## Body\nNone.\n")
    _git(wiki, "add", "-A")
    _git(wiki, "commit", "-q", "-m", "remove referrers")

    result = server.wiki_apply_okf("target", "concept/x", type="architecture")

    assert result["rewritten_pages"] == []
    assert result["rewritten_links"] == 0
    assert result["affected_domains"] == ["target"]


def test_apply_okf_move_allows_referrer_domain_without_ingest_log(
    tmp_path, monkeypatch
):
    wiki = _setup(tmp_path, monkeypatch)
    for domain in ("alpha", "beta"):
        (wiki / domain / "log.jsonl").unlink()
    _git(wiki, "add", "-A")
    _git(wiki, "commit", "-q", "-m", "remove unused referrer logs")

    result = server.wiki_apply_okf("target", "concept/x", type="architecture")

    assert "error" not in result
    assert result["rewritten_pages"] == [
        "alpha/a.md",
        "beta/b.md",
        "target/relative.md",
    ]
    assert not (wiki / "alpha" / "log.jsonl").exists()
    assert not (wiki / "beta" / "log.jsonl").exists()


def test_apply_okf_move_resolves_empty_read_as_all_domains(tmp_path, monkeypatch):
    wiki = _setup(
        tmp_path,
        monkeypatch,
        read_scope=(),
        write_scope=("target", "alpha", "beta", "hidden"),
    )

    result = server.wiki_apply_okf("target", "concept/x", type="architecture")

    assert "error" not in result
    assert result["rewritten_pages"] == [
        "alpha/a.md",
        "beta/b.md",
        "hidden/h.md",
        "target/relative.md",
    ]
    assert "iwiki://target/architecture/x" in (
        wiki / "hidden" / "h.md"
    ).read_text(encoding="utf-8")
