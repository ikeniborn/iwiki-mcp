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


EXPECTED_TOOLS = {
    "wiki_status", "wiki_list_domains", "wiki_list_pages", "wiki_read_page",
    "wiki_search", "wiki_related", "wiki_write_page", "wiki_update_page",
    "wiki_delete_page", "wiki_index", "wiki_create_domain", "wiki_bind",
    "wiki_lint", "wiki_remediation_plan", "wiki_migrate_okf", "wiki_apply_okf",
    "wiki_export_okf", "wiki_sync",
    "wiki_code_status", "wiki_code_index", "wiki_code_search",
    "wiki_code_context",
}


def _enum_values(schema):
    if isinstance(schema, dict):
        values = set(schema.get("enum", []))
        for value in schema.values():
            values.update(_enum_values(value))
        return values
    if isinstance(schema, list):
        values = set()
        for value in schema:
            values.update(_enum_values(value))
        return values
    return set()


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
    (base / "backend" / "page.md").write_text(
        "# Page\n\n## Body\ntext\n", encoding="utf-8"
    )
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
                listed = (await session.list_tools()).tools
                tools = {tool.name: tool for tool in listed}
                assert set(tools) == EXPECTED_TOOLS
                search_schema = tools["wiki_search"].inputSchema
                assert "mode" not in search_schema.get("required", [])
                assert _enum_values(search_schema["properties"]["mode"]) == {
                    "hybrid", "lexical", "semantic",
                }
                code_tools = {
                    name: tool
                    for name, tool in tools.items()
                    if name.startswith("wiki_code_")
                }
                assert set(code_tools) == {
                    "wiki_code_status",
                    "wiki_code_index",
                    "wiki_code_search",
                    "wiki_code_context",
                }
                assert all(
                    "domain" not in tool.inputSchema.get("properties", {})
                    for tool in code_tools.values()
                )
                context_schema = tools["wiki_code_context"].inputSchema
                context_properties = context_schema["properties"]
                assert "seeds" in context_properties
                assert "symbols" not in context_properties
                assert context_properties["include_source"]["default"] is False
                bind_schema = tools["wiki_bind"].inputSchema
                assert "write" in bind_schema["properties"]
                assert "primary" in bind_schema["properties"]
                update_schema = tools["wiki_update_page"].inputSchema
                assert "new_heading" in update_schema["properties"]
                assert "new_heading" not in update_schema.get("required", [])
                assert "expected_revision" in update_schema["properties"]
                assert "expected_revision" not in update_schema.get("required", [])
                delete_schema = tools["wiki_delete_page"].inputSchema
                assert "expected_revision" in delete_schema["properties"]
                assert "expected_revision" not in delete_schema.get("required", [])
                res = await session.call_tool("wiki_status", {})
                assert not res.isError
                assert res.content
                status_payload = json.loads(res.content[0].text)
                assert status_payload["write"] == []
                lint_result = await session.call_tool(
                    "wiki_lint", {"domain": "backend"}
                )
                assert not lint_result.isError
                lint_payload = json.loads(lint_result.content[0].text)
                assert lint_payload["reports"]["backend"]["graph"]["state"] == "missing"
                assert not (base / ".iwiki" / "graph.sqlite3").exists()

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
