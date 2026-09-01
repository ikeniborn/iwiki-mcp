"""Nested code frontmatter stays authored across all rewrite paths."""
from __future__ import annotations

import subprocess

import pytest

from iwiki_mcp import indexer, okf, server
from iwiki_mcp.engine import frontmatter as fm
from iwiki_mcp.engine.chunk import chunk_markdown
from iwiki_mcp.engine.validate import validate_page


AUTHORED = {
    "symbols": [{"qualified_name": "pkg.Service.run"}],
    "files": ["src/pkg/service.py"],
    "source_globs": ["src/pkg/**"],
}


def _patch_server(monkeypatch, tmp_path):
    (tmp_path / "d").mkdir()
    binding = server.base.Binding(
        base=str(tmp_path), read=("d",), write="d", project_dir=str(tmp_path)
    )
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setattr(server.base, "resolve_binding", lambda: binding)
    monkeypatch.setattr(server.sync, "ensure_fresh", lambda _base: {"state": "clean"})
    monkeypatch.setattr(
        server.sync,
        "commit_and_push",
        lambda *_args, **_kwargs: {"committed": True, "pushed": False},
    )
    monkeypatch.setattr(
        indexer, "embed_texts", lambda _cfg, texts: [[0.1, 0.2] for _ in texts]
    )


def _authored_markdown():
    return fm.render({"code": AUTHORED}) + (
        "# Service\n\n## Overview\nSelector page.\n\n## Notes\nOriginal.\n"
    )


def test_nested_code_mapping_round_trips_without_constructing_yaml_objects():
    tagged = {
        "symbols": [{"qualified_name": "!python/object:pkg.Service.run"}],
        "files": ["src/pkg/service.py"],
        "source_globs": ["src/pkg/**"],
    }
    rendered = fm.render({"type": "concept", "code": tagged})
    parsed, _ = fm.split(rendered + "# Service\n")

    assert parsed["code"] == tagged
    assert type(parsed["code"]["symbols"][0]["qualified_name"]) is str


def test_inline_code_list_with_quoted_comma_round_trips_exactly():
    markdown = '---\ncode:\n  files: ["src/a,b.py"]\n---\n# Service\n'

    parsed, body = fm.split(markdown, strict_code=True)

    assert parsed["code"] == {"files": ["src/a,b.py"]}
    assert fm.split(fm.render(parsed) + body, strict_code=True)[0] == parsed


@pytest.mark.parametrize(
    "nested",
    [
        "code:\n  files:\n    - src/a.py\ncode:\n  files:\n    - src/b.py\n",
        "code:\n  files:\n    - src/a.py\n  files:\n    - src/b.py\n",
        (
            "code:\n  symbols:\n"
            "    - qualified_name: pkg.Service.run\n"
            "      qualified_name: pkg.Other.run\n"
        ),
        (
            "code:\n  symbols:\n"
            "    - qualified_name: pkg.Service.run\n"
            "      signature: first\n      signature: second\n"
        ),
        (
            "code:\n  symbols:\n"
            "    - qualified_name: pkg.Service.run\n"
            "      kind: class\n      kind: function\n"
        ),
        "code:\n  symbols: []\n  symbols: []\n",
        "code:\n  source_globs: [\"src/**\"]\n  source_globs: [\"tests/**\"]\n",
    ],
)
def test_strict_parser_rejects_duplicate_nested_code_mapping_keys(nested):
    markdown = f"---\n{nested}---\n# Service\n"

    with pytest.raises(fm.FrontmatterError, match="duplicate"):
        fm.split(markdown, strict_code=True)


def test_fail_soft_split_render_preserves_duplicate_nested_code_bytes():
    markdown = (
        "---\ntype: concept\ncode:\n  files:\n    - src/a.py\n"
        "  files:\n    - src/b.py\n---\n# Service\n"
    )

    meta, body = fm.split(markdown)

    assert fm.render(meta) + body == markdown


def test_build_frontmatter_preserves_authored_code_mapping():
    class Config:
        chat_model = None

    block, _warning = okf.build_frontmatter(
        Config(), "/base", "d", "service",
        "# Service\n\n## Overview\nSelector page.\n",
        source=None,
        explicit_type="concept",
        explicit_tags=None,
        explicit_description=None,
        explicit_status=None,
        timestamp_path="d/concept/service.md",
        authored_code=AUTHORED,
    )

    meta, _ = fm.split(block + "# Service\n")
    assert meta["code"] == AUTHORED


