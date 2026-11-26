"""Public OKF provenance and revision guards on the Git wiki."""
from pathlib import Path


from iwiki_mcp import server
from tests.test_server_write import _seed


PAGE = ("backend", "concept/auth")
BODY = "# Auth\n\n## Overview\nA summary.\n\n## Steps\nCheck the source.\n"
AGENT = "agent:release-editor/2.3"


def read():
    return server.wiki_read_page(*PAGE)


def test_reviewed_revision_loses_trust_only_on_meaningful_edits(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    assert "page" in server.wiki_write_page(*PAGE, BODY, author=AGENT, stale_after="2000-01-01")
    first = read()
    assert first["trust_tier"] == "unverified"
    assert first["generated"]["by"] == AGENT
    assert "page" in server.wiki_verify_page(*PAGE, "human:alice",
                                             expected_page_hash=first["page_hash"])
    reviewed = read()
    assert reviewed["trust_tier"] == "human-reviewed"
    assert reviewed["verified"][0]["by"] == "human:alice"
    server.wiki_update_page(*PAGE, "Steps", "Check the **source**.", presentation_only=True)
    assert read()["generated"] == reviewed["generated"]
    assert "page" in server.wiki_update_page(*PAGE, "Overview", "Another fact.", author=AGENT)
    assert server.wiki_verify_page(*PAGE, AGENT, expected_page_hash=reviewed["page_hash"])[
        "error"] == "conflict"
    changed = read()
    assert changed["trust_tier"] == "unverified"
    assert changed["verified"] == reviewed["verified"]
    assert changed["stale_after"] == "2000-01-01"
    server.wiki_update_page(*PAGE, stale_after="")
    assert "stale_after" not in read()
    assert read()["generated"] == changed["generated"]


def test_missing_or_invalid_author_refuses_write(tmp_path, monkeypatch):
    base, _ = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_editing_agent", lambda: None)
    for author, error in ((None, "author_required"), ("human:", "invalid_author"),
                          ("unknown:legacy-client", "invalid_author"),
                          ("agent:mcp/0.1.0", "invalid_author")):
        assert server.wiki_write_page(*PAGE, BODY, author=author)["error"] == error
    assert not (Path(base) / "backend" / "concept" / "auth.md").exists()


def test_verification_preserves_crlf_and_imported_event(tmp_path, monkeypatch):
    base, _ = _seed(tmp_path, monkeypatch)
    path = Path(base) / "backend/concept/auth.md"
    path.parent.mkdir()
    path.write_bytes(('---\ntype: concept\nverified: {by: editor/1, at: 2025-01-01}\n---\n'
                      + BODY).replace("\n", "\r\n").encode())
    before = read()
    assert before["trust_tier"] == "machine-confirmed"
    assert "page" in server.wiki_verify_page(*PAGE, AGENT, expected_page_hash=before["page_hash"])
    assert path.read_bytes().endswith(BODY.replace("\n", "\r\n").encode())
    assert [event["by"] for event in read()["verified"]] == ["editor/1", AGENT]


def test_create_retains_authored_deadline_and_preserves_external_metadata(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    content = '---\nstale_after: 2027-01-01\nx-owner: {team: docs}\n---\n' + BODY
    assert "page" in server.wiki_write_page(*PAGE, content, author="human:alice")
    result = read()
    assert result["stale_after"] == "2027-01-01"
    from iwiki_mcp.engine.frontmatter import split
    assert split(result["markdown"])[0]["x-owner"] == {"team": "docs"}


def test_submitting_current_section_text_or_order_preserves_review(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(*PAGE, BODY, author=AGENT)
    server.wiki_verify_page(*PAGE, "human:alice", expected_page_hash=read()["page_hash"])
    reviewed = read()
    section = server.wiki_read_page(*PAGE, heading="Steps")
    assert "page" in server.wiki_update_page(*PAGE, "Steps", section["body"])
    assert "page" in server.wiki_move_section(*PAGE, "Steps", after_heading="Overview")
    assert read()["generated"] == reviewed["generated"]
    assert read()["trust_tier"] == "human-reviewed"


def test_verify_preserves_legacy_bare_inline_hash_in_managed_fields(tmp_path, monkeypatch):
    from iwiki_mcp.engine import frontmatter

    base, _ = _seed(tmp_path, monkeypatch)
    path = Path(base) / "backend/concept/auth.md"
    path.parent.mkdir()
    path.write_text(
        "---\ntype: concept\ntitle: Incident #42\ndescription: Tracks #42 for June\n"
        "resource: 'source.md' # YAML comment after a quoted scalar\n---\n" + BODY,
        encoding="utf-8",
    )
    result = server.wiki_verify_page(*PAGE, AGENT, expected_page_hash=read()["page_hash"])
    assert "page" in result, result
    meta, body = frontmatter.split(path.read_text(encoding="utf-8"))
    assert meta["title"] == "Incident #42"
    assert meta["description"] == "Tracks #42 for June"
    assert meta["resource"] == "source.md"
    assert body == BODY


def test_heading_read_keeps_malformed_trust_fields_fail_soft(tmp_path, monkeypatch):
    base, _ = _seed(tmp_path, monkeypatch)
    path = Path(base) / "backend/concept/auth.md"
    path.parent.mkdir()
    for field in ("verified: [unclosed", "x-extension: !!unknown {}"):
        path.write_text("---\ntype: concept\n" + field + "\n---\n" + BODY, encoding="utf-8")
        result = server.wiki_read_page(*PAGE, heading="Steps")
        assert result["body"] == "Check the source.", result
        assert result["trust_tier"] == "unverified"
    path.write_text(
        "---\nverified: [unclosed\ncode:\n    invalid: selector\n---\n" + BODY,
        encoding="utf-8",
    )
    assert "invalid nested code" in server.wiki_read_page(*PAGE, heading="Steps")["error"]


def test_verify_rolls_back_on_index_failure(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(*PAGE, BODY, author=AGENT)
    before = read()

    def unavailable(*args, **kwargs):
        raise OSError("index unavailable")

    monkeypatch.setattr(server.indexer, "index_domain", unavailable)
    result = server.wiki_verify_page(*PAGE, "human:alice", expected_page_hash=before["page_hash"])
    assert "error" in result
    assert read() == before


def test_local_git_commit_failure_preserves_review_history(tmp_path, monkeypatch):
    import subprocess
    from tests.test_server_write import _init_git_base

    base, _ = _seed(tmp_path, monkeypatch)
    _init_git_base(Path(base))
    assert server.wiki_write_page(*PAGE, BODY, author=AGENT)["committed"]
    path = Path(base) / "backend/concept/auth.md"
    path.write_bytes(path.read_bytes() + b"staged note\n")
    subprocess.run(["git", "-C", base, "add", "backend/concept/auth.md"], check=True)
    staged = subprocess.check_output(["git", "-C", base, "show", ":backend/concept/auth.md"])
    path.write_bytes(staged + b"unstaged note\n")
    before = read()
    hook = Path(base) / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    result = server.wiki_verify_page(*PAGE, AGENT, expected_page_hash=before["page_hash"])
    assert result.get("rolled_back"), result
    assert read() == before
    after = subprocess.check_output(["git", "-C", base, "show", ":backend/concept/auth.md"])
    assert after == staged
