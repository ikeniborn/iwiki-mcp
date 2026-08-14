"""Offline unit checks for hosted HTTP boundary helpers."""


def test_session_binding_registry_expires_abandoned_records(monkeypatch):
    from iwiki_mcp import http
    from iwiki_mcp.postgres.auth import AuthContext

    now = 100.0
    monkeypatch.setattr(http.time, "monotonic", lambda: now)
    sessions = http._SessionBindings()
    context = AuthContext("wiki-a", "token-a", ("docs",), (), None)
    state = object()
    sessions.store("session-a", context, state)

    now += http._SESSION_IDLE_SECONDS + 1
    assert sessions.resolve("session-b", context) is None
    assert sessions.resolve("session-a", context) is None