def test_write_update_round_trip_preserves_authored_code_mapping(
    tmp_path, monkeypatch
):
    _patch_server(monkeypatch, tmp_path)

    written = server.wiki_write_page("d", "service", _authored_markdown())
    assert "error" not in written
    page = tmp_path / "d" / "concept" / "service.md"
    written_meta, _ = fm.split(page.read_text(encoding="utf-8"))
    assert written_meta["code"] == AUTHORED

    updated = server.wiki_update_page(
        "d", "concept/service", "Notes", "Updated."
    )
    assert "error" not in updated
    updated_meta, _ = fm.split(page.read_text(encoding="utf-8"))
    assert updated_meta["code"] == AUTHORED


def test_code_only_update_sets_selectors_and_preserves_body_exactly(
    tmp_path, monkeypatch
):
    _patch_server(monkeypatch, tmp_path)
    monkeypatch.setenv("IWIKI_EMBED_DIMENSIONS", "2")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert server.sync.is_git_repo(str(tmp_path))
    page = tmp_path / "d" / "concept" / "service.md"
    original_body = (
        "# Service\n\n## Overview\nExact body.  \n\n## Notes\nOriginal.\n"
    )
    written = server.wiki_write_page(
        "d", "service", original_body,
        type="concept", description="Existing",
    )
    assert "error" not in written
    _meta, original_body = fm.split(page.read_text(encoding="utf-8"))
    reindexes = []
    commits = []
    index_domain = indexer.index_domain

    def reindex_once(*args, **kwargs):
        reindexes.append((args, kwargs))
        return index_domain(*args, **kwargs)

    def commit_once(*args, **kwargs):
        commits.append((args, kwargs))
        return {"committed": True, "pushed": False}

    monkeypatch.setattr(indexer, "index_domain", reindex_once)
    monkeypatch.setattr(server.sync, "commit_and_push", commit_once)

    result = server.wiki_update_page(
        "d", "concept/service", code={"files": ["src/pkg/service.py"]}
    )

    assert "error" not in result
    assert "heading" not in result
    assert result["embedded"] == 0
    assert result["reused"] > 0
    assert len(reindexes) == 1
    assert len(commits) == 1
    updated_meta, updated_body = fm.split(page.read_text(encoding="utf-8"))
    assert updated_meta["code"] == {"files": ["src/pkg/service.py"]}
    assert updated_body == original_body


def test_git_code_only_update_preserves_crlf_body_bytes(tmp_path, monkeypatch):
    _patch_server(monkeypatch, tmp_path)
    page = tmp_path / "d" / "concept" / "service.md"
    page.parent.mkdir(parents=True)
    original_body = (
        b"# Service\r\n\r\n"
        b"## Overview\r\nExact body.  \r\n\r\n"
        b"## Notes\r\nOriginal.\r\n"
    )
    page.write_bytes(
        b"---\r\n"
        b"type: concept\r\n"
        b"description: Existing\r\n"
        b"---\r\n"
        + original_body
    )

    result = server.wiki_update_page(
        "d", "concept/service", code={"files": ["src/pkg/service.py"]}
    )

    assert "error" not in result
    updated = page.read_bytes()
    assert updated.endswith(original_body)
    updated_meta, updated_body = fm.split(
        updated.decode("utf-8"), strict_code=True
    )
    assert updated_meta["code"] == {"files": ["src/pkg/service.py"]}
    assert updated_body.encode("utf-8") == original_body


def test_code_only_update_replaces_existing_selector_mapping(tmp_path, monkeypatch):
    _patch_server(monkeypatch, tmp_path)
    page = tmp_path / "d" / "concept" / "service.md"
    page.parent.mkdir(parents=True)
    page.write_text(_authored_markdown(), encoding="utf-8")

    result = server.wiki_update_page(
        "d", "concept/service", code={"source_globs": ["lib/**"]}
    )

    assert "error" not in result
    updated_meta, _ = fm.split(page.read_text(encoding="utf-8"))
    assert updated_meta["code"] == {"source_globs": ["lib/**"]}


@pytest.mark.parametrize(
    "code",
    [
        {},
        {"symbols": [], "files": [], "source_globs": []},
    ],
)
def test_code_only_update_removes_selectors_when_mapping_is_empty(
    tmp_path, monkeypatch, code
):
    _patch_server(monkeypatch, tmp_path)
    page = tmp_path / "d" / "concept" / "service.md"
    page.parent.mkdir(parents=True)
    page.write_text(_authored_markdown(), encoding="utf-8")

    result = server.wiki_update_page("d", "concept/service", code=code)

    assert "error" not in result
    updated_meta, _ = fm.split(page.read_text(encoding="utf-8"))
    assert "code" not in updated_meta


