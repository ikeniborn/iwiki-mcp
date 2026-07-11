from iwiki_mcp.engine import hier, store


def _rec(file, heading, kind, vec, ordinal=0, chunk=0):
    scale, q = store.quantize(vec)
    return store.Record(id=f"{file}#{heading}", file=file, heading=heading,
                        chunk=chunk, hash="h", dim=len(vec), scale=scale, q=q,
                        kind=kind, ordinal=ordinal)


def test_seed_articles_topk_and_threshold():
    summ = [_rec("a.md", "", "summary", [1.0, 0.0]),
            _rec("b.md", "", "summary", [0.0, 1.0])]
    seeds = hier.seed_articles([1.0, 0.05], summ, top_k=5, threshold=0.5)
    assert [f for f, _ in seeds] == ["a.md"]  # b below threshold


def test_expand_graph_is_undirected_and_caps(tmp_path):
    (tmp_path / "a.md").write_text("[B](b.md)\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("no links\n", encoding="utf-8")
    (tmp_path / "c.md").write_text("[A](a.md)\n", encoding="utf-8")  # c -> a (reverse)
    pool = hier.expand_graph(["a.md"], str(tmp_path), depth=1, cap=10)
    assert pool["a.md"] == "seed"
    assert pool["b.md"] == "graph"   # forward edge
    assert pool["c.md"] == "graph"   # reverse edge (undirected)


def test_rank_sections_pool_filter_and_seed_tiebreak():
    secs = [_rec("a.md", "S1", "section", [1.0, 0.0], ordinal=0),
            _rec("b.md", "S2", "section", [1.0, 0.0], ordinal=0),
            _rec("z.md", "S3", "section", [1.0, 0.0], ordinal=0)]
    pool = {"a.md": "graph", "b.md": "seed"}  # z not in pool
    hits = hier.rank_sections([1.0, 0.0], secs, pool, top_k=5)
    assert [h["file"] for h in hits] == ["b.md", "a.md"]  # z excluded; seed b first on tie
    assert hits[0]["source"] == "seed"
