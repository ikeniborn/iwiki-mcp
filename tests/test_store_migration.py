import os
from iwiki_mcp import base


def test_index_and_log_paths_at_domain_root(tmp_path):
    b = str(tmp_path)
    assert base.index_path(b, "d") == os.path.join(b, "d", "index.jsonl")
    assert base.log_path(b, "d") == os.path.join(b, "d", "log.jsonl")


def test_migrate_moves_legacy_iwiki_store_to_root(tmp_path):
    dom = tmp_path / "d"
    (dom / ".iwiki").mkdir(parents=True)
    (dom / ".iwiki" / "index.jsonl").write_text("{}\n", encoding="utf-8")
    (dom / ".iwiki" / "log.jsonl").write_text("{}\n", encoding="utf-8")

    base.migrate_store_location(str(tmp_path), "d")

    assert (dom / "index.jsonl").is_file()
    assert (dom / "log.jsonl").is_file()
    assert not (dom / ".iwiki").exists()  # empty legacy dir removed


def test_migrate_is_idempotent_and_never_clobbers(tmp_path):
    dom = tmp_path / "d"
    dom.mkdir(parents=True)
    (dom / "index.jsonl").write_text("NEW\n", encoding="utf-8")
    (dom / ".iwiki").mkdir()
    (dom / ".iwiki" / "index.jsonl").write_text("OLD\n", encoding="utf-8")

    base.migrate_store_location(str(tmp_path), "d")  # root already has index.jsonl

    assert (dom / "index.jsonl").read_text() == "NEW\n"  # not clobbered
    base.migrate_store_location(str(tmp_path), "d")  # second run: no error
