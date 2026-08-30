"""One publication contract asserted identically over all three routes."""
from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from iwiki_mcp.codegraph.publication import (
    ADAPTER_ERROR_CODES,
    PUBLICATION_ERROR_CODES,
    READINESS_ERROR_CODES,
    header_payload,
)
from tests.codegraph.publication_contract_support import (
    PublicationContractHarness,
    sqlite_route,
)


def _request(client, token, payload, *, session_id=None):
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    if session_id is not None:
        headers["Mcp-Session-Id"] = session_id
    return client.post("/mcp", headers=headers, json=payload)


def _open_session(client, token):
    initialized = _request(
        client,
        token,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "contract", "version": "1"},
            },
        },
    )
    assert initialized.status_code == 200
    session_id = initialized.headers["mcp-session-id"]
    assert _request(
        client,
        token,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        session_id=session_id,
    ).status_code == 202
    return session_id


class _McpRoute:
    """Call the hosted publication and read tools over real JSON-RPC."""

    def __init__(self, client, token, session_id):
        self._client = client
        self._token = token
        self._session_id = session_id

    def __repr__(self):
        return "<redacted hosted MCP contract route>"

    def call(self, name, arguments):
        response = _request(
            self._client,
            self._token,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            session_id=self._session_id,
        )
        assert response.status_code == 200, response.text
        return json.loads(response.json()["result"]["content"][0]["text"])

    def begin(self, header):
        return self.call(
            "wiki_code_publish_begin", {"header": header_payload(header)}
        )

    def publish_batch(self, session, batch):
        if "session_id" not in session:
            return dict(session)
        return self.call(
            "wiki_code_publish_batch",
            {
                "session_id": session["session_id"],
                "kind": batch.kind,
                "ordinal": batch.ordinal,
                "rows": json.loads(bytes(batch.payload).decode("utf-8")),
                "payload_hash": batch.payload_hash,
            },
        )

    def finalize(self, session):
        return self.call(
            "wiki_code_publish_finalize", {"session_id": session["session_id"]}
        )

    def abort(self, session):
        return self.call(
            "wiki_code_publish_abort", {"session_id": session["session_id"]}
        )

    def status(self):
        return self.call("wiki_code_status", {})

    def search(self, query="needle"):
        return self.call("wiki_code_search", {"query": query})


def _postgres_harness(graph):
    return PublicationContractHarness(
        route="postgres",
        header=graph.header,
        rows=graph.rows,
        publisher=graph.store,
        reader=graph.reader(),
        supports_commit_uncertain=False,
        replacement_factory=lambda: graph.reopen_with_new_ephemeral_owner().store,
        owner_factory=lambda owner: graph.with_owner(owner).store,
        raw_rows=graph.active_rows,
        finalize_lock=graph.hold_domain_advisory_lock,
    )


@pytest.fixture
def publication_adapter(request, tmp_path):
    route = request.param
    if route == "sqlite":
        yield sqlite_route(tmp_path)
        return
    if route == "postgres":
        graph = request.getfixturevalue("pg_ranked_graph")
        yield _postgres_harness(graph)
        return
    if route != "mcp":
        raise AssertionError(f"unknown publication route: {route}")
    hosted = request.getfixturevalue("hosted_ranked_runtime")
    with TestClient(
        hosted.runtime.app, base_url="http://127.0.0.1:8765"
    ) as client:
        session_id = _open_session(client, hosted.token)
        adapter = _McpRoute(client, hosted.token, session_id)
        yield PublicationContractHarness(
            route="mcp",
            header=hosted.graph.header,
            rows=hosted.graph.rows,
            publisher=adapter,
            reader=adapter,
            supports_commit_uncertain=False,
            raw_rows=hosted.active_rows,
            session_ref=lambda session_id: {"session_id": session_id},
        )


@pytest.fixture
def sqlite_publication_adapter(tmp_path):
    return sqlite_route(tmp_path)


ROUTES = ["sqlite", "postgres", "mcp"]


@pytest.mark.parametrize("publication_adapter", ROUTES, indirect=True)
def test_reader_never_observes_partial_snapshot(publication_adapter):
    old_revision = publication_adapter.publish_complete("old")
    session = publication_adapter.begin("new")
    publication_adapter.publish_half(session)

    assert publication_adapter.status()["snapshot_revision"] == old_revision

    new_revision = publication_adapter.finish(session)
    assert publication_adapter.observed_revisions() <= {
        old_revision, new_revision
    }
    assert publication_adapter.status()["snapshot_revision"] == new_revision


