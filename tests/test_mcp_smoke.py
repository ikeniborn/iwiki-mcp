import json
import os
import sys
import threading
from contextlib import contextmanager
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

mcp_client = pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


@contextmanager
def embedding_server():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            requests.append({"path": self.path, "payload": payload})
            body = json.dumps(
                {"data": [{"embedding": [0.1, 0.2]}]}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/v1", requests
    finally:
        server.shutdown()
        thread.join(timeout=5)
        thread_stopped = not thread.is_alive()
        server.server_close()
        assert thread_stopped


@pytest.mark.asyncio
async def test_lists_tools_and_status(tmp_path, monkeypatch):
    hostile_proxy = "http://127.0.0.1:9"
    proxy_names = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    )
    for name in proxy_names:
        monkeypatch.setenv(name, hostile_proxy)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")

    base = tmp_path / "wiki"
    (base / "backend").mkdir(parents=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    env = dict(os.environ)
    for name in proxy_names:
        env.pop(name, None)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    env["IWIKI_BASE_DIR"] = str(base)
    env["IWIKI_PROJECT_DIR"] = str(proj)
    with embedding_server() as (base_url, requests):
        env["IWIKI_LLM_BASE_URL"] = base_url
        env["IWIKI_LLM_KEY"] = "smoke-test-key"
        env["IWIKI_EMBED_MODEL"] = "smoke-test-model"
        env["IWIKI_EMBED_DIMENSIONS"] = "2"
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "iwiki_mcp.server"], env=env
        )
        async with stdio_client(params) as (r, w):
            async with ClientSession(
                r, w, read_timeout_seconds=timedelta(seconds=10)
            ) as session:
                await session.initialize()
                tools = {t.name for t in (await session.list_tools()).tools}
                assert {"wiki_status", "wiki_search", "wiki_write_page"} <= tools
                res = await session.call_tool("wiki_status", {})
                assert not res.isError
                assert res.content

        assert requests == [
            {
                "path": "/v1/embeddings",
                "payload": {
                    "model": "smoke-test-model",
                    "input": ["iwiki startup probe"],
                    "dimensions": 2,
                },
            }
        ]
