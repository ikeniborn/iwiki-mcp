"""Check deadline lint against the Git backend."""
from tests.freshness_workflow import check_deadline_reports, deadline_pages


def test_git_lint_checks_review_deadlines_without_edits(tmp_path, monkeypatch):
    base = tmp_path / "wiki"
    for domain in ("docs", "other", "hidden"):
        (base / domain).mkdir(parents=True)
    pages = deadline_pages()
    for slug, markdown in pages.items():
        (base / "docs" / (slug + ".md")).write_text(markdown, encoding="utf-8")
    (base / "hidden" / "secret.md").write_text(pages["past"], encoding="utf-8")
    (tmp_path / ".iwiki.toml").write_text(
        'read = ["docs", "other"]\nwrite = ["docs"]\nprimary = "docs"\n', encoding="utf-8"
    )
    monkeypatch.setenv("IWIKI_BASE_DIR", str(base))
    monkeypatch.setenv("IWIKI_PROJECT_DIR", str(tmp_path))

    check_deadline_reports(monkeypatch)

    for slug, markdown in pages.items():
        assert (base / "docs" / (slug + ".md")).read_bytes() == markdown.encode("utf-8")
