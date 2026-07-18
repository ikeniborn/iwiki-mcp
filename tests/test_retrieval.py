import builtins
import json
import multiprocessing
import os
from dataclasses import replace
from pathlib import Path

import pytest

from iwiki_mcp import base as wiki_base, indexer, retrieval
from iwiki_mcp.engine import store
from iwiki_mcp.engine.chunk import chunk_markdown
from iwiki_mcp.engine.config import Config


def _cfg():
    return Config(base_url="http://x/v1", api_key="k", embed_model="m",
                  dimensions=2, chunk_size=512, chunk_overlap=64, summary_max=400,
                  top_k=8, score_threshold=0.0, graph_depth=2, ignore=None,
                  seed_top_k=1, bfs_top_k=10, seed_threshold=0.5)


def _seed(tmp_path, monkeypatch):
    base = tmp_path / "wiki"
    domain = base / "d"
    domain.mkdir(parents=True)
    (domain / "seed.md").write_text(
        "---\ndescription: alpha seed\n---\n# Seed\n\n"
        "## Match\nrefresh_token alpha\n\n[Graph](graph.md)\n",
        encoding="utf-8",
    )
    (domain / "graph.md").write_text(
        "---\ndescription: beta graph\n---\n# Graph\n\n## Graph\nbeta details\n",
        encoding="utf-8",
    )
    (domain / "global.md").write_text(
        "---\ndescription: beta global\n---\n# Global\n\n## Global\nalpha details\n",
        encoding="utf-8",
    )

    def fake_index_embed(cfg, texts):
        return [[1.0, 0.0] if "alpha" in text.lower() else [0.0, 1.0]
                for text in texts]

    monkeypatch.setattr(indexer, "embed_texts", fake_index_embed)
    indexer.index_domain(_cfg(), str(base), "d")
    return str(base)


def _long_lexical_page(tmp_path, monkeypatch):
    base = tmp_path / "wiki"
    domain = base / "d"
    domain.mkdir(parents=True)
    (domain / "long.md").write_text(
        "---\ndescription: long page\ntags: [wanted]\n---\n"
        "# Long\n\n## Details\none two three needle five six\n",
        encoding="utf-8",
    )
    cfg = replace(_cfg(), chunk_size=3, chunk_overlap=0)
    monkeypatch.setattr(
        indexer, "embed_texts", lambda cfg, texts: [[1.0, 0.0] for _ in texts]
    )
    indexer.index_domain(cfg, str(base), "d")
    return cfg, str(base)


def test_lexical_signal_targets_exact_current_chunk(tmp_path, monkeypatch):
    cfg, base = _long_lexical_page(tmp_path, monkeypatch)

    signals = retrieval._domain_signals(
        cfg, base, "d", "needle", None, "lexical", 10, 0.0, None, None, {}
    )

    direct = signals["lexical_section"]
    assert [(hit["file"], hit["heading"], hit["chunk"]) for hit in direct] == [
        ("long.md", "Details", 1)
    ]


def test_lexical_signal_omits_stale_indexed_chunk(tmp_path, monkeypatch):
    cfg, base = _long_lexical_page(tmp_path, monkeypatch)
    (Path(base) / "d" / "long.md").write_text(
        "---\ndescription: long page\n---\n"
        "# Long\n\n## Details\nreplacement two three needle five six\n",
        encoding="utf-8",
    )

    signals = retrieval._domain_signals(
        cfg, base, "d", "replacement", None, "lexical", 10, 0.0, None, None, {}
    )

    assert signals.get("lexical_section", []) == []


def test_lexical_collision_uses_all_loaded_sections(tmp_path, monkeypatch):
    cfg, base = _long_lexical_page(tmp_path, monkeypatch)
    path = wiki_base.index_path(base, "d")
    records = store.load_index(path)
    section = next(
        record for record in records
        if record.kind == "section" and record.heading == "Details" and record.chunk == 0
    )
    store.save_index(path, [*records, replace(section, tags=["other"])])

    signals = retrieval._domain_signals(
        cfg, base, "d", "one", None, "lexical", 10, 0.0, None, ["wanted"], {}
    )

    assert signals.get("lexical_section", []) == []


