from __future__ import annotations

import pytest

from iwiki_mcp import server


@pytest.fixture
def hosted_session():
    """Install one hosted binding state so answers carry provenance."""
    tokens = []

    def install(source):
        binding = server.base.PostgresBinding(
            host="127.0.0.1",
            port=5432,
            database="iwiki_test",
            user="iwiki",
            sslmode="prefer",
            iwiki_id="wiki-a",
            read=("payments", "accounts"),
            write=("payments",),
            primary="payments",
            project_dir="/not-used",
            embed_model="fixture-model",
            embed_dimensions=3,
            rerank_model="",
            password="secret",
        )
        selected = server._HostedSelectedState(binding, source=source)
        state = server._HostedBindingState(selected, selected.get())
        state.bind_session("session-a")
        tokens.append(server._SESSION_BINDING.set(state))
        return state

    try:
        yield install
    finally:
        for token in reversed(tokens):
            server._SESSION_BINDING.reset(token)
