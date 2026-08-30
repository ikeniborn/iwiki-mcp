from __future__ import annotations

from hashlib import sha256
import subprocess

import pytest

from iwiki_mcp import indexer, server
from iwiki_mcp.specification_store import GitSpecificationStore
from iwiki_mcp.specifications import PageSnapshot, assemble_projection


SPECIFICATION = '''---
type: specification
---
# Contract

## Preserve ordinary Wiki behavior

```iwiki-gwt
id = "preserve-ordinary-wiki"
title = "Preserve ordinary Wiki behavior"
given = []
when = { role = "command", name = "MaintainWiki" }
then = [{ role = "event", name = "WikiMaintained" }]
code = [
  { relation = "implements", symbol = "wiki.maintain" },
  { relation = "verifies", file = "tests/test_wiki.py" }
]
```
'''


def _git(path, *args):
    subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    )


def _seed(tmp_path, monkeypatch, mode):
    wiki = tmp_path / "wiki"
    domain = wiki / "docs"
    specification_dir = domain / "specification"
    specification_dir.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".iwiki.toml").write_text(
        'read = ["docs"]\nwrite = ["docs"]\nprimary = "docs"\n'
        f'[specifications]\nmode = "{mode}"\n',
        encoding="utf-8",
    )
    specification_path = specification_dir / "contract.md"
    specification_path.write_text(SPECIFICATION, encoding="utf-8")
    if mode != "disabled":
        revision = "sha256:" + sha256(SPECIFICATION.encode("utf-8")).hexdigest()
        projection = assemble_projection(
            "docs",
            (PageSnapshot("specification/contract", SPECIFICATION, revision),),
        )
        GitSpecificationStore(str(wiki), mode).replace_projection(projection)
    monkeypatch.setenv("IWIKI_BASE_DIR", str(wiki))
    monkeypatch.setenv("IWIKI_PROJECT_DIR", str(project))
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://example.invalid/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "fixture")
    monkeypatch.setenv("IWIKI_EMBED_DIMENSIONS", "2")
    monkeypatch.setattr(
        indexer,
        "embed_texts",
        lambda _cfg, texts: [[1.0, 0.0] for _text in texts],
    )
    _git(wiki, "init", "-q")
    _git(wiki, "config", "user.email", "test@example.invalid")
    _git(wiki, "config", "user.name", "test")
    _git(wiki, "add", "-A")
    _git(wiki, "commit", "-q", "-m", "seed")
    return domain


def _install_fault(monkeypatch, fault):
    if fault == "parser_exception":
        monkeypatch.setattr(
            server,
            "_assemble_specification_projection",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("private parser failure")
            ),
        )
        return
    if fault == "projection_exception":
        monkeypatch.setattr(
            server,
            "_git_specification_store_factory",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("private projection failure")
            ),
        )
        return

    if fault == "unreachable":
        class Resolver:
            def status(self):
                raise RuntimeError("private graph endpoint")

            def specification_snapshot(self):
                raise RuntimeError("private graph endpoint")

        resolver = Resolver()
    else:
        resolver = server._specifications.UnavailableSpecificationGraphResolver(
            fault
        )
    monkeypatch.setattr(
        server,
        "_specification_graph_resolver",
        lambda *_args: resolver,
    )


@pytest.mark.parametrize("mode", ["disabled", "optional", "strict"])
@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "disabled",
        "stale_graph",
        "failed",
        "unreachable",
        "parser_exception",
        "projection_exception",
    ],
)
def test_ordinary_wiki_paths_ignore_specification_and_graph_failures(
    tmp_path, monkeypatch, mode, fault,
):
    domain = _seed(tmp_path, monkeypatch, mode)
    _install_fault(monkeypatch, fault)

    written = server.wiki_write_page(
        "docs",
        "ordinary",
        "# Ordinary\n\n## Body\nalpha\n",
        type="concept",
    )
    updated = server.wiki_update_page(
        "docs", "concept/ordinary", "Body", "beta"
    )
    inserted = server.wiki_insert_section(
        "docs", "concept/ordinary", "Extra", "temporary"
    )
    removed_section = server.wiki_delete_section(
        "docs", "concept/ordinary", "Extra"
    )
    indexed = server.wiki_index("docs")
    read = server.wiki_read_page("docs", "concept/ordinary", heading="Body")
    searched = server.wiki_search(
        "beta", domains=["docs"], mode="lexical"
    )
    linted = server.wiki_lint("docs")["reports"]["docs"]
    deleted = server.wiki_delete_page("docs", "concept/ordinary")

    ordinary_results = (
        written, updated, inserted, removed_section, indexed, read, deleted,
    )
    errors = [result for result in ordinary_results if "error" in result]
    assert errors == []
    assert written["page"] == "docs/concept/ordinary.md"
    assert updated["heading"] == "Body"
    assert inserted["heading"] == "Extra"
    assert removed_section["heading"] == "Extra"
    assert indexed["domain"] == "docs"
    assert read["body"] == "beta"
    assert any(item["file"] == "concept/ordinary.md" for item in searched["results"])
    assert linted["wiki_present"] is True
    assert not any(
        finding["severity"] == "block"
        for finding in linted["specifications"]["findings"]
    )
    assert deleted["deleted"] == "docs/concept/ordinary.md"
    assert not (domain / "concept" / "ordinary.md").exists()