def test_lexical_signal_distinguishes_repeated_heading_chunks(tmp_path, monkeypatch):
    base = tmp_path / "wiki"
    domain = base / "d"
    domain.mkdir(parents=True)
    (domain / "repeated.md").write_text(
        "---\ndescription: repeated page\n---\n# Repeated\n\n"
        "## Setup\nfirst\n\n## Setup\nneedle later\n",
        encoding="utf-8",
    )
    cfg = replace(_cfg(), chunk_size=3, chunk_overlap=0)
    monkeypatch.setattr(
        indexer, "embed_texts", lambda cfg, texts: [[1.0, 0.0] for _ in texts]
    )
    indexer.index_domain(cfg, str(base), "d")

    signals = retrieval._domain_signals(
        cfg, str(base), "d", "needle", None, "lexical", 10, 0.0, None, None, {}
    )

    direct = signals["lexical_section"]
    assert [(hit["file"], hit["heading"], hit["chunk"]) for hit in direct] == [
        ("repeated.md", "Setup", 1)
    ]


def test_materialize_page_rejects_change_during_read(tmp_path, monkeypatch):
    cfg, base = _long_lexical_page(tmp_path, monkeypatch)
    cache = {}
    stamps = iter(((1, 2, 10, 100), (1, 2, 20, 200)))
    monkeypatch.setattr(retrieval, "_file_stamp", lambda path: next(stamps))

    page = retrieval._materialize_page(cfg, base, "d", "long.md", cache)

    assert page is None
    assert cache[("d", "long.md")] is None


def test_materialize_page_rejects_file_symlink_swap_after_validation(
        tmp_path, monkeypatch):
    cfg, base = _long_lexical_page(tmp_path, monkeypatch)
    page_path = Path(base) / "d" / "long.md"
    secret = tmp_path / "secret.md"
    secret.write_text(
        "# Secret\n\n## Details\nexternal secret needle\n", encoding="utf-8"
    )
    original_domain_file_path = retrieval._domain_file_path
    swapped = False

    def swap_after_validation(base, domain, file):
        nonlocal swapped
        validated = original_domain_file_path(base, domain, file)
        if validated is not None and not swapped:
            swapped = True
            page_path.unlink()
            try:
                page_path.symlink_to(secret)
            except OSError:
                pytest.skip("symlinks are not supported")
        return validated

    monkeypatch.setattr(retrieval, "_domain_file_path", swap_after_validation)
    cache = {}

    page = retrieval._materialize_page(cfg, base, "d", "long.md", cache)

    assert page is None
    assert cache[("d", "long.md")] is None


def test_materialize_page_rejects_parent_symlink_swap_after_validation(
        tmp_path, monkeypatch):
    base = tmp_path / "wiki"
    nested = base / "d" / "nested"
    nested.mkdir(parents=True)
    (nested / "page.md").write_text(
        "# Page\n\n## Details\nsafe text\n", encoding="utf-8"
    )
    external = tmp_path / "external"
    external.mkdir()
    (external / "page.md").write_text(
        "# Secret\n\n## Details\nexternal secret\n", encoding="utf-8"
    )
    original_domain_file_path = retrieval._domain_file_path
    swapped = False

    def swap_after_validation(base, domain, file):
        nonlocal swapped
        validated = original_domain_file_path(base, domain, file)
        if validated is not None and not swapped:
            swapped = True
            nested.rename(nested.with_name("nested-original"))
            try:
                nested.symlink_to(external, target_is_directory=True)
            except OSError:
                pytest.skip("symlinks are not supported")
        return validated

    monkeypatch.setattr(retrieval, "_domain_file_path", swap_after_validation)
    cache = {}

    page = retrieval._materialize_page(
        _cfg(), str(base), "d", "nested/page.md", cache
    )

    assert page is None
    assert cache[("d", "nested/page.md")] is None


def test_read_domain_file_rejects_fifo_without_blocking_or_leaking_fd(tmp_path):
    if not hasattr(os, "mkfifo") or "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("FIFO fork test requires POSIX")
    base = tmp_path / "wiki"
    domain = base / "d"
    domain.mkdir(parents=True)
    os.mkfifo(domain / "pipe.md")
    context = multiprocessing.get_context("fork")
    results = context.Queue()

    def read_fifo():
        before = len(os.listdir("/proc/self/fd"))
        result = retrieval._read_domain_file(str(base), "d", "pipe.md")
        after = len(os.listdir("/proc/self/fd"))
        results.put((result, before, after))

    process = context.Process(target=read_fifo)
    process.start()
    process.join(1)
    blocked = process.is_alive()
    outcome = None
    if blocked:
        process.terminate()
        process.join()
    else:
        outcome = results.get(timeout=1)
    results.close()
    results.join_thread()

    assert not blocked
    result, before, after = outcome
    assert result is None
    assert after == before