def test_invalid_code_update_skips_freshness_and_preserves_original_bytes(
    tmp_path, monkeypatch
):
    _patch_server(monkeypatch, tmp_path)
    page = tmp_path / "d" / "concept" / "service.md"
    page.parent.mkdir(parents=True)
    original = _authored_markdown()
    page.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        server.cross_domain,
        "recover_pending_transactions",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("recovery called")
        ),
    )
    monkeypatch.setattr(
        server.sync,
        "ensure_fresh",
        lambda *_: (_ for _ in ()).throw(AssertionError("freshness called")),
    )

    result = server.wiki_update_page(
        "d", "concept/service", code={"modules": ["pkg.service"]}
    )

    assert result == {
        "error": "unsupported code selector key",
        "hint": "use only code.symbols, code.files, and code.source_globs",
    }
    assert page.read_text(encoding="utf-8") == original


def test_combined_update_changes_section_and_selectors_with_one_commit(
    tmp_path, monkeypatch
):
    _patch_server(monkeypatch, tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert server.sync.is_git_repo(str(tmp_path))
    page = tmp_path / "d" / "concept" / "service.md"
    page.parent.mkdir(parents=True)
    page.write_text(_authored_markdown(), encoding="utf-8")
    commits = []

    def commit_once(*args, **kwargs):
        commits.append((args, kwargs))
        return {"committed": True, "pushed": False}

    monkeypatch.setattr(server.sync, "commit_and_push", commit_once)

    result = server.wiki_update_page(
        "d",
        "concept/service",
        "Notes",
        "Combined update.",
        code={"symbols": [{"qualified_name": "pkg.Service.run"}]},
    )

    assert "error" not in result
    assert result["heading"] == "Notes"
    assert len(commits) == 1
    updated_meta, updated_body = fm.split(page.read_text(encoding="utf-8"))
    assert updated_meta["code"] == {
        "symbols": [{"qualified_name": "pkg.Service.run"}]
    }
    assert "Combined update." in updated_body
    assert "Original." not in updated_body


def test_write_rejects_unknown_code_selector_key(tmp_path, monkeypatch):
    _patch_server(monkeypatch, tmp_path)
    markdown = fm.render({"code": {"modules": ["pkg.service"]}}) + (
        "# Service\n\n## Notes\nInvalid selector.\n"
    )

    result = server.wiki_write_page("d", "service", markdown)

    assert "unsupported code selector key" in result["error"]
    assert not (tmp_path / "d" / "concept" / "service.md").exists()


def test_write_rejects_duplicate_code_without_creating_page(
    tmp_path, monkeypatch
):
    _patch_server(monkeypatch, tmp_path)
    markdown = (
        "---\ncode:\n  files:\n    - src/a.py\n"
        "code:\n  files:\n    - src/b.py\n---\n"
        "# Service\n\n## Notes\nInvalid selector.\n"
    )

    result = server.wiki_write_page("d", "service", markdown)

    assert "duplicate" in result["error"]
    assert not (tmp_path / "d" / "concept" / "service.md").exists()


@pytest.mark.parametrize(
    "nested",
    [
        "    modules:\n      - pkg.service\n",
        " modules:\n   - pkg.service\n",
        "\tmodules:\n\t  - pkg.service\n",
        (
            "  symbols:\n"
            "    - qualified_name: pkg.Service.run\n"
            "        module_id: forbidden\n"
        ),
        "  symbols:\n    - qualified_name: pkg.Service.run\n    bad-line\n",
    ],
)
def test_write_rejects_malformed_nested_code_without_dropping_authored_lines(
    tmp_path, monkeypatch, nested
):
    _patch_server(monkeypatch, tmp_path)
    markdown = (
        "---\ncode:\n" + nested + "---\n"
        "# Service\n\n## Notes\nInvalid selector.\n"
    )

    result = server.wiki_write_page("d", "service", markdown)

    assert "invalid nested code frontmatter" in result["error"]
    assert not (tmp_path / "d" / "concept" / "service.md").exists()


def test_update_rejects_duplicate_code_and_preserves_original_bytes(
    tmp_path, monkeypatch
):
    _patch_server(monkeypatch, tmp_path)
    page = tmp_path / "d" / "concept" / "service.md"
    page.parent.mkdir(parents=True)
    original = (
        "---\ntype: concept\ncode:\n  files:\n    - src/a.py\n"
        "code:\n  files:\n    - src/b.py\n---\n"
        "# Service\n\n## Notes\nOriginal.\n"
    )
    page.write_text(original, encoding="utf-8")

    result = server.wiki_update_page(
        "d", "concept/service", "Notes", "Changed."
    )

    assert "duplicate" in result["error"]
    assert page.read_text(encoding="utf-8") == original


def test_apply_okf_rejects_duplicate_symbol_field_and_preserves_original_bytes(
    tmp_path, monkeypatch
):
    _patch_server(monkeypatch, tmp_path)
    page = tmp_path / "d" / "concept" / "service.md"
    page.parent.mkdir(parents=True)
    original = (
        "---\ntype: concept\ncode:\n  symbols:\n"
        "    - qualified_name: pkg.Service.run\n"
        "      qualified_name: pkg.Other.run\n---\n"
        "# Service\n\n## Notes\nOriginal.\n"
    )
    page.write_text(original, encoding="utf-8")

    result = server.wiki_apply_okf("d", "concept/service", type="concept")

    assert "duplicate" in result["error"]
    assert page.read_text(encoding="utf-8") == original


def test_migrate_okf_rejects_duplicate_code_before_any_page_write(
    tmp_path, monkeypatch
):
    _patch_server(monkeypatch, tmp_path)
    page = tmp_path / "d" / "service.md"
    original = (
        "---\ntype: concept\ncode:\n  files:\n    - src/a.py\n"
        "code:\n  files:\n    - src/b.py\n---\n"
        "# Service\n\n## Notes\nOriginal.\n"
    )
    page.write_text(original, encoding="utf-8")

    result = server.wiki_migrate_okf("d")

    assert "duplicate" in result["error"]
    assert page.read_text(encoding="utf-8") == original


def test_malformed_code_is_opaque_and_fail_soft_for_ordinary_wiki_paths():
    markdown = (
        "---\ntype: concept\ndescription: Service\ncode:\n"
        "    modules:\n      - pkg.service\n---\n"
        "# Service\n\n## Notes\nOrdinary Wiki content.\n"
    )

    meta, body = fm.split(markdown)
    rendered = fm.render(meta) + body

    assert rendered == markdown
    assert chunk_markdown("concept/service.md", markdown, 512, 64)
    assert isinstance(validate_page(markdown), list)


def test_okf_sweep_preserves_authored_code_mapping(tmp_path):
    domain = tmp_path / "d"
    domain.mkdir()
    page = domain / "service.md"
    page.write_text(_authored_markdown(), encoding="utf-8")

    class Config:
        summary_max = 400

    okf.batch_sweep(Config(), str(tmp_path), "d")

    meta, _ = fm.split(page.read_text(encoding="utf-8"))
    assert meta["code"] == AUTHORED


def test_okf_sweep_preflights_duplicate_code_before_any_page_write(tmp_path):
    domain = tmp_path / "d"
    domain.mkdir()
    first = domain / "a.md"
    first_original = "# A\n\n## Overview\nWould be rewritten.\n"
    first.write_text(first_original, encoding="utf-8")
    duplicate = domain / "z.md"
    duplicate.write_text(
        "---\ncode:\n  files:\n    - src/a.py\n"
        "code:\n  files:\n    - src/b.py\n---\n# Z\n",
        encoding="utf-8",
    )

    class Config:
        summary_max = 400

    with pytest.raises(fm.FrontmatterError, match="duplicate"):
        okf.batch_sweep(Config(), str(tmp_path), "d")

    assert first.read_text(encoding="utf-8") == first_original


def test_apply_okf_without_move_preserves_authored_code_mapping(
    tmp_path, monkeypatch
):
    _patch_server(monkeypatch, tmp_path)
    page = tmp_path / "d" / "concept" / "service.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        fm.render({
            "type": "concept", "description": "Service", "code": AUTHORED,
        }) + "# Service\n\n## Notes\nAuthored.\n",
        encoding="utf-8",
    )

    result = server.wiki_apply_okf(
        "d", "concept/service", type="concept"
    )

    assert "error" not in result
    meta, _ = fm.split(page.read_text(encoding="utf-8"))
    assert meta["code"] == AUTHORED


def test_apply_okf_git_move_preserves_authored_code_mapping(
    tmp_path, monkeypatch
):
    _patch_server(monkeypatch, tmp_path)
    page = tmp_path / "d" / "concept" / "service.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        fm.render({
            "type": "concept", "description": "Service", "code": AUTHORED,
        }) + "# Service\n\n## Notes\nAuthored.\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed"], cwd=tmp_path, check=True
    )

    result = server.wiki_apply_okf(
        "d", "concept/service", type="architecture"
    )

    assert "error" not in result
    moved = tmp_path / "d" / "architecture" / "service.md"
    meta, _ = fm.split(moved.read_text(encoding="utf-8"))
    assert meta["code"] == AUTHORED
