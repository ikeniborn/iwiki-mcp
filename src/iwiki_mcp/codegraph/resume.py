"""Reconcile a pinned upload exclusively from its authenticated attempt and batch receipts."""

from __future__ import annotations

from datetime import datetime
import secrets
import time

from .canonical import canonical_sha256
from .checkpoint import CodeGraphResumeError
from .indexer import BuildControl
from .publication import PublicationSession, SnapshotHeader, quota_resource


class ResumeProgress:
    """Combine the local indexer's observed phases with the pinned upload's phases."""

    def __init__(self):
        self.id = secrets.token_hex(8)
        self.control = BuildControl()
        self.build = None
        self.state = "running"

    def observe_build(self, job):
        self.build = job.control
        return lambda completed: None

    def progress_observations(self, cursor):
        state = self.state
        entries, latest, _ = self.control.observations(0)
        build = self.build
        if build is not None:
            phases, parsed, _ = build.observations(0)
            counts = {
                key: parsed[key] for key in ("files_total", "files_parsed") if key in parsed
            }
            entries = phases + [dict(entry, **counts) for entry in entries[1:]]
            latest = parsed if latest["phase"] == "queued" else dict(latest, **counts)
        return (
            [
                {"id": self.id, "state": "running", "progress": item}
                for item in entries[cursor:]
            ],
            {"id": self.id, "state": state, "progress": latest},
        )


def require_response(reply):
    if not isinstance(reply, dict):
        raise CodeGraphResumeError("resume_failed")
    if "error" not in reply:
        return
    code = reply.get("code") or reply["error"]
    resource = quota_resource(reply)
    if resource is not None:
        raise CodeGraphResumeError("resume_failed", quota_resource=resource)
    if code == "remote_mcp_failed":
        if reply.get("status") in (401, 403):
            code = "unauthorized"
        elif reply.get("reason") in ("connect", "timeout", "http_status", "unknown"):
            code = "transport"
    errors = {
        "transport": "resume_transport_failed",
        "commit_uncertain": "resume_transport_failed",
        "incompatible_peer": "resume_incompatible",
        "resume_limits_changed": "resume_incompatible",
        "session_expired": "resume_expired",
        "busy": "resume_busy",
        "snapshot_conflict": "resume_conflict",
        "batch_conflict": "resume_conflict",
        "revision_mismatch": "resume_conflict",
        "invalid_cursor": "resume_conflict",
        "unauthorized": "resume_access_denied",
        "access_denied": "resume_access_denied",
        "scope_mismatch": "resume_target_mismatch",
        "binding_not_selected": "resume_target_mismatch",
    }
    raise CodeGraphResumeError(errors.get(code, "resume_failed"))


def transfer(pin, publisher, progress):
    record = pin.record
    if record["deadline"] <= time.time():
        raise CodeGraphResumeError("resume_expired")
    pin.verify()
    header = SnapshotHeader(**record["header"])
    if "session" not in record:
        progress.enter_phase("publish_begin")
        session = publisher.begin(
            header, attempt_key=record["key"], recovery_deadline=record["deadline"]
        )
        if not isinstance(session, PublicationSession):
            require_response(session)
            raise CodeGraphResumeError("resume_failed")
        pin.save_session(session)
        record = pin.record
    else:
        session = PublicationSession(**record["session"])

    progress.enter_phase("inspect")
    identity = {
        "session_id": session.session_id,
        "header_digest": canonical_sha256(record["header"], prefix=True),
        "graph_payload_revision": header.graph_payload_revision,
        "recovery_deadline": record["deadline"],
        "max_batch_rows": session.max_batch_rows,
        "max_batch_bytes": session.max_batch_bytes,
    }
    batches = record["batches"]
    expected = {(item["kind"], item["ordinal"]): item["hash"] for item in batches}
    verified = set()
    cursor = None
    seen_cursors = set()
    while True:
        page = publisher.inspect(session, cursor=cursor)
        require_response(page)
        if any(page.get(key) != value for key, value in identity.items()):
            raise CodeGraphResumeError("resume_conflict")
        if page.get("state") == "ready":
            pin.complete(page.get("snapshot_revision"))
            return
        try:
            lease = datetime.fromisoformat(page["lease_expires_at"].replace("Z", "+00:00"))
            if lease.tzinfo is None:
                raise ValueError("missing timezone")
        except (KeyError, ValueError, TypeError, AttributeError):
            raise CodeGraphResumeError("resume_conflict") from None
        if lease.timestamp() <= time.time():
            raise CodeGraphResumeError("resume_expired")
        receipts = page.get("receipts")
        if (
            page.get("state") != "staging"
            or not isinstance(receipts, list)
            or len(receipts) > 256
        ):
            raise CodeGraphResumeError("resume_conflict")
        for receipt in receipts:
            if (
                not isinstance(receipt, dict)
                or not isinstance(receipt.get("kind"), str)
                or type(receipt.get("ordinal")) is not int
            ):
                raise CodeGraphResumeError("resume_conflict")
            key = (receipt.get("kind"), receipt["ordinal"])
            if (
                key not in expected
                or key in verified
                or expected[key] != receipt.get("payload_hash")
            ):
                raise CodeGraphResumeError("resume_conflict")
            verified.add(key)
        cursor = page.get("next_cursor")
        if cursor is None:
            break
        if not receipts or not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            raise CodeGraphResumeError("resume_conflict")
        seen_cursors.add(cursor)

    progress.enter_phase("transfer")
    for item in batches:
        kind, ordinal = item["kind"], item["ordinal"]
        if (kind, ordinal) not in verified:
            accepted = publisher.publish_batch(session, pin.batch(item))
            require_response(accepted)
            if accepted.get("accepted") is not True:
                raise CodeGraphResumeError("resume_failed")
        # Array brackets and commas add one byte over this range of newline-terminated rows.
        progress.acknowledge_batch(kind, ordinal, item["length"] + 1)
    progress.enter_phase("finalize")
    result = publisher.finalize(session)
    require_response(result)
    if result.get("state") != "ready":
        raise CodeGraphResumeError("resume_failed")
    pin.complete(result.get("snapshot_revision"))
