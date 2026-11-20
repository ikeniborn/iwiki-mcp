"""Recover a real publisher process after its fourth durable file."""
import json
import subprocess
import sys

from .synthetic_wiki import create_sqlite_project


_CLI = """
import os
from pathlib import Path
import sys
from iwiki_mcp.admin import main
from iwiki_mcp.codegraph.indexer import BuildControl
from iwiki_mcp.codegraph.languages.python import PythonAdapter

log, stop, project, mode = sys.argv[1:]
from iwiki_mcp.codegraph.languages.bash import BashAdapter
from iwiki_mcp.codegraph.languages.typescript import TypeScriptAdapter
from iwiki_mcp.codegraph.languages.javascript import JavaScriptAdapter
accept = BuildControl.accept_file
count = 0

def trace(parse):
    def traced(self, source, path):
        with Path(log).open('a') as stream:
            print(path, file=stream)
        return parse(self, source, path)
    return traced

def acknowledged(self):
    global count
    accept(self)
    count += 1
    if count == int(stop):
        if mode == 'stdio':
            import time
            gate = Path(project).parent / 'exit'
            gate.with_suffix('.ready').touch()
            while not gate.exists():
                time.sleep(.02)
        os._exit(73)

for adapter in (PythonAdapter, BashAdapter, TypeScriptAdapter, JavaScriptAdapter):
    adapter.parse_file = trace(adapter.parse_file)
BuildControl.accept_file = acknowledged
if mode == 'stdio':
    from iwiki_mcp.server import main
    sys.argv = ['iwiki-mcp']
else:
    sys.argv = ['iwiki-mcp', 'code', 'publish', '--project', project, '--json']
main()
"""


def test_new_cli_reuses_only_compatible_completed_files(tmp_path):
    project = create_sqlite_project(tmp_path)
    for i in range(8):
        (project / f'm{i}.py').write_text(f'def f{i}(): return {i}\n')

    def publish(name, stop=0):
        log = tmp_path / name
        result = subprocess.run(
            [sys.executable, '-c', _CLI, str(log), str(stop), str(project), "cli"],
            capture_output=True, text=True, timeout=90,
        )
        assert result.returncode == (73 if stop else 0), result.stderr
        calls = log.read_text().splitlines()
        if stop:
            return calls
        assert 'state=ready' in result.stderr
        return calls, json.loads(result.stdout)

    config = project / '.iwiki.toml'
    config.write_text(config.read_text().replace(
        '["python"]', '["python", "typescript", "javascript", "bash"]'))
    (project / 'a.js').write_text('export const x = 1;\n')
    (project / 'b.ts').write_text('export const y: number = 2;\n')
    (project / 'c.sh').write_text('f() { true; }\n')

    completed = publish('interrupted', 4)
    assert len(set(completed)) == 4
    original, removed, moved, hit = completed
    (project / original).write_text('from replacement import replacement\n')
    (project / removed).unlink()
    (project / moved).rename(project / 'moved.py')
    (project / 'replacement.py').write_text('def replacement(): return 1\n')
    resumed_calls, resumed = publish('resumed')
    assert hit not in resumed_calls
    # Preserve the entire first cache, then rebuild the identical checkout.
    (tmp_path / 'wiki' / '.iwiki').rename(tmp_path / 'previous-cache')
    clean_calls, clean = publish('clean')
    assert sorted(resumed_calls + [hit]) == sorted(clean_calls)
    assert removed not in clean_calls and moved not in clean_calls
    assert resumed['snapshot_revision'] == clean['snapshot_revision']
    assert resumed['counts'] == clean['counts']


def test_new_stdio_job_recovers_after_worker_exit(tmp_path):
    import asyncio
    from tests.test_mcp_smoke import embedding_server

    with embedding_server() as (url, _requests):
        asyncio.run(_recover_stdio(tmp_path, url))


async def _recover_stdio(tmp_path, url):
    import asyncio
    from datetime import timedelta
    import os

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    project = create_sqlite_project(tmp_path)
    for i in range(8):
        (project / f'm{i}.py').write_text(f'x = {i}\n')
    env = {**os.environ, 'IWIKI_PROJECT_DIR': str(project),
           'IWIKI_BASE_DIR': str(tmp_path / 'wiki'), 'IWIKI_LLM_BASE_URL': url,
           'IWIKI_LLM_KEY': 'fixture', 'IWIKI_EMBED_DIMENSIONS': '2',
           'NO_PROXY': '127.0.0.1,localhost'}
    gate = tmp_path / 'exit'
    old_job = None

    async def call(session, name, **kwargs):
        answer = await session.call_tool(name, kwargs)
        assert not answer.isError, answer.content
        return json.loads(answer.content[0].text)

    for attempt in ('first', 'second'):
        log = tmp_path / attempt
        params = StdioServerParameters(command=sys.executable, env=env, args=[
            '-c', _CLI, str(log), '4' if attempt == 'first' else '0', str(project), 'stdio'])
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(
                reader, writer, read_timeout_seconds=timedelta(seconds=15)
            ) as session:
                await session.initialize()
                await call(session, 'wiki_bind', read=['docs'], write=['docs'], primary='docs')
                if old_job:
                    old = await call(session, 'wiki_code_status', job_id=old_job, job_only=True)
                    assert not old.get('job')
                started = await call(session, 'wiki_code_index', wait_seconds=0)
                job = started['job']['id']
                if attempt == 'first':
                    old_job = job
                    for _ in range(200):
                        if (tmp_path / 'exit.ready').exists():
                            break
                        await asyncio.sleep(.05)
                    try:
                        assert (tmp_path / 'exit.ready').exists()
                        current = await call(session, 'wiki_code_status', job_id=job, job_only=True)
                        assert current['job']['state'] == 'running'
                    finally:
                        gate.touch()
                    await asyncio.sleep(.15)
                else:
                    assert job != old_job
                    for _ in range(200):
                        current = await call(session, 'wiki_code_status', job_id=job, job_only=True)
                        if current['job']['state'] != 'running':
                            break
                        await asyncio.sleep(.05)
                    assert current['job']['state'] == 'ready'
                    assert set(log.read_text().splitlines()).isdisjoint(
                        (tmp_path / 'first').read_text().splitlines())