@pytest.mark.parametrize("publication_adapter", ROUTES, indirect=True)
def test_replayed_batches_are_idempotent_and_conflicts_are_rejected(
    publication_adapter
):
    session = publication_adapter.begin("replay")
    batch = publication_adapter.batches()[0]

    first = publication_adapter.publisher.publish_batch(session, batch)
    replayed = publication_adapter.publisher.publish_batch(session, batch)
    assert first["accepted"] is True
    assert replayed["accepted"] is True
    conflict = publication_adapter.publisher.publish_batch(
        session, publication_adapter.divergent(batch)
    )
    assert conflict["error"] == "batch_conflict"

    mismatched = publication_adapter.publisher.publish_batch(
        session, publication_adapter.mismatched_hash(batch)
    )
    assert mismatched["error"] in {"invalid_batch", "batch_conflict"}


@pytest.mark.parametrize("publication_adapter", ROUTES, indirect=True)
def test_incomplete_snapshots_never_activate(publication_adapter):
    session = publication_adapter.begin("incomplete")
    publication_adapter.publish_one(session)

    result = publication_adapter.finalize(session)

    assert result["error"] in {"snapshot_incomplete", "invalid_batch"}
    assert publication_adapter.status().get("snapshot_revision") is None


@pytest.mark.parametrize("publication_adapter", ROUTES, indirect=True)
def test_aborted_sessions_leave_no_active_snapshot(publication_adapter):
    session = publication_adapter.begin("aborted")
    publication_adapter.publish_half(session)

    assert publication_adapter.abort(session)["state"] == "aborted"
    assert publication_adapter.status().get("snapshot_revision") is None


@pytest.mark.parametrize("publication_adapter", ["sqlite", "postgres"], indirect=True)
def test_wrong_or_replacement_owner_never_mutates(publication_adapter):
    session = publication_adapter.begin("owner-a")
    replacement = publication_adapter.replacement_process()

    assert replacement.publish_one(session)["error"] == "unauthorized"
    assert replacement.abort(session)["error"] == "unauthorized"
    assert publication_adapter.finish(session)


@pytest.mark.parametrize("publication_adapter", ROUTES, indirect=True)
def test_error_codes_stay_in_their_routes(publication_adapter):
    session = publication_adapter.begin("codes")
    publication_adapter.publisher.publish_batch(
        session, publication_adapter.divergent(publication_adapter.batches()[0])
    )
    publication_adapter.finalize(session)
    publication_adapter.status()

    observed = publication_adapter.observable_failure_codes()
    known = (
        set(PUBLICATION_ERROR_CODES)
        | set(ADAPTER_ERROR_CODES)
        | set(READINESS_ERROR_CODES)
    )
    assert observed
    assert observed <= known
    assert set(PUBLICATION_ERROR_CODES).isdisjoint(ADAPTER_ERROR_CODES)
    assert set(PUBLICATION_ERROR_CODES).isdisjoint(READINESS_ERROR_CODES)
    assert set(ADAPTER_ERROR_CODES).isdisjoint(READINESS_ERROR_CODES)


@pytest.mark.parametrize("publication_adapter", ["postgres", "mcp"], indirect=True)
def test_commit_uncertain_is_not_emitted_by_distributed_routes(
    publication_adapter
):
    publication_adapter.publish_complete("first")
    session = publication_adapter.begin("second")
    publication_adapter.publish_one(session)
    publication_adapter.finalize(session)

    assert publication_adapter.supports_commit_uncertain is False
    assert "commit_uncertain" not in publication_adapter.observable_failure_codes()


@pytest.mark.parametrize("publication_adapter", ROUTES, indirect=True)
def test_published_rows_carry_no_source_or_absolute_path(publication_adapter):
    publication_adapter.publish_complete("scan")

    payload = json.dumps(publication_adapter.persisted_rows())
    assert "/home/" not in payload
    assert '"source"' not in payload
    assert "def " not in payload


@pytest.mark.parametrize("publication_adapter", ROUTES, indirect=True)
def test_specification_relations_never_enter_structural_graph_rows(
    publication_adapter,
):
    publication_adapter.publish_complete("structural-only")

    rows = publication_adapter.persisted_rows()

    assert {row["relation_type"] for row in rows["relations"]} <= {
        "DECLARES", "IMPORTS", "CALLS", "INHERITS"
    }
    assert "implements" not in json.dumps(rows).casefold()
    assert "verifies" not in json.dumps(rows).casefold()


@pytest.mark.parametrize("publication_adapter", ROUTES, indirect=True)
def test_forged_payload_revision_is_recomputed_and_rejected(
    publication_adapter
):
    from dataclasses import replace

    forged = replace(
        publication_adapter.header, graph_payload_revision="sha256:" + "0" * 64
    )
    session = publication_adapter.publisher.begin(forged)
    if isinstance(session, dict) and "error" in session:
        assert session["error"] in PUBLICATION_ERROR_CODES
        return
    publication_adapter.publish_complete_batches(session)

    assert publication_adapter.finalize(session)["error"] == "revision_mismatch"
    assert publication_adapter.status().get("snapshot_revision") is None


