"""A busy server must still answer the liveness probe."""

from __future__ import annotations

import threading
import time

from starlette.testclient import TestClient

from iwiki_mcp import server


def _tool_call(client, token, name, arguments):
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )


def test_a_saturated_tool_ceiling_does_not_stall_the_liveness_probe(
    hosted_runtime, monkeypatch
):
    """The probe is unauthenticated, so it needs a free thread and a free connection."""
    token = hosted_runtime.token
    original = server._resolved_binding

    def slow_binding(*args, **kwargs):
        time.sleep(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(server, "_resolved_binding", slow_binding)
    monkeypatch.setattr(server._TOOL_LIMITER, "total_tokens", 2, raising=False)

    with TestClient(
        hosted_runtime.runtime.app, base_url="http://127.0.0.1:8765"
    ) as client:
        assert client.get("/mcp").status_code in {401, 405}

        started = threading.Barrier(5)
        errors: list[BaseException] = []

        def occupy():
            try:
                started.wait(timeout=30)
                _tool_call(client, token, "wiki_status", {})
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                errors.append(exc)

        threads = [threading.Thread(target=occupy, daemon=True) for _ in range(4)]
        for thread in threads:
            thread.start()
        started.wait(timeout=30)

        probe_started = time.monotonic()
        status = client.get("/mcp").status_code
        probe_elapsed = time.monotonic() - probe_started

        for thread in threads:
            thread.join(timeout=60)

    assert not errors, errors
    assert status in {401, 405}
    assert probe_elapsed < 2.0, (
        f"the probe took {probe_elapsed:.2f}s while tools were saturated"
    )


def test_genuinely_overlapping_updates_yield_one_success_and_one_conflict(
    hosted_runtime,
):
    """The old four-way test never overlapped: the event loop serialized it."""
    token = hosted_runtime.token

    with TestClient(
        hosted_runtime.runtime.app, base_url="http://127.0.0.1:8765"
    ) as client:
        seeded = _tool_call(client, token, "wiki_write_page", {
            "domain": "docs",
            "slug": "reference/overlap-probe",
            "type": "reference",
            "markdown": "# Overlap probe\n\n## Body\n\nSeed.\n",
        })
        assert seeded.status_code == 200, seeded.text

        results: list[str] = []
        barrier = threading.Barrier(2)

        def update(marker: str):
            barrier.wait(timeout=30)
            response = _tool_call(client, token, "wiki_update_page", {
                "domain": "docs",
                "slug": "reference/overlap-probe",
                "heading": "Body",
                "new_body": f"Body\n\nWritten by {marker}.\n",
                "expected_revision": 1,
            })
            results.append(response.text)

        threads = [threading.Thread(target=update, args=(m,)) for m in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

    assert len(results) == 2, results
    assert sum("conflict" in text for text in results) == 1, results
