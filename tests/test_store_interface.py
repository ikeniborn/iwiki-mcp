from iwiki_mcp.engine.store import VectorStore, make_record
from iwiki_mcp.engine.chunk import Chunk


def _chunk(heading, text):
    return Chunk(file="a.md", heading=heading, chunk=0, text=text, hash="h" + heading)


def test_store_roundtrip_and_query(tmp_path):
    idx = str(tmp_path / "index.jsonl")
    store = VectorStore(idx)
    recs = [
        make_record(_chunk("One", "x"), [1.0, 0.0]),
        make_record(_chunk("Two", "y"), [0.0, 1.0]),
    ]
    store.save(recs)
    assert len(store.load()) == 2
    hits = store.query([1.0, 0.0], top_k=1, threshold=0.1)
    assert hits and hits[0]["heading"] == "One"
