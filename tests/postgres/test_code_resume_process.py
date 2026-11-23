"""Interrupt the CLI immediately after a real hosted transaction and restart it."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

import pytest
import uvicorn

from iwiki_mcp import server
from iwiki_mcp.codegraph.mcp_adapter import McpCodeGraphReader, RemoteMcpTransport
from iwiki_mcp.postgres.codegraph import PostgresCodeGraphStore
from iwiki_mcp.postgres.config import HostedCodeGraphConfig
from tests.codegraph.synthetic_wiki import create_sqlite_project
from tests.postgres.test_code_publish_cli_mcp import _configure_mcp_project


pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def live_target(hosted_runtime, monkeypatch):
    monkeypatch.setattr(server, "_HOSTED_CODE_GRAPH", HostedCodeGraphConfig(max_batch_rows=1))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        config = uvicorn.Config(hosted_runtime.runtime.app, log_level="error", access_log=False)
        service = uvicorn.Server(config)
        thread = threading.Thread(target=service.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not service.started:
                assert thread.is_alive() and time.monotonic() < deadline
                time.sleep(0.02)
            yield {
                "IWIKI_CODE_GRAPH_MCP_URL": f"http://127.0.0.1:{listener.getsockname()[1]}/mcp",
                "IWIKI_CODE_GRAPH_MCP_TOKEN": hosted_runtime.token,
            }
        finally:
            service.should_exit = True
            thread.join(10)


_CRASH_DRIVER = """
import os
from iwiki_mcp import admin
from iwiki_mcp.codegraph.mcp_adapter import McpSnapshotPublisher
point = os.environ['TEST_STOP_POINT']
owner = admin if point == '_write_code_json' else McpSnapshotPublisher
original = getattr(owner, point)
calls = 0
def interrupt(*args, **kwargs):
    global calls
    if point == '_write_code_json' and args[1].get('state') == 'ready':
        os._exit(73)
    result = original(*args, **kwargs)
    calls += 1
    if point != '_write_code_json' and calls == (5 if point == 'publish_batch' else 1):
        os._exit(73)
    return result
setattr(owner, point, interrupt)
admin.main()
"""


@pytest.mark.parametrize("point", ["begin", "publish_batch", "finalize", "_write_code_json"])
def test_resume_after_process_loss(live_target, tmp_path, point, monkeypatch, record_property):
    receipts = []
    inspections = []
    inspect = PostgresCodeGraphStore.inspect

    def count_inspection(store, session_id, cursor=None):
        inspections.append(session_id)
        return inspect(store, session_id, cursor)

    monkeypatch.setattr(PostgresCodeGraphStore, "inspect", count_inspection)
    publish_batch = PostgresCodeGraphStore.publish_batch

    def count_batch(store, session, batch):
        response = publish_batch(store, session, batch)
        if response.get("accepted"):
            receipts.append((session.session_id, batch.kind, batch.ordinal, batch.byte_count))
        return response

    monkeypatch.setattr(PostgresCodeGraphStore, "publish_batch", count_batch)
    project = create_sqlite_project(tmp_path)
    _configure_mcp_project(project)
    env = {k: v for k, v in os.environ.items() if not k.startswith("IWIKI_")}
    env.update(live_target)
    args = ["code", "publish", "--project", str(project), "--resume", "--json"]
    lost = subprocess.run(
        [sys.executable, "-c", _CRASH_DRIVER, *args], env={**env, "TEST_STOP_POINT": point},
        capture_output=True, timeout=45,
    )
    assert lost.returncode == 73, lost.stdout
    record_property("pinned_bytes_after_exit", sum(
        p.stat().st_size for p in tmp_path.glob("wiki/.iwiki/resume-*/*") if p.is_file()
    ))
    if point == "publish_batch":
        assert len(receipts) == 5
    later_revision = None
    executable = str(Path(sys.executable).with_name("iwiki-mcp"))
    if point == "finalize":
        # A later ordinary publisher must remain active while the former one recovers.
        (project / "service.py").write_text("def later_revision(): return 20\n")
        later = subprocess.run([executable, *[a for a in args if a != "--resume"]],
                               env=env, capture_output=True, timeout=45)
        assert later.returncode == 0, later.stdout
        later_revision = json.loads(later.stdout)["snapshot_revision"]
    # A fresh invocation on this checkout would now build different source.
    (project / "service.py").unlink()
    # A valid upload pin must bypass the local build even with new inputs.
    (project / "unpinned.py").write_text("def unpinned(): return 2\n")
    replay_driver = """
from iwiki_mcp import admin
from iwiki_mcp.codegraph.indexer import CodeGraphIndexer
def unexpected_build(*args, **kwargs):
    raise AssertionError("upload replay entered the local builder")
CodeGraphIndexer.build = unexpected_build
admin.main()
"""
    command = [sys.executable, "-c", replay_driver, *args]

    def run():
        reply = subprocess.run(command, env=env, capture_output=True, timeout=45)
        assert reply.returncode == 0, reply.stdout
        return json.loads(reply.stdout)
    first = run()
    assert first["state"] == "ready" and first["counts"]["files"] == 1
    assert run()["snapshot_revision"] == first["snapshot_revision"]
    assert len(receipts) == len(set(receipts))
    record_property("receipt_page_calls", len(inspections))
    record_property("acknowledged_bytes", sum(row[-1] for row in receipts))
    if later_revision:
        assert first["snapshot_revision"] != later_revision
        transport = RemoteMcpTransport(environ=env, primary="docs", verify_binding=True)
        assert McpCodeGraphReader(transport).status()["snapshot_revision"] == later_revision


def test_resume_cli_retries_a_quota_refused_batch_on_the_original_session(
    live_target, hosted_runtime, clean_postgres, tmp_path, monkeypatch,
):
    from dataclasses import replace
    from io import StringIO
    from iwiki_mcp import admin
    from iwiki_mcp.postgres.publication_roles import provision_publication_pair

    project = create_sqlite_project(tmp_path)
    _configure_mcp_project(project)
    args = ["code", "publish", "--project", str(project), "--resume", "--json"]
    original = server._HOSTED_CODE_GRAPH

    def install(settings):
        provision_publication_pair(
            clean_postgres, hosted_runtime.runtime.config.storage.user,
            os.environ["IWIKI_PUBLICATION_DB_USER"], settings.publication_limits,
        )
        monkeypatch.setattr(server, "_HOSTED_CODE_GRAPH", settings)

    install(replace(original, max_publisher_staged_bytes=1))
    stdout = StringIO()
    assert admin.run(args, environ=live_target, stdout=stdout, stderr=StringIO()) == 1
    refusal = json.loads(stdout.getvalue())
    assert refusal["error"] == "resume_failed"
    assert refusal["code"] == "publication_quota_exceeded"
    assert refusal["resource"] == "staged_bytes"

    import psycopg
    with psycopg.connect(clean_postgres, autocommit=True) as conn:
        [(session_id,)] = conn.execute(
            "SELECT session_id FROM iwiki.code_graph_publication_sessions WHERE state='staging'"
        ).fetchall()
        install(original)
        stdout = StringIO()
        assert admin.run(args, environ=live_target, stdout=stdout, stderr=StringIO()) == 0
        assert json.loads(stdout.getvalue())["state"] == "ready"
        assert conn.execute(
            "SELECT session_id,state FROM iwiki.code_graph_publication_sessions"
        ).fetchall() == [(session_id, "ready")]
