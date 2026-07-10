from iwiki_mcp import okf
from iwiki_mcp.engine import frontmatter as fm
from iwiki_mcp.engine.config import Config


def _cfg():
    return Config(base_url="http://x", api_key="k", embed_model="e", chat_model=None,
                  dimensions=2, chunk_size=512, chunk_overlap=64, summary_max=400,
                  top_k=8, score_threshold=0.2, graph_depth=2, ignore=None)


def _build(body, **kw):
    block, warning = okf.build_frontmatter(
        _cfg(), "/b", "d", "s", body, source=None, explicit_type="person",
        explicit_tags=None, timestamp_path="d/s.md", **kw)
    meta, _ = fm.split(block + "# x\n")
    return meta, warning


def test_explicit_description_and_status():
    meta, warning = _build("# X\n\n## Role\nprose\n",
                           explicit_description="Alice covers AR.", explicit_status="Stable")
    assert meta["description"] == "Alice covers AR."
    assert meta["status"] == "stable"
    assert warning is None


def test_status_defaults_to_stub():
    meta, _ = _build("# X\n\n## Role\nprose\n", explicit_description="d")
    assert meta["status"] == "stub"


def test_open_type_kept():
    meta, _ = _build("# X\n\n## Role\nprose\n", explicit_description="d")
    assert meta["type"] == "person"            # not clamped to concept


def test_transitional_overview_derive():
    meta, warning = _build("# X\n\n## Overview\nsummary here\n\n## Role\nprose\n")
    assert meta["description"] == "summary here"
    assert warning is None


def test_missing_description_warns():
    meta, warning = _build("# X\n\n## Role\nprose\n")
    assert "description" not in meta
    assert "description" in warning


def test_strip_overview_first_section_only():
    body = "# X\n\n## Overview\nsum\n\n## Role\nprose\n"
    new_body, text = okf._strip_overview(body, 400)
    assert text == "sum"
    assert "## Overview" not in new_body
    assert "## Role" in new_body


def test_strip_overview_ignores_non_first():
    body = "# X\n\n## Role\nprose\n\n## Overview\nnot first\n"
    new_body, text = okf._strip_overview(body, 400)
    assert text == ""
    assert new_body == body
