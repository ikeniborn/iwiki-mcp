import json
from datetime import datetime, timedelta, timezone

import pytest

from iwiki_mcp.codegraph import application
from iwiki_mcp.codegraph.canonical import canonical_json_bytes, canonical_sha256
from iwiki_mcp.codegraph.publication import (
    PublicationSession, graph_payload_revision, header_payload,
)
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
        ("busy", "resume_busy"),
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


@pytest.mark.parametrize("resource", ["sessions", "staged_bytes"])
def test_resume_cli_reports_quota_then_completes_the_pinned_upload(project, monkeypatch, resource):
    from io import StringIO
    import json
    from iwiki_mcp import admin

    peer = Peer(None)
    monkeypatch.setattr(application, "McpSnapshotPublisher", lambda transport: peer)
    args = ["code", "publish", "--project", str(project), "--resume", "--json"]
    refuse = {"error": "publication_quota_exceeded", "resource": resource, "hint": "private-detail"}
    original_batch = peer.publish_batch
    with monkeypatch.context() as limited:
        if resource == "sessions":
            limited.setattr(peer, "begin", lambda *args, **kw: refuse)
        else:
            def capped_batch(session, batch):
                return refuse if peer.receipts else original_batch(session, batch)
            limited.setattr(peer, "publish_batch", capped_batch)
        out, err = StringIO(), StringIO()
        assert admin.run(args, environ=ENV, stdout=out, stderr=err) == 1
        failure = json.loads(out.getvalue())
        assert failure["error"] == "resume_failed"
        assert failure["code"] == "publication_quota_exceeded"
        assert failure["resource"] == resource
        assert "private-detail" not in out.getvalue() + err.getvalue()
    assert len(peer.receipts) == (1 if resource == "staged_bytes" else 0)
    monkeypatch.setattr(application, "code_runtime", lambda *args, **kw: pytest.fail("rebuilt pin"))
    out = StringIO()
    assert admin.run(args, environ=ENV, stdout=out, stderr=StringIO()) == 0
    assert json.loads(out.getvalue())["snapshot_revision"] == peer.terminal


@pytest.mark.parametrize("busy", [True, False])
def test_new_pin_refuses_busy_or_superseded_capture(project, monkeypatch, busy):
    from iwiki_mcp.codegraph.runtime import CodeGraphRuntime
    from iwiki_mcp.codegraph.store import code_graph_write_lock

    original = CodeGraphRuntime.export_snapshot
    peer = Peer(None)
    monkeypatch.setattr(application, "McpSnapshotPublisher", lambda transport: peer)

    def capture(runtime):
        if busy:
            with code_graph_write_lock(runtime.paths.lock, timeout=0):
                return original(runtime)
        header, rows = original(runtime)
        rows["repositories"][0]["revision"] = "sha256:" + "b" * 64
        return header, rows

    with monkeypatch.context() as scoped:
        scoped.setattr(CodeGraphRuntime, "export_snapshot", capture)
        with pytest.raises(application.CodeGraphResumeError,
                           match="resume_busy" if busy else "resume_conflict"):
            application.resume_project(str(project), environ=ENV)
    assert peer.calls == ["capabilities"]
    assert application.resume_project(str(project), environ=ENV).ready


def pin_files(project):
    manifest = next((project.parent / "wiki" / ".iwiki").glob("resume-*/manifest-*"))
    return manifest, manifest.with_name("rows")


def set_policy(manifest, policy):
    envelope = json.loads(manifest.read_bytes())
    record = envelope["record"]
    if policy is None:
        record.pop("signature_policy_revision", None)
    else:
        record["signature_policy_revision"] = policy
    envelope["sha256"] = canonical_sha256(record, prefix=True)
    manifest.write_bytes(canonical_json_bytes(envelope))


MARKERS = ("MiXeD_Default", "MiXeD_Alpha", "MiXeD_Beta", "MiXeD_Body")


@pytest.fixture
def previous_attempt(project, monkeypatch):
    from dataclasses import replace
    from iwiki_mcp.codegraph.runtime import CodeGraphRuntime

    service = project / "service.py"
    service.write_text(service.read_text().replace(
        "def helper():\n    return 1",
        'def helper(key: Literal["MiXeD_Alpha", "MiXeD_Beta"]="MiXeD_Default".lower()):\n'
        '    "MiXeD_Body".upper()\n'
        "    return notify()",
    ))
    original = CodeGraphRuntime.export_snapshot

    def previous_export(runtime):
        header, _ = original(runtime)
        raw = runtime._store.export_rows()
        return replace(header, graph_payload_revision=graph_payload_revision(raw)), raw

    peer = Peer("batch")
    with monkeypatch.context() as previous:
        previous.setattr(CodeGraphRuntime, "export_snapshot", previous_export)
        previous.setattr(application, "McpSnapshotPublisher", lambda transport: peer)
        with pytest.raises(application.CodeGraphResumeError, match="resume_transport_failed"):
            application.resume_project(str(project), environ=ENV)
    manifest, rows = pin_files(project)
    set_policy(manifest, None)
    assert all(marker.encode() in rows.read_bytes() for marker in MARKERS)
    assert len(peer.receipts) == 5
    return manifest, rows


