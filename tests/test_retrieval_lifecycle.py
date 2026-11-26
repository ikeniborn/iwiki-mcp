from types import SimpleNamespace

from iwiki_mcp.retrieval import _TerminalSections


def section(file, heading, ordinal, tags=()):
    return SimpleNamespace(file=file, heading=heading, ordinal=ordinal, chunk=0,
                           type="guide", tags=tags)


def test_terminal_selection_and_facets():
    old = section("old.md", "Second", 0, ["task"])
    first = section("new.md", "First", 0)
    second = section("new.md", "Second", 1)
    pages = {"old.md": {"status": "deprecated", "superseded_by": "new.md"}, "new.md": {}}
    select = _TerminalSections(pages, [old, first, second], "current", None, None, ["task"], None)
    assert select.target(old.file, old.heading) is second
    assert select.target(old.file, "Obsolete") is first
    assert select.target("new.md", "First") is first
    missing = _TerminalSections(pages, [old, first], "current", None, None, None, "Missing")
    assert missing.target(old.file, old.heading) is None


def test_git_aliases_do_not_consume_limit(tmp_path, monkeypatch):
    from iwiki_mcp import indexer, retrieval
    from tests.test_retrieval_facets import _cfg
    cfg = _cfg()
    domain = tmp_path / "docs"
    domain.mkdir()
    for i in range(38):
        (domain / f"obsoleteword-{i}.md").write_text(
            "---\ntype: guide\nstatus: deprecated\nsuperseded_by: /current.md\n---\n"
            "# Old\n\n## Old detail\nHistorical wording.\n")
    (domain / "current.md").write_text("# Current\n\n## New detail\nCurrent instruction.\n")
    (domain / "other.md").write_text("# Other\n\n## Other detail\nobsoleteword directly.\n")
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, items: [[1., 0.] for _ in items])
    indexer.index_domain(cfg, str(tmp_path), "docs")

    def search(**options):
        return retrieval.prepare_read_candidates(cfg, str(tmp_path), ["docs"], "obsoleteword",
                                                 2, 0.1, mode="lexical", **options)
    result = search(lifecycle="current")[:2]
    assert {(h["file"], h["heading"]) for h in result} == {
        ("current.md", "New detail"), ("other.md", "Other detail")}
    assert search() == search(lifecycle="current")


def test_graph_aliases_do_not_consume_expansion_cap():
    from iwiki_mcp.retrieval import _graph_candidates
    successor = section("new.md", "New", 0)
    other = section("other.md", "Other", 0)
    files = {"a.md": [successor], "b.md": [successor], "other.md": [other]}
    pages = [{"file": "docs/" + file, "source": "graph"} for file in files]
    result = _graph_candidates(pages, {"docs": files}, 2, lambda path: path.split("/", 1))
    assert [recs[0].file for _, _, recs in result] == ["new.md", "other.md"]


def test_reranker_receives_terminal_text_for_obsolete_heading(tmp_path, monkeypatch):
    from iwiki_mcp import indexer, server
    from iwiki_mcp.engine.config import Config
    from tests.test_server_search import _seed
    base = _seed(tmp_path, monkeypatch)
    domain = tmp_path / "wiki/backend"
    (domain / "old.md").write_text(
        "---\nstatus: deprecated\nsuperseded_by: auth.md\n"
        "---\n# Losttitle\n\n## Legacy\nretired text\n")
    indexer.index_domain(Config.load(), base, "backend")
    monkeypatch.setenv("IWIKI_RERANK_MODEL", "fixture")
    captured = []

    def rerank(cfg, query, candidates, top_n):
        captured.extend(candidates)
        return candidates, {"applied": False}

    monkeypatch.setattr(server.rerank, "rerank_candidates", rerank)
    result = server.wiki_search("Losttitle", mode="lexical")
    assert [row["file"] for row in result["results"]] == ["auth.md"]
    assert captured and all("retired text" not in row["text"] for row in captured)
