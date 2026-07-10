from iwiki_mcp.engine import store


class _C:
    id = "a.md#H"
    file = "a.md"
    heading = "H"
    chunk = 0
    hash = "abc"
    kind = "summary"
    ordinal = 3
    type = "reference"
    tags = ["x"]


def test_make_record_carries_kind_ordinal_and_schema_version():
    r = store.make_record(_C(), [0.1, 0.2])
    assert r.kind == "summary"
    assert r.ordinal == 3
    assert r.v == store.SCHEMA_VERSION == 2


def test_old_jsonl_without_kind_loads_with_defaults(tmp_path):
    p = tmp_path / "index.jsonl"
    p.write_text('{"id":"a.md#H","file":"a.md","heading":"H","chunk":0,'
                 '"hash":"h","dim":2,"scale":1.0,"q":[1,2]}\n', encoding="utf-8")
    recs = store.load_index(str(p))
    assert recs[0].kind == "section"
    assert recs[0].v == 1


def test_quantize_dequantize_roundtrip():
    vec = [0.1, -0.5, 0.9, -1.0, 0.0]
    scale, q = store.quantize(vec)
    out = store.dequantize(scale, q)
    assert all(abs(a - b) <= scale for a, b in zip(vec, out))


def test_quantize_zero_vector():
    scale, q = store.quantize([0.0, 0.0, 0.0])
    assert q == [0, 0, 0]
    assert scale == 1.0


def test_cosine_identical_is_one():
    assert abs(store.cosine([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) - 1.0) < 1e-9


def test_cosine_zero_vector_is_zero():
    assert store.cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