@pytest.mark.parametrize("policy", [None, 2, True, "1"])
def test_incompatible_pin_refuses_locally_and_preserves_bytes(
    project, previous_attempt, monkeypatch, policy
):
    from io import StringIO
    from iwiki_mcp import admin

    manifest, rows = previous_attempt
    set_policy(manifest, policy)
    before = manifest.read_bytes(), rows.read_bytes()
    monkeypatch.setattr(
        application, "McpSnapshotPublisher", lambda *args: pytest.fail("built peer")
    )
    args = ["code", "publish", "--project", str(project), "--resume"]
    for extra in (["--json"], []):
        out, err = StringIO(), StringIO()
        assert admin.run(args + extra, environ=ENV, stdout=out, stderr=err) == 2
        if extra:
            assert json.loads(out.getvalue())["error"] == "resume_incompatible"
        else:
            assert "--forget-resume" in err.getvalue()
            assert "active" in err.getvalue()
        assert all(marker.lower() not in (out.getvalue() + err.getvalue()).lower()
                   for marker in MARKERS)
        assert ENV["IWIKI_CODE_GRAPH_MCP_TOKEN"] not in out.getvalue() + err.getvalue()
        assert (manifest.read_bytes(), rows.read_bytes()) == before


def test_previous_pin_checks_credential_and_integrity(project, previous_attempt, monkeypatch):
    manifest, _ = previous_attempt
    monkeypatch.setattr(
        application, "McpSnapshotPublisher", lambda *args: pytest.fail("built peer")
    )
    with pytest.raises(application.CodeGraphResumeError, match="resume_access_denied"):
        application.resume_project(str(project), environ={**ENV, application.TOKEN_ENV: "other"})
    envelope = json.loads(manifest.read_bytes())
    envelope["record"]["key"] = "altered"
    manifest.write_bytes(canonical_json_bytes(envelope))
    with pytest.raises(application.CodeGraphResumeError, match="resume_invalid_checkpoint"):
        application.resume_project(str(project), environ=ENV)


def test_forget_previous_pin_then_rebuild_projected_attempt(project, previous_attempt, monkeypatch):
    manifest, rows = previous_attempt
    old_key = json.loads(manifest.read_bytes())["record"]["key"]
    raw_calls = [row for line in rows.read_bytes().splitlines()
                 if (row := json.loads(line)).get("relation_type") == "CALLS"]
    for marker in ("MiXeD_Default", "MiXeD_Body"):
        assert any(marker in row["target_reference"] for row in raw_calls)
    assert application.forget_resume_project(str(project), environ=ENV)["outcome_unknown"]
    peer = Peer("batch")
    monkeypatch.setattr(application, "McpSnapshotPublisher", lambda transport: peer)
    with pytest.raises(application.CodeGraphResumeError, match="resume_transport_failed"):
        application.resume_project(str(project), environ=ENV)
    pinned_bytes = rows.read_bytes()
    assert all(marker.lower().encode() not in pinned_bytes.lower() for marker in MARKERS)
    calls = [row for line in pinned_bytes.splitlines()
             if (row := json.loads(line)).get("relation_type") == "CALLS"]
    assert any(row["target_reference"] == "notify" for row in calls)
    endpoints = ("relation_id", "source_symbol_id", "source_file_id", "source_module_id",
                 "target_symbol_id", "target_module_id")
    assert {tuple(row[k] for k in endpoints) for row in calls} == {
        tuple(row[k] for k in endpoints) for row in raw_calls
    }
    record = json.loads(manifest.read_bytes())["record"]
    assert record["key"] != old_key
    assert ("begin", record["key"]) in peer.calls
    assert record["signature_policy_revision"] == 1

    (project / "service.py").write_text("def changed(): pass\n")
    source, _ = application._resume_context(str(project), ENV)
    runtime = application.code_runtime(source, environ=ENV)
    assert runtime.index()["state"] == "ready"
    new_header, _ = runtime.export_snapshot()
    assert new_header.graph_payload_revision != record["header"]["graph_payload_revision"]
    assert rows.read_bytes() == pinned_bytes
    acknowledged = set(peer.receipts)
    previous_calls = len(peer.calls)
    outcome = application.resume_project(str(project), environ=ENV)
    assert outcome.ready
    expected = {(b["kind"], b["ordinal"]): b["hash"] for b in record["batches"]}
    assert peer.receipts == expected
    resent = [call[1] for call in peer.calls[previous_calls:] if isinstance(call, tuple)]
    assert sorted(resent) == sorted(set(expected) - acknowledged)


def test_previous_ready_receipt_returns_local_result(project, monkeypatch):
    peer = Peer(None)
    monkeypatch.setattr(application, "McpSnapshotPublisher", lambda transport: peer)
    outcome = application.resume_project(str(project), environ=ENV)
    manifest, rows = pin_files(project)
    set_policy(manifest, None)
    before = manifest.read_bytes()
    monkeypatch.setattr(
        application, "McpSnapshotPublisher", lambda *args: pytest.fail("built peer")
    )
    receipt = application.resume_project(str(project), environ=ENV)
    assert receipt.snapshot_revision == outcome.snapshot_revision
    assert receipt.index["counts"] == outcome.index["counts"]
    assert manifest.read_bytes() == before and not rows.exists()