def test_read_domain_file_fails_closed_when_dir_fd_is_unsupported(
        tmp_path, monkeypatch):
    cfg, base = _long_lexical_page(tmp_path, monkeypatch)
    del cfg
    real_open = retrieval.os.open
    opened = []

    def unsupported_dir_fd(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None:
            raise NotImplementedError("dir_fd unavailable")
        fd = real_open(path, flags, mode)
        opened.append(fd)
        return fd

    monkeypatch.setattr(retrieval.os, "open", unsupported_dir_fd)

    assert retrieval._read_domain_file(base, "d", "long.md") is None
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_lexical_page_seed_uses_source_term_frequency_not_overlap(
        tmp_path, monkeypatch):
    base = tmp_path / "wiki"
    domain = base / "d"
    domain.mkdir(parents=True)
    (domain / "overlap.md").write_text(
        "---\ndescription: overlap page\n---\n# Overlap\n\n"
        "## Details\none two needle four five\n",
        encoding="utf-8",
    )
    (domain / "twice.md").write_text(
        "---\ndescription: twice page\n---\n# Twice\n\n"
        "## Details\nneedle x needle\n",
        encoding="utf-8",
    )
    cfg = replace(_cfg(), chunk_size=3, chunk_overlap=2, seed_top_k=1)
    monkeypatch.setattr(
        indexer, "embed_texts", lambda cfg, texts: [[1.0, 0.0] for _ in texts]
    )
    indexer.index_domain(cfg, str(base), "d")

    signals = retrieval._domain_signals(
        cfg, str(base), "d", "needle", None, "lexical", 10, 0.0, None, None, {}
    )

    assert {hit["file"] for hit in signals["lexical_page"]} == {"twice.md"}


@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
def test_semantic_modes_embed_query_once(tmp_path, monkeypatch, mode):
    base = _seed(tmp_path, monkeypatch)
    calls = []

    def fake_query_embed(cfg, texts):
        calls.append(texts)
        return [[1.0, 0.0]]

    monkeypatch.setattr(retrieval, "embed_texts", fake_query_embed)

    retrieval.search_read(_cfg(), base, ["d"], "alpha", 5, 0.0, mode=mode)

    assert calls == [["alpha"]]


def test_lexical_mode_never_embeds_and_returns_only_lexical_hits(tmp_path, monkeypatch):
    base = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(
        retrieval, "embed_texts",
        lambda cfg, texts: (_ for _ in ()).throw(AssertionError("embedded query")),
    )

    hits = retrieval.search_read(
        _cfg(), base, ["d"], "refresh_token", 10, 0.0, mode="lexical"
    )

    assert hits
    assert all(hit["hit"] == "lexical" for hit in hits)


def test_hybrid_duplicate_hit_is_both(tmp_path, monkeypatch):
    base = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(retrieval, "embed_texts", lambda cfg, texts: [[1.0, 0.0]])

    hits = retrieval.search_read(
        _cfg(), base, ["d"], "refresh_token alpha", 10, 0.0, mode="hybrid"
    )

    duplicate = next(hit for hit in hits
                     if hit["file"] == "seed.md" and hit["heading"] == "Match")
    assert duplicate["hit"] == "both"


def test_semantic_includes_global_section_outside_seed_graph(tmp_path, monkeypatch):
    base = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(retrieval, "embed_texts", lambda cfg, texts: [[1.0, 0.0]])

    hits = retrieval.search_read(
        _cfg(), base, ["d"], "alpha", 10, 0.5, mode="semantic"
    )

    global_hit = next(hit for hit in hits if hit["file"] == "global.md")
    assert global_hit["source"] == "global"


def test_semantic_seed_receives_graph_page_signal_and_keeps_seed_source(
        tmp_path, monkeypatch):
    base = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(retrieval, "embed_texts", lambda cfg, texts: [[1.0, 0.0]])

    hits = retrieval.prepare_read_candidates(
        _cfg(), base, ["d"], "alpha", 10, 0.5, mode="semantic"
    )

    seed = next(hit for hit in hits if hit["file"] == "seed.md")
    assert seed["score"] == pytest.approx(2 / 61 + 1 / 62)
    assert seed["source"] == "seed"


def test_lexical_seed_expands_graph_without_embedding(tmp_path, monkeypatch):
    base = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(
        retrieval, "embed_texts",
        lambda cfg, texts: (_ for _ in ()).throw(AssertionError("embedded query")),
    )

    hits = retrieval.search_read(
        _cfg(), base, ["d"], "refresh_token", 10, 0.0, mode="lexical"
    )

    graph_hit = next(hit for hit in hits if hit["file"] == "graph.md")
    assert graph_hit["source"] == "graph"
    assert graph_hit["hit"] == "lexical"


def test_candidate_limit_has_floor_but_never_reduces_top_k(monkeypatch):
    monkeypatch.setattr(retrieval, "CANDIDATE_LIMIT", 2)

    assert retrieval._candidate_limit(5) == 5
    assert retrieval._candidate_limit(1) == 2


def test_invalid_mode_lists_allowed_values():
    with pytest.raises(
        ValueError,
        match="invalid search mode: bogus; allowed values: hybrid, lexical, semantic",
    ):
        retrieval.search_read(_cfg(), "base", ["d"], "q", 10, 0.0, mode="bogus")


def test_empty_request_does_not_embed(monkeypatch):
    monkeypatch.setattr(
        retrieval, "embed_texts",
        lambda cfg, texts: (_ for _ in ()).throw(AssertionError("embedded query")),
    )

    assert retrieval.prepare_read_candidates(
        _cfg(), "base", [], "q", 10, 0.0, mode="semantic"
    ) == []
    assert retrieval.prepare_read_candidates(
        _cfg(), "base", ["d"], "q", 0, 0.0, mode="semantic"
    ) == []


def test_vector_search_is_semantic_compatibility_alias(tmp_path, monkeypatch):
    base = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(retrieval, "embed_texts", lambda cfg, texts: [[1.0, 0.0]])

    hits = retrieval.vector_search(_cfg(), base, ["d"], "alpha", 10, 0.0)

    assert hits
    assert all(hit["hit"] == "semantic" for hit in hits)
    json.dumps(hits)


def test_lexical_search_delegates_to_canonical_flow(tmp_path, monkeypatch):
    cfg, base = _long_lexical_page(tmp_path, monkeypatch)

    hits = retrieval.lexical_search(
        cfg, base, ["d"], "needle", 10, type=None, tags=None
    )

    direct = next(hit for hit in hits if hit["heading"] == "Details")
    assert direct["chunk"] == 1


def test_hydrate_candidates_preserves_order_and_exact_chunks(tmp_path, monkeypatch):
    base = tmp_path / "wiki"
    page = base / "d" / "nested" / "page.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ndescription: secret frontmatter\n---\n# Page\n\n"
        "## Long\none two three four five six\n\n## Other\nno leakage\n",
        encoding="utf-8",
    )
    cfg = replace(_cfg(), chunk_size=3, chunk_overlap=0)
    monkeypatch.setattr(indexer, "embed_texts",
                        lambda cfg, texts: [[1.0, 0.0] for _ in texts])
    indexer.index_domain(cfg, str(base), "d")
    candidates = [
        {"domain": "d", "file": "nested/page.md", "heading": "Long", "chunk": 1,
         "score": 0.2, "hit": "semantic", "source": "global"},
        {"domain": "d", "file": "nested/page.md", "heading": "Long", "chunk": 0,
         "score": 0.1, "hit": "semantic", "source": "seed"},
    ]

    hydrated = retrieval.hydrate_candidates(cfg, str(base), candidates)

    assert [(hit["heading"], hit["chunk"]) for hit in hydrated] == [
        ("Long", 1), ("Long", 0)
    ]
    assert hydrated[0]["text"] == "## Long\nfour five six"
    assert "frontmatter" not in hydrated[0]["text"]
    assert "Other" not in hydrated[0]["text"]


