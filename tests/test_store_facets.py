import json
from iwiki_mcp.engine.store import make_record, load_index


class _C:
    id = "p.md#H"
    file = "p.md"
    heading = "H"
    chunk = 0
    hash = "abc"
    type = "api"
    tags = ["x", "y"]


def test_make_record_copies_type_tags():
    r = make_record(_C(), [0.1, 0.2])
    assert r.type == "api"
    assert r.tags == ["x", "y"]


def test_old_jsonl_without_facets_loads(tmp_path):
    p = tmp_path / "index.jsonl"
    rec = {"id": "p.md#H", "file": "p.md", "heading": "H", "chunk": 0,
           "hash": "abc", "dim": 2, "scale": 1.0, "q": [1, 2]}
    p.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    recs = load_index(str(p))
    assert recs[0].type is None
    assert recs[0].tags == []
