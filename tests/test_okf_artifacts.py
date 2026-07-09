from iwiki_mcp.engine.okf_artifacts import RESERVED_OKF, render_index, render_log


def test_reserved_okf_constant():
    assert RESERVED_OKF == ("index.md", "log.md")


def test_render_index_sorted_links():
    assert render_index(["b", "a"]) == "# Index\n\n- [a](a.md)\n- [b](b.md)\n"


def test_render_index_empty():
    assert render_index([]) == "# Index\n\n"


def test_render_log_lines():
    recs = [{"date": "2026-07-01", "op": "ingest", "page": "a.md"}]
    assert render_log(recs) == "# Log\n\n- 2026-07-01 ingest a.md\n"


def test_render_log_empty():
    assert render_log([]) == "# Log\n\n"
