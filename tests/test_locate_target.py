from iwiki_mcp import retrieval, indexer
from iwiki_mcp.engine.config import Config


def _cfg_env(monkeypatch):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")


def _fake_embed(cfg, texts):
    """Deterministic stub: text mentioning 'purpose'/'retriev' gets one vector,
    everything else gets an orthogonal one. This is what makes the summary and
    the '## Purpose' section genuinely findable for an on-topic query, and
    genuinely unreachable (score below write_seed_threshold) for an unrelated
    one -- not just a heading-filter coincidence."""
    vecs = []
    for t in texts:
        tl = t.lower()
        if "purpose" in tl or "retriev" in tl:
            vecs.append([1.0, 0.0, 0.0])
        else:
            vecs.append([0.0, 1.0, 0.0])
    return vecs


def _seed_two_level(tmp_path, monkeypatch, dom):
    _cfg_env(monkeypatch)
    b = tmp_path
    (b / dom).mkdir(parents=True)
    (b / dom / "p.md").write_text(
        "---\ndescription: This page explains the purpose of the retrieval system.\n---\n"
        "# P\n\n"
        "## Purpose\nThe purpose of retrieval is to locate write targets precisely.\n\n"
        "## Setup\nInstall steps and configuration values for the server.\n"
    )
    monkeypatch.setattr(indexer, "embed_texts", _fake_embed)
    cfg = Config.load()
    indexer.index_domain(cfg, str(b), dom)
    return cfg, str(b)


def test_locate_target_exact_heading(tmp_path, monkeypatch):
    cfg, b = _seed_two_level(tmp_path, monkeypatch, "d")
    monkeypatch.setattr(retrieval, "embed_texts", _fake_embed)

    hit = retrieval.locate_target(cfg, b, "d", "purpose of retrieval", heading="Purpose")

    assert hit["exists"] is True
    assert hit["heading"] == "Purpose"
    assert hit["domain"] == "d"
    assert hit["file"] == "p.md"
    assert isinstance(hit["score"], float)


def test_locate_target_miss_returns_exists_false(tmp_path, monkeypatch):
    cfg, b = _seed_two_level(tmp_path, monkeypatch, "d")
    monkeypatch.setattr(retrieval, "embed_texts", _fake_embed)

    hit = retrieval.locate_target(cfg, b, "d", "totally unrelated", heading="Nonexistent")

    assert hit["exists"] is False
    assert hit["domain"] == "d"


def test_locate_target_wrong_heading_alone_misses(tmp_path, monkeypatch):
    """On-topic query (would seed and rank fine) but the heading hint doesn't
    exist on the page -> the exact-heading filter alone must drive the miss."""
    cfg, b = _seed_two_level(tmp_path, monkeypatch, "d")
    monkeypatch.setattr(retrieval, "embed_texts", _fake_embed)

    hit = retrieval.locate_target(cfg, b, "d", "purpose of retrieval", heading="Nope")

    assert hit["exists"] is False


def test_locate_target_case_insensitive_heading(tmp_path, monkeypatch):
    cfg, b = _seed_two_level(tmp_path, monkeypatch, "d")
    monkeypatch.setattr(retrieval, "embed_texts", _fake_embed)

    hit = retrieval.locate_target(cfg, b, "d", "purpose of retrieval", heading="pURPOSE")

    assert hit["exists"] is True
    assert hit["heading"] == "Purpose"


def test_locate_target_empty_domain_returns_exists_false(tmp_path, monkeypatch):
    """No index.jsonl / no records at all (un-indexed domain) -> the
    `not summ or not secs` early return, not a crash."""
    _cfg_env(monkeypatch)
    b = tmp_path
    (b / "d").mkdir(parents=True)
    monkeypatch.setattr(retrieval, "embed_texts", _fake_embed)
    cfg = Config.load()

    hit = retrieval.locate_target(cfg, str(b), "d", "anything", heading="Whatever")

    assert hit == {"domain": "d", "exists": False}


def _fake_embed_graded(cfg, texts):
    """Deterministic stub with a wide score gap: text mentioning 'target-topic'
    embeds far from the query (cos ~0.77), everything else (8 filler sections +
    the description + the query) embeds identical to the query (cos ~1.0). Used
    to build a pool where the exact-heading target section scores BELOW all 8
    filler sections, so with the default IWIKI_TOP_K=8 it is the 9th-ranked
    section -- truncated out of `rank_sections` unless the heading filter runs
    before ranking."""
    vecs = []
    for t in texts:
        if "target-topic" in t.lower():
            vecs.append([1.0, 1.0, 0.0])
        else:
            vecs.append([1.0, 0.1, 0.0])
    return vecs


def test_locate_target_heading_filter_runs_before_top_k_truncation(tmp_path, monkeypatch):
    """RED under the old order (rank_sections(..., cfg.top_k) THEN filter by
    heading): 8 filler sections all out-score the 'Target' section, so ranking
    first truncates 'Target' out of the top-8 before the heading filter ever
    sees it -> exists=False for a section that exists. GREEN with the fix:
    filtering `secs` down to the exact heading BEFORE ranking means 'Target' is
    the only candidate and is always found."""
    _cfg_env(monkeypatch)
    b = tmp_path
    (b / "d").mkdir(parents=True)
    fillers = "\n\n".join(
        f"## Filler{i}\nfiller-topic body number {i}." for i in range(1, 9)
    )
    (b / "d" / "p.md").write_text(
        "---\ndescription: filler-topic summary for this page.\n---\n"
        "# P\n\n"
        f"{fillers}\n\n"
        "## Target\ntarget-topic body holds the real section text.\n"
    )
    monkeypatch.setattr(indexer, "embed_texts", _fake_embed_graded)
    cfg = Config.load()
    indexer.index_domain(cfg, str(b), "d")
    monkeypatch.setattr(retrieval, "embed_texts", _fake_embed_graded)

    hit = retrieval.locate_target(cfg, str(b), "d", "filler-topic query", heading="Target")

    assert hit["exists"] is True
    assert hit["heading"] == "Target"
    assert hit["file"] == "p.md"
