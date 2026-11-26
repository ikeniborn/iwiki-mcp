"""Public HTTP authoring, verification and deadline workflow on actual PostgreSQL."""
from starlette.testclient import TestClient

from iwiki_mcp import server
from tests.postgres.test_http import _initialize, _tool_call, _tool_result


def test_confirm_observed_revision_and_keep_editing_clients_separate(hosted_runtime, monkeypatch):
    original = server._postgres_store_for_binding

    def fixture_store(binding):
        return original(binding).with_embedder(lambda cfg, texts: [[1., 0., 0.] for _ in texts])

    monkeypatch.setattr(server, "_postgres_store_for_binding", fixture_store)
    token = hosted_runtime.token
    with TestClient(hosted_runtime.runtime.app, base_url="http://127.0.0.1:8765") as client:
        init = _initialize(client, token, client_name="agent:release-editor")
        named = init.headers["mcp-session-id"]
        generic = _initialize(client, token, client_name="mcp").headers["mcp-session-id"]

        def call(name, args, session=named):
            return _tool_result(_tool_call(client, token, name, args, session_id=session))

        for session in (generic, named):
            assert call("wiki_bind", {"read": ["docs"], "write": ["docs"], "primary": "docs"},
                        session)["binding_source"] == "session"
        page = {"domain": "docs", "slug": "concept/review"}
        body = {"markdown": "# Review\n\n## Body\nOriginal fact.\n", "type": "concept"}
        assert call("wiki_write_page", {**page, **body}, generic)["error"] == "author_required"
        created = call("wiki_write_page", {**page, **body, "stale_after": "1999-01-01"})
        assert created["revision"] == 1
        current = call("wiki_read_page", page)
        assert current["generated"]["by"] == "agent:release-editor/1"
        assert current["trust_tier"] == "unverified"
        assert call("wiki_verify_page", {**page, "verifier": "human:alice",
                                         "expected_revision": 1})["revision"] == 2
        reviewed = call("wiki_read_page", page)
        assert reviewed["trust_tier"] == "human-reviewed"
        assert reviewed["generated"] == current["generated"]
        assert call("wiki_verify_page", {**page, "verifier": "agent:release-editor/1",
                                         "expected_revision": 1})["error"] == "conflict"
        assert call("wiki_read_page", page) == reviewed
        inserted = call("wiki_insert_section", {
            **page, "heading": "Notes", "body": "New fact.", "expected_revision": 2,
            "after_heading": "Body",
        })
        assert inserted["revision"] == 3
        changed = call("wiki_read_page", page)
        assert changed["verified"] == reviewed["verified"]
        assert changed["trust_tier"] == "unverified"
        findings = call("wiki_lint", {"domain": "docs"})["reports"]["docs"]["freshness"]
        assert {"page": "concept/review.md", "type": "review_due"} in findings
        assert call("wiki_read_page", page) == changed
        assert call("wiki_verify_page", {**page, "verifier": "process:check",
                                         "expected_revision": 3})["revision"] == 4
        assert call("wiki_read_page", page)["trust_tier"] == "machine-confirmed"