def test_hydrate_candidates_omits_stale_heading_and_missing_page(tmp_path, monkeypatch):
    base = tmp_path / "wiki"
    page = base / "d" / "page.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Page\n\n## Current\ntext\n", encoding="utf-8")
    monkeypatch.setattr(indexer, "embed_texts",
                        lambda cfg, texts: [[1.0, 0.0] for _ in texts])
    indexer.index_domain(_cfg(), str(base), "d")
    candidates = [
        {"domain": "d", "file": "page.md", "heading": "Stale", "chunk": 0},
        {"domain": "d", "file": "missing.md", "heading": "Current", "chunk": 0},
    ]

    assert retrieval.hydrate_candidates(_cfg(), str(base), candidates) == []


def test_hydrate_candidates_omits_changed_indexed_chunk(tmp_path, monkeypatch):
    base = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(retrieval, "embed_texts", lambda cfg, texts: [[1.0, 0.0]])
    candidates = retrieval.search_read(
        _cfg(), base, ["d"], "alpha", 10, 0.5, mode="semantic"
    )
    old = next(hit for hit in candidates
               if hit["file"] == "seed.md" and hit["heading"] == "Match")
    page = tmp_path / "wiki" / "d" / "seed.md"
    page.write_text(
        "---\ndescription: alpha seed\n---\n# Seed\n\n"
        "## Match\ncompletely unrelated replacement\n\n[Graph](graph.md)\n",
        encoding="utf-8",
    )

    assert retrieval.hydrate_candidates(_cfg(), base, [old]) == []