@pytest.mark.parametrize("publication_adapter", ROUTES, indirect=True)
def test_failures_never_leak_secrets_paths_or_source(publication_adapter):
    session = publication_adapter.begin("redaction")
    rejected = publication_adapter.publisher.publish_batch(
        session, publication_adapter.mismatched_hash(
            publication_adapter.batches()[0]
        )
    )
    finalize = publication_adapter.finalize(session)

    payload = json.dumps([rejected, finalize, repr(publication_adapter)])
    assert "/home/" not in payload
    assert "password" not in payload.lower()
    assert "Bearer" not in payload
    assert "def " not in payload


@pytest.mark.parametrize("publication_adapter", ROUTES, indirect=True)
def test_sql_shaped_identifiers_are_data_not_syntax(publication_adapter):
    injected = "'; DROP TABLE iwiki.code_graph_files; --"

    rejected = publication_adapter.publisher.finalize(
        publication_adapter.reference(injected)
    )

    assert rejected["error"] in PUBLICATION_ERROR_CODES
    assert publication_adapter.publish_complete("after-injection")


def test_expired_lease_rejects_every_mutation(pg_ranked_graph):
    harness = _postgres_harness(pg_ranked_graph)
    session = harness.begin("expiring")
    pg_ranked_graph.advance_clock(pg_ranked_graph.session_ttl_seconds + 1)

    assert harness.publish_one(session)["error"] == "session_expired"
    assert harness.finalize(session)["error"] == "session_expired"


def test_contended_activation_is_retryable_busy(pg_ranked_graph):
    harness = _postgres_harness(pg_ranked_graph)
    session = harness.begin("contended")
    harness.publish_complete_batches(session)

    with harness.hold_finalize_lock():
        busy = harness.finalize(session)

    assert busy == {
        "error": "busy",
        "hint": "another publication holds this domain",
        "retryable": True,
    }
    assert harness.finalize(session)["state"] == "ready"


def test_another_domain_never_sees_this_snapshot(pg_ranked_graph):
    harness = _postgres_harness(pg_ranked_graph)
    harness.publish_complete("isolated")
    other = pg_ranked_graph.for_domain("private")

    assert other.reader().status()["state"] == "missing"
    assert other.reader().search(
        pg_ranked_graph.search_request
    )["results"] == []


def test_commit_uncertain_allows_only_sqlite_finalize_reconciliation(
    sqlite_publication_adapter,
):
    from tests.codegraph.publication_contract_support import (
        fail_next_directory_sync,
        restore_directory_sync,
    )

    session = sqlite_publication_adapter.begin("uncertain")
    sqlite_publication_adapter.publish_complete_batches(session)
    fail_next_directory_sync(sqlite_publication_adapter)

    first = sqlite_publication_adapter.finalize(session)
    assert first["error"] == "commit_uncertain"
    assert sqlite_publication_adapter.status()["snapshot_revision"] == (
        first["snapshot_revision"]
    )

    for rejected in (
        sqlite_publication_adapter.publish_one(session),
        sqlite_publication_adapter.abort(session),
    ):
        assert rejected.get("accepted") is not True
        assert rejected["error"] in PUBLICATION_ERROR_CODES
        assert rejected["error"] != "commit_uncertain"

    restore_directory_sync(sqlite_publication_adapter)
    assert sqlite_publication_adapter.finalize(session)["state"] == "ready"


@pytest.mark.parametrize("publication_adapter", ROUTES, indirect=True)
def test_out_of_order_and_miscounted_batches_never_activate(
    publication_adapter
):
    batches = publication_adapter.batches()
    if len(batches) < 2:
        pytest.skip("route fixture produced a single batch")
    session = publication_adapter.begin("disordered")

    publication_adapter.publisher.publish_batch(session, batches[-1])
    miscounted = publication_adapter.publisher.publish_batch(
        session, publication_adapter.miscounted(batches[0])
    )
    result = publication_adapter.finalize(session)

    assert miscounted.get("accepted") is not True or result.get("error")
    assert result["error"] in {
        "invalid_batch", "snapshot_incomplete", "revision_mismatch"
    }
    assert publication_adapter.status().get("snapshot_revision") is None


def test_concurrent_reader_observes_only_complete_revisions(pg_ranked_graph):
    import threading

    harness = _postgres_harness(pg_ranked_graph)
    first = harness.publish_complete("first")
    reader = pg_ranked_graph.reader()
    seen = []
    stop = threading.Event()

    def poll():
        while not stop.is_set():
            seen.append(reader.status().get("snapshot_revision"))

    watcher = threading.Thread(target=poll, daemon=True)
    watcher.start()
    try:
        pg_ranked_graph.write_markdown_page(
            "guide", "# Guide\n\n## Body\nmore\n"
        )
        second = harness.publish_complete("second")
    finally:
        stop.set()
        watcher.join(timeout=10)

    assert seen
    assert set(seen) <= {first, second}
    assert reader.status()["snapshot_revision"] == second
