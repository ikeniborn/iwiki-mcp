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
