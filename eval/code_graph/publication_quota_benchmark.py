"""Measure hosted quota admission on matched revisions with PostgreSQL pytest fixtures.

Copy this module into tests/postgres/ on each revision and invoke it explicitly.
Record peak Python heap in a separate single-publisher run.
"""
import json
import os
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from starlette.testclient import TestClient

from iwiki_mcp.codegraph.publication import iter_snapshot_batches
from tests.postgres.conftest import RankedPayload
from tests.postgres.test_code_graph_contract import _McpRoute, _open_session


def test_measure(hosted_runtime):
    count = int(os.environ["BENCH_PUBLISHERS"])
    profile = os.environ.get("BENCH_HEAP") == "1"
    assert count in (1, 2) and (not profile or count == 1)
    barrier = Barrier(count)
    inputs = []
    for domain in ("docs", "private")[:count]:
        payload = RankedPayload(domain)
        batches = list(iter_snapshot_batches(payload.rows, max_rows=4, max_bytes=1_000_000))
        token = hosted_runtime.auth.create_token(
            "wiki-a", domain, read_domains=[domain], write_domains=[domain],
        )["token"]
        inputs.append((domain, payload, batches, token))
    with TestClient(hosted_runtime.runtime.app, base_url="http://127.0.0.1:8080") as client:
        def publish(item):
            domain, payload, batches, token = item
            route = _McpRoute(client, token, _open_session(client, token))
            assert "error" not in route.call("wiki_bind", {"primary": domain})
            barrier.wait()
            start = time.perf_counter()
            session = route.begin(payload.header)
            assert "session_id" in session, session
            acks = []
            for batch in batches:
                before = time.perf_counter()
                result = route.publish_batch(session, batch)
                acks.append(time.perf_counter() - before)
                assert result.get("accepted"), result
            if profile:
                tracemalloc.start()
            final = route.finalize(session)
            elapsed = time.perf_counter() - start
            heap = 0
            if profile:
                heap = tracemalloc.get_traced_memory()[1]
                tracemalloc.stop()
            assert final.get("state") == "ready", final
            return {
                "elapsed": elapsed,
                "acks": acks,
                "heap": heap,
                "staged_bytes": sum(batch.byte_count for batch in batches),
            }

        with ThreadPoolExecutor(count) as executor:
            results = list(executor.map(publish, inputs))
    with Path(os.environ["BENCH_FILE"]).open("a") as out:
        print(json.dumps({"label": os.environ["BENCH_LABEL"], "results": results}), file=out)
