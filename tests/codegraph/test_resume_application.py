from datetime import datetime, timedelta, timezone

import pytest

from iwiki_mcp.codegraph import application
from iwiki_mcp.codegraph.canonical import canonical_sha256
from iwiki_mcp.codegraph.publication import PublicationSession, header_payload
from tests.codegraph.synthetic_wiki import create_sqlite_project


class Peer:
    def __init__(self, lost):
        self.lost = lost
        self.calls = []
        self.receipts = {}
        self.terminal = None
        self.session = PublicationSession(
            "id",
            (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            None,
            0,
            1,
            5000000,
        )
        self.identity = None
        self.reject = None

    def check_resume_support(self):
        self.calls.append("capabilities")
        return {}

    def begin(self, header, *, attempt_key, recovery_deadline):
        self.calls.append(("begin", attempt_key))
        self.identity = {
            "graph_payload_revision": header.graph_payload_revision,
            "session_id": "id",
            "max_batch_rows": 1,
            "max_batch_bytes": 5000000,
            "header_digest": canonical_sha256(header_payload(header), prefix=True),
            "recovery_deadline": recovery_deadline,
        }
        if self.lost == "begin":
            self.lost = None
            return {"error": "remote_mcp_failed", "reason": "timeout"}
        return self.session

    def inspect(self, session, cursor=None):
        self.calls.append("inspect")
        if self.reject:
            return self.reject
        if self.terminal:
            return {**self.identity, "state": "ready", "snapshot_revision": self.terminal}
        receipts = [
            {"kind": k, "ordinal": n, "payload_hash": h}
            for (k, n), h in sorted(self.receipts.items())
        ]
        pos = int(cursor or 0)
        return {
            **self.identity,
            "state": "staging",
            "lease_expires_at": self.session.lease_expires_at,
            "receipts": receipts[pos:pos + 2],
            "next_cursor": str(pos + 2) if len(receipts) > pos + 2 else None,
        }

    def publish_batch(self, session, batch):
        key = (batch.kind, batch.ordinal)
        self.calls.append(("batch", key))
        assert key not in self.receipts
        self.receipts[key] = batch.payload_hash
        if self.lost == "batch" and len(self.receipts) == 5:
            self.lost = None
            return {"error": "remote_mcp_failed", "reason": "timeout"}
        return {"accepted": True}

    def finalize(self, session):
        self.calls.append("finalize")
        self.terminal = "sha256:" + "a" * 64
        if self.lost == "finalize":
            self.lost = None
            return {"error": "remote_mcp_failed", "reason": "timeout"}
        return {"state": "ready", "snapshot_revision": self.terminal}


@pytest.fixture
def project(tmp_path):
    root = create_sqlite_project(tmp_path)
    config = root / ".iwiki.toml"
    config.write_text(
        config.read_text().replace('publish_mode = "sqlite"', 'publish_mode = "mcp"')
    )
    return root


ENV = {
    "IWIKI_CODE_GRAPH_MCP_URL": "https://fixture.invalid/mcp",
    "IWIKI_CODE_GRAPH_MCP_TOKEN": "token",
}


@pytest.mark.parametrize("lost", ["begin", "batch", "finalize"])
def test_resume_lost_reply_pins_once_and_replays_committed_revision(project, monkeypatch, lost):
    peer = Peer(lost)
    monkeypatch.setattr(application, "McpSnapshotPublisher", lambda transport: peer)
    with pytest.raises(application.CodeGraphResumeError, match="resume_transport_failed"):
        application.resume_project(str(project), environ=ENV)
    (project / "service.py").write_text("pass\n")
    monkeypatch.setattr(
        application, "code_runtime", lambda *a, **kw: pytest.fail("rebuilt pin")
    )
    result = application.resume_project(str(project), environ=ENV)
    assert result.ready and result.snapshot_revision == peer.terminal
    assert peer.calls.count("finalize") == 1
    begins = [call[1] for call in peer.calls if isinstance(call, tuple) and call[0] == "begin"]
    assert len(set(begins)) == 1
    assert len(begins) == (2 if lost == "begin" else 1)
    old_calls = list(peer.calls)
    peer.terminal = "sha256:" + "b" * 64
    assert (
        application.resume_project(str(project), environ=ENV).snapshot_revision
        == result.snapshot_revision
    )
    assert (
        application.forget_resume_project(str(project), environ=ENV)["outcome_unknown"] is False
    )
    assert peer.calls == old_calls


@pytest.mark.parametrize(
    "error,expected",
    [
        ("session_expired", "resume_expired"),
        ("snapshot_conflict", "resume_conflict"),
        ("unauthorized", "resume_access_denied"),
    ],
)
def test_resume_stops_without_resending_on_inspection_refusal(
    project, monkeypatch, error, expected
):
    peer = Peer("batch")
    monkeypatch.setattr(application, "McpSnapshotPublisher", lambda transport: peer)
    with pytest.raises(application.CodeGraphResumeError, match="resume_transport_failed"):
        application.resume_project(str(project), environ=ENV)
    previous = peer.receipts.copy()
    peer.reject = {"error": error}
    with pytest.raises(application.CodeGraphResumeError, match=expected):
        application.resume_project(str(project), environ=ENV)
    assert peer.receipts == previous
    assert "finalize" not in peer.calls
    assert (
        application.forget_resume_project(str(project), environ=ENV)["outcome_unknown"] is True
    )


def test_resume_conflicting_receipt_stops_before_next_transfer(project, monkeypatch):
    peer = Peer("batch")
    monkeypatch.setattr(application, "McpSnapshotPublisher", lambda transport: peer)
    with pytest.raises(application.CodeGraphResumeError, match="resume_transport_failed"):
        application.resume_project(str(project), environ=ENV)
    peer.receipts[next(iter(peer.receipts))] = "sha256:" + "e" * 64
    with pytest.raises(application.CodeGraphResumeError, match="resume_conflict"):
        application.resume_project(str(project), environ=ENV)
    assert len(peer.receipts) == 5
    assert "finalize" not in peer.calls


def test_rotated_credential_stops_before_retrying_lost_begin_but_can_forget(
    project, monkeypatch
):
    peer = Peer("begin")
    monkeypatch.setattr(application, "McpSnapshotPublisher", lambda transport: peer)
    with pytest.raises(application.CodeGraphResumeError, match="resume_transport_failed"):
        application.resume_project(str(project), environ=ENV)
    calls = list(peer.calls)
    changed = {**ENV, "IWIKI_CODE_GRAPH_MCP_TOKEN": "rotated-test-token"}
    with pytest.raises(application.CodeGraphResumeError, match="resume_access_denied"):
        application.resume_project(str(project), environ=changed)
    assert application.forget_resume_project(str(project), environ=changed)["outcome_unknown"]
    assert peer.calls == calls


def test_unsupported_peer_and_mode_do_not_build(project, monkeypatch):
    class Legacy:
        def check_resume_support(self):
            return {"error": "incompatible_peer"}

    monkeypatch.setattr(application, "McpSnapshotPublisher", lambda transport: Legacy())
    monkeypatch.setattr(
        application, "code_runtime", lambda *a, **kw: pytest.fail("built graph")
    )
    with pytest.raises(application.CodeGraphResumeError, match="resume_incompatible"):
        application.resume_project(str(project), environ=ENV)
    config = project / ".iwiki.toml"
    config.write_text(
        config.read_text().replace('publish_mode = "mcp"', 'publish_mode = "sqlite"')
    )
    with pytest.raises(application.CodeGraphResumeError, match="invalid_config"):
        application.resume_project(str(project), environ=ENV)


def test_expired_inspection_lease_sends_no_batch(project, monkeypatch):
    peer = Peer("batch")
    monkeypatch.setattr(application, "McpSnapshotPublisher", lambda transport: peer)
    with pytest.raises(application.CodeGraphResumeError, match="resume_transport_failed"):
        application.resume_project(str(project), environ=ENV)
    peer.reject = {
        **peer.identity,
        "state": "staging",
        "lease_expires_at": "2000-01-01T00:00:00Z",
        "receipts": [],
        "next_cursor": None,
    }
    with pytest.raises(application.CodeGraphResumeError, match="resume_expired"):
        application.resume_project(str(project), environ=ENV)
    assert len(peer.receipts) == 5


def test_local_disk_failure_after_remote_commit_recovers_same_attempt(project, monkeypatch):
    import errno
    import os

    peer = Peer(None)
    monkeypatch.setattr(application, "McpSnapshotPublisher", lambda transport: peer)
    real_fsync = os.fsync

    def fail_completion_sync(fd):
        if peer.terminal is not None:
            raise OSError(errno.ENOSPC, "fixture disk full")
        return real_fsync(fd)

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "fsync", fail_completion_sync)
        with pytest.raises(application.CodeGraphResumeError, match="resume_local_io_failed"):
            application.resume_project(str(project), environ=ENV)
    first_calls = list(peer.calls)
    assert peer.terminal and first_calls.count("finalize") == 1
    monkeypatch.setattr(application, "code_runtime", lambda *a, **kw: pytest.fail("rebuilt pin"))
    recovered = application.resume_project(str(project), environ=ENV)
    assert recovered.snapshot_revision == peer.terminal
    assert peer.calls[len(first_calls):] == ["capabilities", "inspect"]


def test_malformed_remote_receipt_preserves_local_checkpoint(project, monkeypatch):
    peer = Peer("batch")
    monkeypatch.setattr(application, "McpSnapshotPublisher", lambda transport: peer)
    with pytest.raises(application.CodeGraphResumeError, match="resume_transport_failed"):
        application.resume_project(str(project), environ=ENV)
    page = peer.inspect(peer.session)
    page["receipts"][0]["kind"] = []
    peer.reject = page
    with pytest.raises(application.CodeGraphResumeError, match="resume_conflict"):
        application.resume_project(str(project), environ=ENV)
    assert len(peer.receipts) == 5
