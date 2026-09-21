"""Binding table semantics. No database: this is pure in-process state."""
from iwiki_mcp.http import _SessionBindings
from iwiki_mcp.postgres.auth import AuthContext


def _context(token_id: str = "t1", iwiki_id: str = "w1") -> AuthContext:
    return AuthContext(
        token_id=token_id,
        iwiki_id=iwiki_id,
        read_domains=("alpha",),
        write_domains=("alpha",),
        primary="alpha",
    )


def test_remove_deletes_a_binding_its_owner_asks_for():
    bindings = _SessionBindings()
    owner = _context()
    bindings.store("s1", owner, "state")

    assert bindings.remove("s1", owner) is True
    assert bindings.resolve("s1", owner) is None


def test_remove_refuses_another_tokens_binding():
    bindings = _SessionBindings()
    owner = _context(token_id="t1")
    stranger = _context(token_id="t2")
    bindings.store("s1", owner, "state")

    assert bindings.remove("s1", stranger) is False
    assert bindings.resolve("s1", owner) == "state"


def test_remove_of_an_unknown_session_is_not_an_error():
    bindings = _SessionBindings()

    assert bindings.remove("never-existed", _context()) is False
    assert bindings.remove(None, _context()) is False
