"""OKF metadata changes for live MCP writes. Importers retain authored evidence."""
from __future__ import annotations

import datetime as dt
import re

from .engine import trust


class ProvenanceError(ValueError):
    def __init__(self, code: str, hint: str):
        self.code = code
        self.hint = hint
        super().__init__(code)


_PLACEHOLDERS = {"unknown", "anonymous", "unspecified", "legacy-client", "agent", "mcp"}


def agent_id(name, version):
    if not all(isinstance(v, str) and v.strip() == v and v and not re.search(r"[\s/:]", v)
               and v.lower() not in _PLACEHOLDERS for v in (name, version)):
        return None
    actor = f"agent:{name}/{version}"
    return actor if trust.valid_actor(actor) else None


def client_agent(name, version):
    if not isinstance(name, str):
        return None
    if name.startswith("agent:"):
        return agent_id(name[6:], version)
    if name.lower() in {"codex", "codex-cli", "codex-mcp-client", "claude-code", "claude code"}:
        return agent_id(name.replace(" ", "-"), version)
    return None


def live_actor(actor, *, verifier=False):
    kind, _, identity = actor.partition(":") if isinstance(actor, str) else ("", "", "")
    if identity.strip() == identity and identity and identity.lower() not in _PLACEHOLDERS:
        if kind == "human" or (verifier and kind == "process"):
            if trust.valid_actor(actor) and not any(ord(c) < 32 for c in identity):
                return actor
        if kind == "agent":
            producer, _, version = identity.partition("/")
            if agent_id(producer, version):
                return actor
    raise ProvenanceError("invalid_verifier" if verifier else "invalid_author",
                          "Supply human:<id> or agent:<producer>/<version>."
                          + (" A verifier may also be process:<id>." if verifier else ""))


def events(meta):
    value = meta.get("verified")
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _now():
    return dt.datetime.now(dt.timezone.utc)


def record_generation(meta, author, editing_agent):
    if author is None:
        author = editing_agent()
        if author is None:
            raise ProvenanceError("author_required",
                                  "Identify the editing agent and its version or the human author.")
    actor = live_actor(author)
    now = _now()
    generated = meta.get("generated")
    evidence = [record for record in events(meta)
                if isinstance(record, dict) and trust.valid_actor(record.get("by"))]
    evidence.append(generated)
    for record in evidence:
        if isinstance(record, dict):
            previous = trust.instant_key(record.get("at"))
            if previous is not None and previous >= (now, ""):
                raise ProvenanceError("clock_conflict",
                                      "Check the clock and existing provenance before retrying.")
    meta["generated"] = {**(generated if isinstance(generated, dict) else {}),
                         "by": actor, "at": now.isoformat()}


def record_review(meta, verifier):
    actor = live_actor(verifier, verifier=True)
    now = _now()
    generated = meta.get("generated")
    if "generated" in meta:
        at = trust.instant_key(generated.get("at")) if isinstance(generated, dict) else None
        if at is None or at > (now, ""):
            raise ProvenanceError("invalid_generation_time",
                                  "Check the generation timestamp before confirming this content.")
    meta["verified"] = [*events(meta), {"by": actor, "at": now.isoformat()}]


def set_deadline(meta, deadline):
    if deadline is None:
        return
    if deadline == "":
        meta.pop("stale_after", None)
    elif trust.instant(deadline) is None:
        raise ProvenanceError("invalid_stale_after", "Supply an RFC 3339 timestamp or date.")
    else:
        meta["stale_after"] = deadline
