from iwiki_mcp.resources import AUTHORING_RULES
from pathlib import Path


def test_authoring_rules_mention_frontmatter_and_types():
    assert "frontmatter" in AUTHORING_RULES.lower()
    for t in ("architecture", "api", "guide", "reference", "runbook", "concept"):
        assert t in AUTHORING_RULES


def test_authoring_rules_keep_generated_artifacts_out_of_graph_links():
    assert "never link to `index.md` or `log.md`" in AUTHORING_RULES.casefold()
    assert "SQLite graph cache" in AUTHORING_RULES


def test_public_docs_describe_cross_domain_transaction_contract():
    root = Path(__file__).parents[1]
    for relative in ("README.md", "docs/README.ru.md"):
        text = (root / relative).read_text(encoding="utf-8")
        assert "write_scope" in text
        assert "new_heading" in text
        assert ".iwiki/transactions/<id>" in text
        assert "Iwiki-Transaction" in text
        assert "graph" in text and "dirty" in text


def test_project_guidance_uses_current_document_and_store_paths():
    root = Path(__file__).parents[1]
    text = (root / "CLAUDE.md").read_text(encoding="utf-8")
    assert "docs/architecture.md" in text
    assert "docs/wiki/" not in text
    assert ".iwiki/index.jsonl" not in text
