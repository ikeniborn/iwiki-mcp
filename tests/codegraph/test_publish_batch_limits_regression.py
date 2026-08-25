"""Regression: a client's local max_batch_rows must not leak into publish_mode='mcp'
batch sizing when it exceeds the hosted server's own limit — reproduces the exact
aioperator publish failure this plan fixes."""
from __future__ import annotations

from iwiki_mcp.codegraph import application
from iwiki_mcp.codegraph.config import CodeGraphConfig
from iwiki_mcp.codegraph.publication import canonical_batch
from iwiki_mcp.postgres.config import HostedCodeGraphConfig
from iwiki_mcp.server import _HostedPublication


class _FakeHostedStore:
    def __init__(self, session):
        self.domain = "docs"
        self._session = session

    def begin(self, header):
        return self._session

    def publish_batch(self, session, batch):
        return {"accepted": True}


def test_client_local_max_batch_rows_above_server_default_no_longer_rejected():
    from iwiki_mcp.codegraph.publication import PublicationSession

    # The hosted server's own (unconfigured-default-shaped) limit — matches
    # HostedCodeGraphConfig's real default of 1000, reproducing the aioperator case
    # where the remote server.toml had no [code_graph] section at all.
    settings = HostedCodeGraphConfig()
    assert settings.max_batch_rows == 1000

    session = PublicationSession(
        session_id="s1",
        lease_expires_at="2026-08-19T00:00:00Z",
        base_snapshot_revision=None,
        base_markdown_token=0,
        max_batch_rows=settings.max_batch_rows,
        max_batch_bytes=settings.max_batch_bytes,
    )
    # The client project's local .iwiki.toml — matches aioperator's real config,
    # larger than the server's default.
    config = CodeGraphConfig(max_batch_rows=5000, max_batch_bytes=1_000_000)

    max_rows, max_bytes = application.effective_batch_bounds(session, config)

    # Before this plan: this would be 5000 (config), producing a single oversized
    # batch of e.g. 4001 symbol rows that the server then rejects with invalid_batch.
    assert max_rows == 1000
    # A batch built with this bound can never exceed the server's real limit.
    assert max_rows <= settings.max_batch_rows

    store = _FakeHostedStore(session)
    publication = _HostedPublication(store, settings)
    oversized_rows = [{"symbol_id": f"sym{i}"} for i in range(4001)]

    # Confirm the OLD failure mode is real and reproducible against these exact
    # numbers (this assertion documents the bug this plan fixes, it is not itself
    # the fix under test):
    rejected = publication.publish_from_mapping(
        "s1", "symbols", 0, oversized_rows, "sha256:" + "0" * 64
    )
    assert rejected["error"] == "invalid_batch"
    assert rejected["limit"] == 1000
    assert rejected["received"] == 4001

    # But a client using the FIXED effective_batch_bounds never builds a batch
    # this large in the first place — chunking 4001 rows at max_rows=1000 yields
    # batches of size <= 1000, all of which pass:
    chunk = oversized_rows[:max_rows]
    assert len(chunk) <= settings.max_batch_rows
    # Use the real payload hash so the hash-verification branch passes and the
    # batch is genuinely accepted, not incidentally rejected on a hash mismatch.
    payload_hash = canonical_batch("symbols", 0, chunk).payload_hash
    accepted = publication.publish_from_mapping(
        "s1", "symbols", 0, chunk, payload_hash
    )
    assert "error" not in accepted
