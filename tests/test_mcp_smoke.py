import os
import sys
from datetime import timedelta

import pytest

mcp_client = pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


@pytest.mark.asyncio
async def test_lists_tools_and_status(tmp_path):
    base = tmp_path / "wiki"
    (base / "backend").mkdir(parents=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    env = dict(os.environ)
    env["IWIKI_BASE_DIR"] = str(base)
    env["IWIKI_PROJECT_DIR"] = str(proj)
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
            assert res.content