def _poisoned_section(base, file, content):
    chunk = next(chunk for chunk in chunk_markdown(
        file, content, _cfg().chunk_size, _cfg().chunk_overlap, _cfg().summary_max
    ) if chunk.kind == "section")
    rec = store.make_record(chunk, [1.0, 0.0])
    store.save_index(wiki_base.index_path(str(base), "d"), [rec])
    return rec


def _assert_secret_not_read(monkeypatch, secret, callback):
    real_open = builtins.open
    reads = []

    def guarded_open(file, *args, **kwargs):
        if Path(file).resolve() == secret.resolve():
            reads.append(str(file))
            raise AssertionError("secret read")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    assert callback() == []
    assert reads == []


def test_retrieval_and_hydration_reject_traversal_file(tmp_path, monkeypatch):
    base = tmp_path / "wiki"
    (base / "d").mkdir(parents=True)
    secret = base / "secret.md"
    content = "# Secret\n\n## Secret\nprivate alpha\n"
    secret.write_text(content, encoding="utf-8")
    rec = _poisoned_section(base, "../secret.md", content)
    monkeypatch.setattr(retrieval, "embed_texts", lambda cfg, texts: [[1.0, 0.0]])

    assert retrieval.prepare_read_candidates(
        _cfg(), str(base), ["d"], "alpha", 10, -1.0, mode="semantic"
    ) == []
    candidate = {
        "domain": "d", "file": rec.file, "heading": rec.heading,
        "chunk": rec.chunk, "score": 1.0, "hit": "semantic", "source": "global",
    }
    _assert_secret_not_read(
        monkeypatch, secret,
        lambda: retrieval.hydrate_candidates(_cfg(), str(base), [candidate]),
    )


def test_retrieval_and_hydration_reject_symlink_escape(tmp_path, monkeypatch):
    base = tmp_path / "wiki"
    domain = base / "d"
    domain.mkdir(parents=True)
    secret = tmp_path / "secret.md"
    content = "# Secret\n\n## Secret\nprivate alpha\n"
    secret.write_text(content, encoding="utf-8")
    link = domain / "linked.md"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("symlinks are not supported")
    rec = _poisoned_section(base, "linked.md", content)
    monkeypatch.setattr(retrieval, "embed_texts", lambda cfg, texts: [[1.0, 0.0]])

    assert retrieval.prepare_read_candidates(
        _cfg(), str(base), ["d"], "alpha", 10, -1.0, mode="semantic"
    ) == []
    candidate = {
        "domain": "d", "file": rec.file, "heading": rec.heading,
        "chunk": rec.chunk, "score": 1.0, "hit": "semantic", "source": "global",
    }
    _assert_secret_not_read(
        monkeypatch, secret,
        lambda: retrieval.hydrate_candidates(_cfg(), str(base), [candidate]),
    )
