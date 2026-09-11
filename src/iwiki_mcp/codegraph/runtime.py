"""Fail-soft request-scoped runtime facade for the project code graph."""
from __future__ import annotations

import atexit
from dataclasses import asdict
import json
import logging
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Callable, Mapping, Protocol

from filelock import Timeout

from . import models as codegraph_models
from .config import (
    KNOWN_LANGUAGES,
    CodeGraphConfig,
    CodeGraphConfigError,
    load_code_graph_config,
)
from .context import (
    CodeGraphContext,
    CodeGraphContextError,
    ContextRequest,
    capture_project_root,
    validate_context_request,
)
from .fingerprint import config_fingerprint, parser_fingerprint
from .indexer import (
    AdapterFactory,
    BuildControl,
    CodeGraphIndexer,
    CodeGraphStaleError,
    CodeGraphStoreFailure,
    CodeGraphUnsafePathError,
    _wiki_read_lock,
    exact_ready_metadata,
    sanitize_warning_codes,
    valid_envelope,
)
from .linking import selector_capture_budget
from .location import CodeGraphLocationError, CodeGraphLocationResolver
from .models import CodeGraphError
from .query import (
    CodeGraphQuery,
    CodeGraphQueryError,
    validate_search_request,
)
from .publication import SnapshotHeader, graph_payload_revision
from .schema import SCHEMA_VERSION, CodeGraphStoreError
from .store import (
    CodeGraphStore,
    _is_canonical_revision,
    code_graph_read_lock,
    code_graph_write_lock,
)


LOGGER = logging.getLogger(__name__)
_INDEX_HINT = "run wiki_code_index"
_DEFAULT_RESOLVER_VERSION = "resolver-v1"
_PHASE_NAMES = (
    "discovery",
    "fingerprint",
    "parsing",
    "normalization",
    "resolution",
    "persistence",
    "validation",
    "canonical_verification_1",
    "final_verification",
    "publication",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INDEX_GRACE_SECONDS = 0.5


class CodeGraphSource(Protocol):
    base: str
    project_dir: str
    primary: str | None


class _JobSnapshot:
    """Terminal record of a finished build, free of the job's object graph.

    The registry remembers this instead of the finished `_BuildJob` so the
    live slot can be cleared the moment a build ends: keeping the job would
    pin its `control`, its `result` dict and the runtime that produced them
    for the rest of a long-lived stdio session. It carries only what a poller
    reads, and `thread` is `None` because a terminal record owns no worker.
    """

    thread: threading.Thread | None = None

    def __init__(
        self,
        job: "_BuildJob",
        *,
        state: str,
        finished_at: float,
    ) -> None:
        self.domain_key = job.domain_key
        self.job_id = job.job_id
        self.started_at = job.started_at
        self.explicit = job.explicit
        self.state = state
        self.finished_at = finished_at
        # Why this build failed, for the one reason a poller cannot see in
        # the graph status it asked for: the snapshot is locally complete and
        # the published one is not. Captured here, as a scalar, rather than by
        # keeping the result dict the snapshot exists to let go of.
        self.publication_failed = _publication_failed(job.result)

    def describe(self) -> dict[str, object]:
        """Return the frozen caller-visible descriptor for this finished job."""
        return {
            "id": self.job_id,
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class _BuildJob:
    def __init__(
        self,
        domain_key: tuple[str, str],
        *,
        force: bool,
        languages: list[str] | None,
        explicit: bool,
        build_deadline: float,
    ) -> None:
        self.domain_key = domain_key
        self.job_id = secrets.token_hex(8)
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.state = "running"
        self.force = force
        self.languages = None if languages is None else tuple(languages)
        self.explicit = explicit
        # The monotonic deadline this build was started with. A caller that
        # joined the job did not choose it, so every judgement about whether
        # the *running* build can still publish must read it from here rather
        # than from the joining caller's own budget.
        self.build_deadline = build_deadline
        self.control = BuildControl()
        self.result: dict[str, object] = {}
        self.thread: threading.Thread | None = None
        self.terminal: _JobSnapshot | None = None

    def matches(
        self, *, force: bool, languages: list[str] | None, explicit: bool
    ) -> bool:
        """Report whether a new request asks for the work this job is doing.

        Provenance is part of the request: an explicit `wiki_code_index` call
        and a query-time auto-rebuild carry different build deadlines and a
        different `restore_prior_on_abort` policy even when `force` and
        `languages` happen to agree, so joining across that boundary would
        silently hand one caller the other's deadline and abort policy.
        """
        requested = None if languages is None else tuple(languages)
        return (
            self.force == force
            and self.languages == requested
            and self.explicit == explicit
        )

    def describe(self) -> dict[str, object]:
        """Return the caller-visible descriptor for this job.

        Exactly one unsynchronised read -- of `terminal` -- decides the whole
        answer, and the live branch then reads nothing else off the job. That
        is what makes the descriptor atomic, not the order `finish()` writes
        in: a second read could land after `finish()` published the snapshot
        *and* set `job.state`, and would report a terminal state with no
        `finished_at`. The write order only narrows that gap; removing the
        read closes it.

        The literal `"running"` is this branch's actual meaning rather than a
        stand-in for `self.state`: the branch is reached only when no snapshot
        was published, and an unpublished snapshot is precisely a job that has
        not reached a terminal state. `state`/`finished_at` on the job stay
        for direct readers after a join; they are never part of a descriptor.
        """
        terminal = self.terminal
        if terminal is not None:
            return terminal.describe()
        return {
            "id": self.job_id,
            "state": "running",
            "started_at": self.started_at,
        }

    @property
    def publication_failed(self) -> bool:
        """Report a finished build's publication failure, never a live one's.

        Reads `terminal` exactly once, for the reason `describe()` does: a
        running build has no publication outcome yet, and the snapshot is the
        one place the finished one is recorded.
        """
        terminal = self.terminal
        return terminal is not None and terminal.publication_failed


_TERMINAL_HISTORY = 16
#: What a build records for itself when its publication raised. Mirrors the
#: shape `application.publish_snapshot` returns when it detects an
#: unpublishable result, so a caller reading `publication` sees one dialect
#: whichever way the publication failed -- and never the exception's text.
_PUBLICATION_FAILED = {"state": "failed", "error": "publication_failed"}


def _publication_failed(result: Mapping[str, object]) -> bool:
    """Report whether a build's publication did not reach `ready`.

    `publication` is absent -- not empty -- when nothing was asked to publish:
    `publish_mode = "sqlite"` selects no publisher, so no callback is
    installed and the key never appears. A mode whose publisher cannot be
    built is not this case; it raises on the caller's thread before any build
    starts.
    """
    publication = result.get("publication")
    if publication is None:
        return False
    return not (
        isinstance(publication, Mapping)
        and publication.get("state") == "ready"
    )


def _terminal_state(result: Mapping[str, object]) -> str:
    """Decide a finished build's terminal state, its publication included.

    A build that indexed but could not publish its snapshot is `failed`, not
    `ready`: under a publishing `publish_mode` the graph a reader answers
    from is still the old one, and a caller polling its handle must not be
    told `ready` on the strength of a local index alone.
    """
    if result.get("state") != "ready":
        return "failed"
    return "failed" if _publication_failed(result) else "ready"


class _BuildWorkerRegistry:
    """Own the process's single bounded code-graph build worker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job: _BuildJob | None = None
        # Terminal snapshots of the builds that already ended, newest last,
        # keyed by job id. A caller that took a handle must still be able to
        # learn how *its* build ended, and the builds that follow are not
        # rare: a query-time auto-rebuild against an unchanged checkout
        # finishes in well under a second, so a single remembered answer
        # would routinely be gone before the caller's next poll. Bounded
        # because nothing here is ever cleaned up otherwise; a snapshot is a
        # handful of scalars, so the cap can be generous without the pinning
        # that keeping whole jobs would bring. The cap is shared across every
        # domain this process builds for -- see `_evict_locked` for what that
        # costs and what it protects.
        self._terminal: dict[str, _JobSnapshot] = {}

    def start(
        self,
        domain_key,
        target,
        *,
        force=False,
        languages=None,
        explicit=False,
        build_deadline: float,
    ):
        """Start a build, or join the live one that matches this request.

        Returns `(job, started)`: `started` is True only when this call
        created and started the thread, False when it handed back someone
        else's already-running job (a join) or refused (`job is None`).
        Cancellation on wait expiry is a starter-only privilege, so callers
        must branch on `started` rather than reconstruct it later.
        """
        with self._lock:
            live = (
                self._job
                if self._job is not None
                and self._job.thread is not None
                and self._job.thread.is_alive()
                else None
            )
            if live is not None:
                if live.domain_key == domain_key and live.matches(
                    force=force, languages=languages, explicit=explicit
                ):
                    return live, False
                return None, False
            job = _BuildJob(
                domain_key,
                force=force,
                languages=languages,
                explicit=explicit,
                build_deadline=build_deadline,
            )

            def run() -> None:
                # The worker owns the terminal state: a caller that detached
                # at its wait expiry never comes back to record it, and
                # `finished_at` must be when the build ended rather than
                # whenever some caller happened to return. The target
                # publishes before it returns, so the publication's outcome is
                # already in `result` when `_terminal_state` reads it.
                try:
                    target(job.control, job.result)
                finally:
                    self.finish(job, _terminal_state(job.result))

            job.thread = threading.Thread(
                target=run,
                name="iwiki-code-graph-build",
                daemon=True,
            )
            self._job = job
            try:
                job.thread.start()
            except Exception:
                self._job = None
                raise
            return job, True

    def current(self, domain_key):
        """Return the live job for this domain, else its terminal snapshot.

        A live job answers first because only it can report `phase` progress;
        once it ends it leaves the slot and the newest snapshot for the same
        domain keeps answering in its place. This is the answer for a caller
        that names no job -- one that holds an id asks `terminal_by_id`.
        """
        with self._lock:
            job = self._job
            if job is not None and job.domain_key == domain_key:
                return job
            return self._newest_locked(domain_key)

    def terminal(self, domain_key) -> _JobSnapshot | None:
        """Return the newest finished build for this domain, live one or not."""
        with self._lock:
            return self._newest_locked(domain_key)

    def terminal_by_id(
        self, domain_key, job_id: str
    ) -> _JobSnapshot | None:
        """Return one domain's specific finished build, if remembered.

        The lookup a caller holding a handle needs: it asks about its own
        build rather than about whichever build happens to be the latest.
        `None` means the build is unknown here -- it is still running, it was
        never started in this process, or it has aged out of the bounded
        history -- and the caller falls back to `state`/`fresh`.

        `domain_key` is required even though the id alone would find the
        snapshot, and the check lives here rather than at any call site
        because this is the one place every future caller must pass
        through. A build belongs to exactly one domain, so answering across
        domains would not merely widen the answer -- it would tell a session
        that rebound to another primary that its graph is `ready` because a
        build for some *other* primary finished. This lookup asks `current`'s
        question more precisely; it must not be the looser of the two.
        """
        with self._lock:
            snapshot = self._terminal.get(job_id)
            if snapshot is None or snapshot.domain_key != domain_key:
                return None
            return snapshot

    def _newest_locked(self, domain_key) -> _JobSnapshot | None:
        """Return the most recent snapshot for one domain. Lock held."""
        for snapshot in reversed(self._terminal.values()):
            if snapshot.domain_key == domain_key:
                return snapshot
        return None

    def explicit_job(self) -> _BuildJob | None:
        """Return the live job when an explicit build is running.

        Deliberately lock-free, unlike every other reader here: the caller is
        the stdio server's idle predicate, which runs on the event loop that
        serves the whole session and must never wait on a lock. Each field it
        reads is written before the job becomes reachable through `_job`, and
        a single attribute read is atomic, so the worst answer is one moment
        stale -- which a one-second poll already tolerates.
        """
        job = self._job
        if job is None or not job.explicit:
            return None
        thread = job.thread
        if thread is None or not thread.is_alive():
            return None
        return job

    def finish(self, job: _BuildJob, state: str) -> None:
        """Record the terminal state, publish its snapshot, free the slot."""
        with self._lock:
            # Exactly once per job. The worker records the terminal state from
            # its own `finally`; the caller-side call after a completed join
            # only ever repeats that fact, and repeating it must not restamp
            # `finished_at` to whenever that caller happened to return.
            if job.terminal is not None:
                return
            # Build the snapshot exactly once, here, while the lock is held:
            # it is the only place a terminal answer is assembled, so no
            # reader ever has to re-combine `state` with `finished_at`.
            finished_at = time.time()
            snapshot = _JobSnapshot(
                job, state=state, finished_at=finished_at
            )
            # Publish it *first*, before the job's own fields. What makes a
            # descriptor atomic is `describe()` reading `terminal` and nothing
            # else (see its docstring); this order is the second half of that
            # bargain, keeping the loose `state` from ever being the newer of
            # the two facts. A reader that did consult `job.state` would, in
            # the opposite order, see a terminal state with no `finished_at`
            # -- the mirror of the race this snapshot exists to close, and one
            # the READMEs forbid just as firmly.
            job.terminal = snapshot
            job.state = state
            job.finished_at = finished_at
            self._terminal[snapshot.job_id] = snapshot
            self._evict_locked()
            self._release_locked(job)

    def _evict_locked(self) -> None:
        """Hold the history to its cap, sparing a domain's last answer.

        The cap is global, not per domain: a per-domain cap would make total
        retention grow with every domain a session rebinds through, which is
        the unbounded growth this history is capped to avoid. The cost is
        that one busy domain can push another's snapshots out, so eviction
        prefers the oldest snapshot of a domain that still has more than one
        and falls back to the global oldest only when every domain holds
        exactly one. A domain therefore keeps its most recent answer -- the
        one a poller holding a handle is most likely to ask for -- until the
        cap can only be met by taking someone's last.
        """
        while len(self._terminal) > _TERMINAL_HISTORY:
            per_domain: dict[tuple[str, str], int] = {}
            for snapshot in self._terminal.values():
                per_domain[snapshot.domain_key] = (
                    per_domain.get(snapshot.domain_key, 0) + 1
                )
            evicted = next(
                (
                    job_id
                    for job_id, snapshot in self._terminal.items()
                    if per_domain[snapshot.domain_key] > 1
                ),
                None,
            )
            if evicted is None:
                # Only now, when no domain holds a spare: `next(iter(...))` as
                # the default argument would be evaluated on every pass,
                # including the common one the generator answers.
                evicted = next(iter(self._terminal))
            del self._terminal[evicted]

    def is_active(self, domain_key: tuple[str, str]) -> bool:
        """Report whether a worker for this domain is still alive.

        Thread liveness, publication included: the question asked by anything
        that must not pre-empt a worker that still exists. Readers asking
        whether the *graph* is being rewritten want `is_indexing`.
        """
        with self._lock:
            return bool(
                self._job is not None
                and self._job.domain_key == domain_key
                and self._job.thread is not None
                and self._job.thread.is_alive()
            )

    def is_indexing(self, domain_key: tuple[str, str]) -> bool:
        """Report whether a live build is still writing this domain's graph.

        The predicate every local read consults. It stops being true when the
        build hands its finished snapshot to a publication: from that moment
        the local graph is complete and readable, and refusing reads until the
        remote publication returns would report a working graph as unavailable
        for as long as that publication takes. The worker is still alive then,
        which is what `is_active` and `explicit_job_active` keep answering.
        """
        with self._lock:
            return bool(
                self._job is not None
                and self._job.domain_key == domain_key
                and self._job.thread is not None
                and self._job.thread.is_alive()
                and self._job.control.indexing
            )

    @property
    def active_count(self) -> int:
        with self._lock:
            return int(
                self._job is not None
                and self._job.thread is not None
                and self._job.thread.is_alive()
            )

    def join(self, timeout: float | None = None) -> None:
        with self._lock:
            job = self._job
        if job is not None and job.thread is not None:
            job.thread.join(timeout)
            self.release(job)

    def release(self, job: _BuildJob) -> None:
        """Drop a finished job from the live slot."""
        with self._lock:
            self._release_locked(job)

    def _release_locked(self, job: _BuildJob) -> None:
        """Clear the live slot, but only for a job that has already ended.

        Terminality is the condition rather than thread liveness: the worker
        clears its own slot from inside its `finally`, where its thread is by
        definition still alive, while a caller that merely gave up waiting
        must never evict the build it is still waiting on. Once the slot is
        clear nothing reaches the job's `control`, its `result`, or the
        runtime behind them -- the terminal snapshot answers instead.
        """
        if self._job is job and job.terminal is not None:
            self._job = None

    def shutdown(self, timeout: float = 1.0) -> None:
        with self._lock:
            job = self._job
        if job is None or job.thread is None:
            return
        job.control.cancel()
        job.thread.join(max(0.0, timeout))
        self.release(job)


_BUILD_WORKERS = _BuildWorkerRegistry()


def worker_domain_key(binding: CodeGraphSource) -> tuple[str, str]:
    """Derive the build registry's key for one bound domain.

    A build belongs to the wiki base it publishes into and the primary
    domain it indexes, so the tool layer can name the very job a runtime
    started without holding -- or rebuilding -- that runtime.
    """
    return (str(Path(binding.base).absolute()), binding.primary or "")


def _resolve_job(
    domain_key: tuple[str, str], job_id: str | None
) -> _BuildJob | _JobSnapshot | None:
    """Find the job an answer should describe, by id or by domain.

    Without an id the domain's current job answers: the live one while a
    build runs, its newest terminal snapshot afterwards. With an id only
    that build answers -- the history first, because a finished build has
    left the live slot and whatever occupies it now is someone else's.

    Both lookups are scoped to `domain_key`, and neither is scoped here:
    `current` and `terminal_by_id` own that themselves, so no caller of
    either can widen it.
    """
    if job_id is None:
        return _BUILD_WORKERS.current(domain_key)
    remembered = _BUILD_WORKERS.terminal_by_id(domain_key, job_id)
    if remembered is not None:
        return remembered
    live = _BUILD_WORKERS.current(domain_key)
    return live if live is not None and live.job_id == job_id else None


def _warned(status: dict[str, object], warning: str) -> dict[str, object]:
    """Add one warning without mutating the reader's own answer.

    `warnings` is assumed to be a list when present -- that is what every
    producer on this path emits, and what the remote reader's JSON decodes
    to. A value of any other shape is *replaced*, not preserved: this is an
    assumption, not a check, and a caller that starts emitting a tuple or a
    bare string would lose it silently. Handle it here if that ever becomes
    reachable rather than discovering it downstream.
    """
    existing = status.get("warnings")
    warnings = list(existing) if isinstance(existing, list) else []
    if warning not in warnings:
        warnings.append(warning)
    return {**status, "warnings": warnings}


def job_unknown(status: dict[str, object]) -> dict[str, object]:
    """Report a named handle as unknown, leaving the answer otherwise whole.

    The one place the warning's name is spelled, so the local branch's
    "never issued, aged out, or another domain's" and the hosted branch's
    "this server issues no handles at all" stay the same answer to the
    caller. An error answer is left alone for the same reason it carries no
    job: it describes the graph, not the handle.
    """
    return status if "error" in status else _warned(status, "job_unknown")


def attach_job(
    domain_key: tuple[str, str],
    job_id: str | None,
    status: dict[str, object],
) -> dict[str, object]:
    """Attach this process's job descriptor to a non-error status answer.

    Lives at the tool layer rather than inside any one reader: the job is a
    fact about this process, not about the snapshot a `read_mode` selected,
    so `wiki_code_status` answers with it whichever reader produced
    `status`. An error answer explains why the graph cannot be read;
    attaching a job to it would wrongly suggest the error belongs to that
    job, and here -- above every branch that merges an error onto a status
    -- is the one place that invariant can actually hold.

    A named `job_id` that nothing knows is not an error: it may simply have
    aged out of the bounded history, and the graph status the caller also
    asked for is still valid. The answer keeps its shape, carries no job,
    and says so with `job_unknown`.
    """
    if "error" in status:
        return status
    job = _resolve_job(domain_key, job_id)
    if job is None:
        # A caller that named no handle has nothing unknown to be warned
        # about: `job_unknown` answers an unknown *id*, never the plain
        # absence of a build, which `state`/`fresh` already report.
        return status if job_id is None else job_unknown(status)
    descriptor = job.describe()
    # Read `state` once from the descriptor rather than re-reading
    # `job.state`: the worker can finish between the two reads, and a
    # second, later read could see "ready" after `describe()` already
    # captured "running" -- leaving `phase`/`phases_done` off an answer
    # that still claims `state: "running"`, a shape the README promises
    # cannot occur. The same read is what keeps `control` a live-job-only
    # attribute: a terminal snapshot never describes itself as running,
    # and owns no `control` to read.
    if descriptor["state"] == "running":
        descriptor["phase"] = job.control.phase
        descriptor["phases_done"] = list(job.control.phases_done)
    answer = {**status, "job": descriptor}
    # The detached poller's channel, and the one case where the graph status
    # it asked for cannot show the failure: the local snapshot is complete and
    # the published one is a revision behind, so `state` here reads `ready`
    # beside a `failed` job. `wiki_code_index`'s own answer says this in its
    # warnings; this is the same fact, on the answer that the feature's whole
    # point is to be read instead.
    return (
        _warned(answer, "publication_failed")
        if job.publication_failed
        else answer
    )


def explicit_job_active() -> bool:
    """Report whether an explicit `wiki_code_index` job is still running.

    Only an explicit build counts: a query-time auto-rebuild is started by a
    search, and letting one hold the stdio server open would turn any query
    against a dirty graph into an open-ended lease on the process.
    """
    return _BUILD_WORKERS.explicit_job() is not None


def shutdown_code_graph_workers(timeout: float = 1.0) -> None:
    """Cooperatively cancel and boundedly join process build worker."""
    _BUILD_WORKERS.shutdown(timeout)


atexit.register(shutdown_code_graph_workers)


class _NoopWikiSelectorResolver:
    def resolve(self, **_kwargs):
        return ()


def _not_configured() -> dict[str, object]:
    return {
        "error": "code graph is not configured",
        "code": "not_configured",
        "hint": "configure a primary domain and enable code_graph",
    }


_FIELD_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _whitelisted_field(name: object) -> str | None:
    """Only ever surface an identifier-shaped config field or parameter name.

    Guards an `invalid_config` response against ever leaking raw exception
    text: the offending name must look like a real field/parameter
    identifier (lowercase snake_case, bounded length), never free-form
    prose. A raise site always attaches a deliberate, bounded name (a
    dataclass field, a validator parameter, or a literal unrecognized TOML
    key) -- this is a shape check, not a fixed enumeration, so an unknown
    but well-formed key (e.g. a typo'd field) is still diagnosable.
    """
    return (
        name
        if isinstance(name, str) and _FIELD_NAME_PATTERN.fullmatch(name)
        else None
    )


def _invalid_config(field: str | None = None) -> dict[str, object]:
    result = {
        "error": "code graph configuration is invalid",
        "code": "invalid_config",
        "hint": "inspect code_graph project configuration",
    }
    whitelisted = _whitelisted_field(field)
    if whitelisted is not None:
        result["field"] = whitelisted
    return result


def _unsupported_language(available: tuple[str, ...]) -> dict[str, object]:
    declared = ", ".join(available) if available else "no language"
    return {
        "error": "language not available in the active snapshot",
        "code": "unsupported_language",
        "hint": "the active snapshot declares: " + declared,
    }


def _rebuilding_job_answer(job: _BuildJob) -> dict[str, object]:
    """Answer a caller whose wait ended while its build kept running."""
    descriptor = job.describe()
    # Same single-read gate `attach_job` applies: progress fields belong to a
    # running descriptor only. The worker publishes terminality from inside
    # its own `finally`, while its thread is still alive, so this is reachable
    # for a caller whose wait expires in that window -- and `phase` beside a
    # terminal `state` is the one shape the READMEs promise cannot occur.
    if descriptor["state"] == "running":
        descriptor["phase"] = job.control.phase
        descriptor["phases_done"] = list(job.control.phases_done)
    return {
        "state": "rebuilding",
        "fresh": False,
        "job": descriptor,
        "hint": "poll wiki_code_status for this job",
    }


def _rebuild_failed() -> dict[str, object]:
    return {
        "error": "code graph rebuild failed",
        "code": "rebuild_failed",
        "hint": "inspect wiki_code_status and retry",
    }


def _not_ready_defaults(state: object) -> tuple[str, str]:
    """Default diagnostics for a non-ready answer with no more specific error."""
    code = (
        f"code_graph_{state}" if isinstance(state, str) and state else "not_ready"
    )
    return "code graph is not ready for this query", code


def _not_ready(payload: Mapping[str, object]) -> dict[str, object]:
    """Guarantee a non-ready query answer names its own error and code.

    A caller-visible empty `results`/`nodes` list must never be mistaken for
    a real, ready answer that simply matched nothing: every non-ready branch
    of `query_guard`/`search` carries `error`, `code`, and `hint` alongside
    it. Existing, more specific diagnostics (e.g. a typed failure or a busy
    response) are preserved -- only a missing key is filled in.
    """
    result = dict(payload)
    error_default, code_default = _not_ready_defaults(result.get("state"))
    result.setdefault("error", error_default)
    result.setdefault("code", code_default)
    result.setdefault("hint", _INDEX_HINT)
    return result


def _typed_failure(error: CodeGraphError) -> dict[str, object]:
    responses = {
        "parse_failed": (
            "code graph parse failed",
            "inspect wiki_code_status and retry",
        ),
        "store_failed": (
            "code graph store failed",
            "inspect wiki_code_status and retry",
        ),
        "stale": ("code graph is stale", _INDEX_HINT),
        "unsafe_path": (
            "code graph source path is unsafe",
            "inspect code_graph project configuration",
        ),
    }
    code = getattr(error, "code", "rebuild_failed")
    if code not in responses:
        return _rebuild_failed()
    message, hint = responses[code]
    return {
        "error": message,
        "code": code,
        "hint": hint,
        "fresh": False,
    }


def sanitized_error(error: CodeGraphError) -> dict[str, object]:
    """Map typed graph failures without exposing exception text."""
    code = getattr(error, "code", "rebuild_failed")
    if code == "invalid_config":
        return _invalid_config(getattr(error, "parameter", None))
    if code == "unsupported_language":
        return _unsupported_language(getattr(error, "available", ()))
    if code == "not_configured":
        return _not_configured()
    if code == "busy":
        return CodeGraphRuntime._busy_response()
    return _typed_failure(error)


def _metadata(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _safe_nonnegative(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


def _safe_phase_timings(value: object) -> dict[str, int]:
    mapping = value if isinstance(value, Mapping) else {}
    return {
        phase: timing
        for phase in _PHASE_NAMES
        if type(timing := mapping.get(phase)) is int and timing >= 0
    }


def _pending_final_verify(metadata: Mapping[str, object]) -> bool:
    """Return whether final verification lacks durable success diagnostics."""
    duration = metadata.get("duration_ms")
    timings = _safe_phase_timings(metadata.get("phase_timings_ms"))
    return (
        metadata.get("state") == "ready"
        and metadata.get("publication_phase") == "pending_final_verify"
        and not (
            type(duration) is int
            and duration >= 0
            and set(timings) == set(_PHASE_NAMES)
        )
    )


class CodeGraphRuntime:
    """Resolve configuration once and isolate all optional graph failures."""

    def __init__(
        self,
        binding: CodeGraphSource,
        *,
        adapter_factories: Mapping[str, AdapterFactory] | None = None,
        resolver_version: str = _DEFAULT_RESOLVER_VERSION,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.binding = binding
        self.config: CodeGraphConfig | None = None
        self.paths = None
        self._store = None
        self._indexer = None
        self._context_root = None
        self._configuration_error: CodeGraphConfigError | None = None
        self._initialization_error = False
        self._unsafe_location = False
        self._worker_domain_key = worker_domain_key(binding)
        self._parser_version = ""
        self._grammar_version = ""
        self._adapter_version = ""
        self._resolver_version = resolver_version
        if binding.primary is None:
            return
        try:
            self.config = load_code_graph_config(
                binding.project_dir,
                environ=environ,
            )
        except CodeGraphConfigError as exc:
            self._configuration_error = exc
            return
        if not self.config.enabled:
            return
        try:
            self._context_root = capture_project_root(binding.project_dir)
            self.paths = CodeGraphLocationResolver(
                binding.base,
                binding.primary,
                binding.project_dir,
            ).resolve(ensure_excluded=False)
            factories = adapter_factories or {}
            try:
                selected_factories = {
                    language: factories[language]
                    for language in self.config.languages
                }
            except KeyError:
                self._configuration_error = CodeGraphConfigError(
                    "code_graph.languages names a language with no "
                    "registered adapter",
                    field="languages",
                )
                self.paths = None
                return
            self._parser_version = ";".join(
                f"{language}:{factory.parser_version}"
                for language, factory in selected_factories.items()
            )
            self._grammar_version = ";".join(
                f"{language}:{factory.grammar_version}"
                for language, factory in selected_factories.items()
            )
            self._adapter_version = ";".join(
                f"{language}:{factory.adapter_version}"
                for language, factory in selected_factories.items()
            )
            self._store = CodeGraphStore(
                self.paths.database,
                cache_base=binding.base,
            )
            self._indexer = CodeGraphIndexer(
                cache_base=binding.base,
                project_dir=binding.project_dir,
                domain=binding.primary,
                config=self.config,
                paths=self.paths,
                adapter_factories=selected_factories,
                resolver_version=self._resolver_version,
                wiki_selector_resolver=_NoopWikiSelectorResolver(),
            )
        except CodeGraphLocationError:
            self.paths = None
            self._store = None
            self._indexer = None
            self._unsafe_location = True
        except Exception:
            self.paths = None
            self._store = None
            self._indexer = None
            self._initialization_error = True

    def _unavailable(self) -> dict[str, object] | None:
        if self._unsafe_location:
            return _typed_failure(CodeGraphUnsafePathError())
        if self._initialization_error:
            return _rebuild_failed()
        if self._configuration_error is not None:
            return _invalid_config(self._configuration_error.field)
        if (
            self.binding.primary is None
            or self.config is None
            or not self.config.enabled
        ):
            return _not_configured()
        return None

    def _missing_status(
        self,
        normalization_versions: tuple[str, str] | None = None,
    ) -> dict[str, object]:
        if normalization_versions is None:
            normalization_versions = self._normalization_versions()
        return {
            "enabled": True,
            "domain": self.binding.primary,
            "state": "missing",
            "revision": None,
            "fresh": False,
            "schema_version": SCHEMA_VERSION,
            "parser_version": self._parser_version,
            "grammar_version": self._grammar_version,
            "adapter_version": self._adapter_version,
            "resolver_version": self._resolver_version,
            "normalizer_version": normalization_versions[0],
            "unicode_data_version": normalization_versions[1],
            "counts": {
                "languages": {},
                "files": 0,
                "modules": 0,
                "symbols": 0,
                "relations": 0,
                "entity_kinds": {},
                "symbol_kinds": {},
                "relation_types": {},
                "resolution_states": {},
            },
            "duration_ms": 0,
            "pending_final_verify": False,
            "module_warnings": 0,
            "excluded_files": 0,
            "truncated_files": 0,
            "truncated": False,
            "parser_errors": 0,
            "warnings": ["code_graph_missing"],
        }

    def _store_failure_status(self) -> dict[str, object]:
        return {
            **_typed_failure(CodeGraphStoreFailure()),
            "enabled": True,
            "domain": self.binding.primary,
            "state": "failed",
            "revision": None,
            "fresh": False,
            "duration_ms": 0,
            "pending_final_verify": False,
            "module_warnings": 0,
            "excluded_files": 0,
            "truncated_files": 0,
            "truncated": False,
            "parser_errors": 0,
            "warnings": ["code_graph_store_failed"],
            "hint": _INDEX_HINT,
        }

    @staticmethod
    def _normalization_versions() -> tuple[str, str]:
        return (
            codegraph_models.NORMALIZER_VERSION,
            codegraph_models.UNICODE_DATA_VERSION,
        )

    def _current_toolchain_fingerprint(
        self,
        normalization_versions: tuple[str, str],
    ) -> str:
        assert self.config is not None
        return parser_fingerprint(
            languages=self.config.languages,
            schema_version=SCHEMA_VERSION,
            parser_version=self._parser_version,
            grammar_version=self._grammar_version,
            adapter_version=self._adapter_version,
            resolver_version=self._resolver_version,
            normalizer_version=normalization_versions[0],
            unicode_data_version=normalization_versions[1],
        )

    def _with_rebuilding_state(
        self, status: dict[str, object], *, shared_writer: bool = False
    ) -> dict[str, object]:
        if (
            not shared_writer
            and not _BUILD_WORKERS.is_indexing(self._worker_domain_key)
        ):
            return status
        return {
            **status,
            "enabled": True,
            "state": "rebuilding",
            "fresh": False,
            "warnings": ["code_graph_rebuilding"],
            "hint": _INDEX_HINT,
        }

    def _read_status(
        self,
        *,
        persisted_metadata: Mapping[str, object] | None = None,
        normalization_versions: tuple[str, str] | None = None,
    ) -> dict[str, object]:
        """Read authoritative metadata/schema while publication cannot race."""
        assert self.paths is not None and self._store is not None
        if normalization_versions is None:
            normalization_versions = self._normalization_versions()
        if not self.paths.database.is_file():
            return self._with_rebuilding_state(
                self._missing_status(normalization_versions)
            )
        schema_version = self._store.inspect_schema_version()
        if schema_version is not None and schema_version != SCHEMA_VERSION:
            incompatible = self._missing_status(normalization_versions)
            incompatible["warnings"] = ["code_graph_incompatible"]
            incompatible["hint"] = _INDEX_HINT
            return self._with_rebuilding_state(incompatible)
        persisted = (
            persisted_metadata
            if persisted_metadata is not None
            else _metadata(self.paths.metadata)
        )
        try:
            with self._store.read_lease() as connection:
                data_version = connection.execute(
                    "PRAGMA data_version"
                ).fetchone()
                row = connection.execute(
                    "SELECT repository_id, git_commit, source_fingerprint, "
                    "config_fingerprint, parser_fingerprint, "
                    "normalizer_version, unicode_data_version, revision, state, "
                    "indexed_at FROM repositories WHERE repository_id = ?",
                    (self.binding.primary,),
                ).fetchone()
                if row is None:
                    return self._with_rebuilding_state(
                        self._missing_status(normalization_versions)
                    )
                storage_stamp = self._store.storage_stamp()
                row_after = connection.execute(
                    "SELECT repository_id, git_commit, source_fingerprint, "
                    "config_fingerprint, parser_fingerprint, "
                    "normalizer_version, unicode_data_version, revision, state, "
                    "indexed_at FROM repositories WHERE repository_id = ?",
                    (self.binding.primary,),
                ).fetchone()
                data_version_after = connection.execute(
                    "PRAGMA data_version"
                ).fetchone()
                if row != row_after or data_version != data_version_after:
                    raise CodeGraphStoreError(
                        "code graph changed during readiness proof"
                    )
        except Exception:
            return self._with_rebuilding_state(
                self._store_failure_status()
            )
        state = str(row[8])
        persisted_timings = _safe_phase_timings(
            persisted.get("phase_timings_ms")
        )
        persisted_duration = persisted.get("duration_ms")
        persisted_fingerprints = persisted.get("fingerprints")
        persisted_input = persisted.get("input_fingerprint")
        metadata_matches = (
            valid_envelope(persisted, state=state)
            and persisted.get("domain") == row[0]
            and persisted.get("revision") == row[7]
            and _is_canonical_revision(row[7])
            and persisted.get("schema_version") == SCHEMA_VERSION
            and persisted_fingerprints == {
                "source": row[2],
                "config": row[3],
                "parser": row[4],
            }
            and isinstance(persisted_input, str)
            and _SHA256.fullmatch(persisted_input) is not None
            and persisted.get("git_commit") == row[1]
            and persisted.get("indexed_at") == row[9]
            and persisted.get("storage_stamp") == storage_stamp
        )
        persisted_parser_version = persisted.get("parser_version")
        persisted_grammar_version = persisted.get("grammar_version")
        persisted_adapter_version = persisted.get("adapter_version")
        persisted_resolver_version = persisted.get("resolver_version")
        persisted_normalizer_version = persisted.get("normalizer_version")
        persisted_unicode_data_version = persisted.get("unicode_data_version")
        toolchain_mismatch = (
            metadata_matches
            and (
                persisted_parser_version != self._parser_version
                or persisted_grammar_version != self._grammar_version
                or persisted_adapter_version != self._adapter_version
                or persisted_resolver_version != self._resolver_version
                or persisted_normalizer_version != normalization_versions[0]
                or persisted_unicode_data_version != normalization_versions[1]
                or row[3] != config_fingerprint(self.config)
                or row[5] != normalization_versions[0]
                or row[6] != normalization_versions[1]
            )
        ) or (
            not metadata_matches
            and (
                row[4] != self._current_toolchain_fingerprint(
                    normalization_versions
                )
                or row[5] != normalization_versions[0]
                or row[6] != normalization_versions[1]
            )
        )
        if not metadata_matches:
            persisted = {}
        if state == "ready" and not metadata_matches:
            effective_state = "failed"
        elif state == "ready" and toolchain_mismatch:
            effective_state = "dirty"
        else:
            effective_state = state
        if metadata_matches:
            counts = dict(persisted["counts"])
            resolution_ratios = dict(persisted["resolution_ratios"])
        else:
            counts = dict(
                self._missing_status(normalization_versions)["counts"]
            )
            resolution_ratios = {}
        result = {
            "enabled": True,
            "domain": self.binding.primary,
            "state": effective_state,
            "revision": row[7],
            "fresh": effective_state == "ready",
            "git_commit": row[1],
            "fingerprints": {
                "source": row[2],
                "config": row[3],
                "parser": row[4],
            },
            "schema_version": SCHEMA_VERSION,
            "parser_version": (
                persisted_parser_version
                if isinstance(persisted_parser_version, str)
                else None
            ),
            "grammar_version": (
                persisted_grammar_version
                if isinstance(persisted_grammar_version, str)
                else None
            ),
            "adapter_version": (
                persisted_adapter_version
                if isinstance(persisted_adapter_version, str)
                else None
            ),
            "resolver_version": (
                persisted_resolver_version
                if isinstance(persisted_resolver_version, str)
                else None
            ),
            "normalizer_version": (
                persisted_normalizer_version
                if isinstance(persisted_normalizer_version, str)
                else None
            ),
            "unicode_data_version": (
                persisted_unicode_data_version
                if isinstance(persisted_unicode_data_version, str)
                else None
            ),
            "counts": counts,
            "resolution_ratios": resolution_ratios,
            "excluded_files": _safe_nonnegative(
                persisted.get("excluded_files")
            ),
            "truncated": (
                persisted.get("truncated")
                if type(persisted.get("truncated")) is bool
                else False
            ),
            "truncated_files": _safe_nonnegative(
                persisted.get("truncated_files")
            ),
            "parser_errors": _safe_nonnegative(
                persisted.get("parser_errors")
            ),
            "module_warnings": _safe_nonnegative(
                persisted.get("module_warnings")
            ),
            "pending_final_verify": (
                persisted.get("pending_final_verify") is True
            ),
            "indexed_at": row[9],
            "warnings": sanitize_warning_codes(persisted.get("warnings")),
        }
        if metadata_matches:
            result["phase_timings_ms"] = persisted_timings
            result["duration_ms"] = persisted_duration
        if effective_state != "ready":
            result["warnings"] = [f"code_graph_{effective_state}"]
            if not metadata_matches:
                result["warnings"].append("metadata_reconstructed")
            result["hint"] = _INDEX_HINT
        elif not metadata_matches:
            result["warnings"] = [
                "metadata_reconstructed",
                "metrics_incomplete",
            ]
        return self._with_rebuilding_state(result)

    def export_snapshot(self) -> tuple[SnapshotHeader, dict] | dict[str, object]:
        """Return the canonical rows and header of the local ready snapshot."""
        unavailable = self._unavailable()
        if unavailable is not None:
            return unavailable
        assert self._store is not None
        try:
            rows = {
                kind: list(self._store.stable_rows(kind))
                for kind in ("repositories", "files", "symbols", "relations")
            }
        except CodeGraphStoreError:
            return self._store_failure_status()
        repository = next(
            (
                row
                for row in rows["repositories"]
                if row.get("repository_id") == self.binding.primary
            ),
            None,
        )
        if repository is None or repository.get("state") != "ready":
            return _not_configured()
        header = SnapshotHeader(
            protocol_version=1,
            schema_version=SCHEMA_VERSION,
            repository_id=str(repository["repository_id"]),
            source_fingerprint=str(repository["source_fingerprint"]),
            parser_fingerprint=str(repository["parser_fingerprint"]),
            normalizer_version=str(repository["normalizer_version"]),
            unicode_data_version=str(repository["unicode_data_version"]),
            languages=tuple(sorted({row["language"] for row in rows["files"]})),
            expected_counts={kind: len(rows[kind]) for kind in rows},
            graph_payload_revision=graph_payload_revision(rows),
        )
        return header, rows

    def status(self) -> dict[str, object]:
        """Read metadata and compatible schema only; never discover or parse.

        The answer describes the graph and nothing else. The build job that
        may be running behind it is attached above, by `wiki_code_status`
        (`attach_job`), so every `read_mode` reports it alike and no branch
        that merges an error onto a status can carry a job with it.
        """
        unavailable = self._unavailable()
        if unavailable is not None:
            return {
                **unavailable,
                "enabled": unavailable.get("code") != "not_configured",
            }
        assert self.paths is not None and self._store is not None
        normalization_versions = self._normalization_versions()
        for _attempt in range(4):
            before = dict(_metadata(self.paths.metadata))
            try:
                with code_graph_read_lock(self.paths.lock):
                    locked_metadata = dict(_metadata(self.paths.metadata))
                    if before != locked_metadata:
                        continue
                    status = self._read_status(
                        persisted_metadata=locked_metadata,
                        normalization_versions=normalization_versions,
                    )
                    after = dict(_metadata(self.paths.metadata))
            except Timeout:
                return self._shared_rebuilding_status(
                    before, normalization_versions
                )
            except CodeGraphStoreError:
                return self._store_failure_status()
            if locked_metadata != after:
                continue
            if "error" in status:
                return status
            metadata_state = after.get("state")
            if metadata_state in {"rebuilding", "recovering"} or (
                _pending_final_verify(after)
            ):
                if self._local_build_active():
                    return self._with_rebuilding_state(
                        status, shared_writer=True
                    )
                try:
                    recovered = self._recover_stale_metadata(after)
                except CodeGraphStoreError:
                    return self._store_failure_status()
                if recovered:
                    continue
                return self._with_rebuilding_state(
                    status, shared_writer=True
                )
            if metadata_state == "failed":
                failed = {
                    **status,
                    "state": "failed",
                    "fresh": False,
                    "warnings": [
                        "code_graph_failed",
                        "metrics_incomplete",
                    ],
                    "hint": _INDEX_HINT,
                }
                failed.pop("duration_ms", None)
                failed.pop("phase_timings_ms", None)
                return failed
            metadata_revision = after.get("revision")
            if (
                metadata_state == "ready"
                and metadata_revision is not None
                and status.get("revision") != metadata_revision
            ):
                continue
            return status
        metadata = dict(_metadata(self.paths.metadata))
        try:
            with code_graph_read_lock(self.paths.lock):
                current = dict(_metadata(self.paths.metadata))
                status = self._read_status(
                    persisted_metadata=current,
                    normalization_versions=normalization_versions,
                )
        except Timeout:
            return self._shared_rebuilding_status(
                metadata, normalization_versions
            )
        except CodeGraphStoreError:
            return self._store_failure_status()
        metadata = current
        if (
            metadata.get("state") in {"rebuilding", "recovering"}
            or _pending_final_verify(metadata)
        ):
            if self._local_build_active():
                return self._with_rebuilding_state(
                    status, shared_writer=True
                )
            try:
                recovered = self._recover_stale_metadata(metadata)
            except CodeGraphStoreError:
                return self._store_failure_status()
            if recovered:
                return self._read_status(
                    persisted_metadata=_metadata(self.paths.metadata),
                    normalization_versions=normalization_versions,
                )
            return self._with_rebuilding_state(
                status, shared_writer=True
            )
        return status

    def _shared_rebuilding_status(
        self,
        metadata: Mapping[str, object],
        normalization_versions: tuple[str, str] | None = None,
    ) -> dict[str, object]:
        revision = metadata.get("revision")
        status = self._missing_status(normalization_versions)
        status["revision"] = revision if isinstance(revision, str) else None
        return self._with_rebuilding_state(status, shared_writer=True)

    def _local_build_active(self) -> bool:
        """Report whether this process still owns a live publication worker."""
        return _BUILD_WORKERS.is_active(self._worker_domain_key)

    def _recover_stale_metadata(
        self,
        expected: Mapping[str, object],
    ) -> bool:
        """Replace crash-stale metadata from SQL while owning writer lock."""
        assert self.paths is not None and self._store is not None
        try:
            recovery = code_graph_write_lock(self.paths.lock, timeout=0)
            recovery.__enter__()
        except Timeout:
            return False
        try:
            current = dict(_metadata(self.paths.metadata))
            if (
                current != expected
                or (
                    current.get("state") not in {"rebuilding", "recovering"}
                    and not _pending_final_verify(current)
                )
            ):
                return True
            normalization_versions = self._normalization_versions()
            authoritative = self._read_status(
                persisted_metadata={},
                normalization_versions=normalization_versions,
            )
            authoritative_state = authoritative.get("state")
            generation = current.get("generation")
            if type(generation) is not int or generation < 0:
                generation = 0
            revision = authoritative.get("revision")
            safe_revision = revision if isinstance(revision, str) else None
            metadata_revision = current.get("revision")
            previous_revision = current.get("previous_revision")
            publication_phase = current.get("publication_phase")
            prior_state = current.get("prior_state")
            recovery_policy = current.get("recovery_policy")
            if _pending_final_verify(current):
                state = "failed"
            elif publication_phase == "provisional":
                state = "failed"
            elif publication_phase == "building":
                state = (
                    prior_state
                    if recovery_policy == "restore_prior"
                    and previous_revision == safe_revision
                    and prior_state in {
                        "missing", "ready", "dirty", "failed"
                    }
                    else "failed"
                )
            else:
                state = (
                    authoritative_state
                    if metadata_revision == safe_revision
                    and authoritative_state in {"missing", "ready", "failed"}
                    else "failed"
                )
            recovered = {
                key: value
                for key, value in authoritative.items()
                if key not in {
                    "error",
                    "code",
                    "hint",
                    "duration_ms",
                    "phase_timings_ms",
                }
            }
            recovered.update({
                "state": state,
                "generation": generation,
                "revision": safe_revision,
                "fresh": state == "ready",
                "warnings": (
                    ["metadata_reconstructed", "metrics_incomplete"]
                    if state == "ready"
                    else [f"code_graph_{state}"]
                ),
            })
            staging = self._store.prepare_metadata(
                self.paths.metadata, recovered
            )
            try:
                self._store.publish_metadata(
                    self.paths.metadata, staging
                )
            except Exception:
                self._store.discard_metadata(staging)
                return False
            return True
        finally:
            recovery.__exit__(None, None, None)

    @property
    def active_workers(self) -> int:
        return _BUILD_WORKERS.active_count

    def join_workers(self, timeout: float | None = None) -> None:
        _BUILD_WORKERS.join(timeout)

    @staticmethod
    def _busy_response() -> dict[str, object]:
        return {
            "error": "code graph is busy",
            "code": "busy",
            "hint": "retry wiki_code_index",
        }

    def _index_with_deadline(
        self,
        *,
        force: bool,
        languages: list[str] | None,
        build_deadline: float,
        wait_deadline: float,
        restore_prior_on_abort: bool,
        cancel_on_wait: bool,
        publish: Callable[[], dict[str, object]] | None = None,
    ) -> dict[str, object]:
        assert self._indexer is not None
        if time.monotonic() >= build_deadline:
            return self._busy_response()

        def run_build(
            control: BuildControl,
            result: dict[str, object],
        ) -> None:
            try:
                built = self._indexer.build(
                    force=force,
                    languages=languages,
                    deadline=build_deadline,
                    restore_prior_on_abort=restore_prior_on_abort,
                    control=control,
                )
                counts = built.get("counts", {})
                file_count = (
                    counts.get("files", 0)
                    if isinstance(counts, dict)
                    else 0
                )
                LOGGER.info(
                    "code_graph_build code=ready files=%d duration_ms=%d",
                    file_count,
                    built.get("duration_ms", 0),
                )
                result.update(built)
            except Timeout:
                LOGGER.info("code_graph_build code=busy")
                result.update(self._busy_response())
            except CodeGraphError as exc:
                failure = _typed_failure(exc)
                LOGGER.error(
                    "code_graph_build code=%s", failure["code"]
                )
                result.update(failure)
            except Exception:
                LOGGER.error("code_graph_build code=rebuild_failed")
                result.update(_rebuild_failed())
            # The build publishes its own snapshot, on this thread, before it
            # reports terminality: a caller that detached at its wait expiry
            # is not there to publish for it, and leaving the publication to
            # whoever calls next would keep the remote graph stale for an
            # unbounded time. A build that never reached `ready` -- cancelled,
            # timed out, or failed -- publishes nothing.
            if publish is None or result.get("state") != "ready":
                return
            # The graph is written and complete from here on; what remains
            # happens against a remote target. Local reads must stop being
            # refused at this point rather than at thread death, or a batched
            # remote publication reports a working local graph as
            # `rebuilding` for its entire duration.
            control.indexing = False
            try:
                published = publish()
            except Exception:
                # Nobody is watching this thread, so the failure has to be
                # recorded rather than raised, and in the same redacted style
                # every other worker failure uses: the exception's text never
                # reaches a log line or a tool answer.
                LOGGER.error("code_graph_publish code=publication_failed")
                published = dict(_PUBLICATION_FAILED)
            # Recorded unconditionally: a caller installs this callback only
            # when it has a target to publish to, so whatever came back is the
            # publication's outcome. A mode with nothing to publish passes no
            # callback at all and leaves the key absent, which is what lets
            # `_terminal_state` tell "did not publish" from "published badly".
            result["publication"] = published
            if _terminal_state(result) != "ready":
                # The third signal, beside the `failed` job and the
                # publication itself. This answer's own `state` still reports
                # the local snapshot, which really is ready, so without a
                # warning a caller reading `state` alone is told the build
                # succeeded while the published graph is still the previous
                # revision -- the very silence this whole path exists to end.
                # No client should have to cross-reference two fields to learn
                # that what it asked for did not happen.
                result.update(_warned(result, "publication_failed"))
        try:
            job, started = _BUILD_WORKERS.start(
                self._worker_domain_key,
                run_build,
                force=force,
                languages=languages,
                explicit=not cancel_on_wait,
                build_deadline=build_deadline,
            )
        except Exception:
            return _rebuild_failed()
        if job is None or job.thread is None:
            return self._busy_response()
        job.thread.join(max(0.0, wait_deadline - time.monotonic()))
        if job.thread.is_alive():
            if cancel_on_wait:
                # Cancellation on wait expiry is a starter-only privilege: a
                # caller that only joined someone else's job never asked for
                # this work and must not cut it short out from under whoever
                # did. A joined caller still answers busy -- it just leaves
                # the build it does not own running.
                if started:
                    job.control.cancel()
                LOGGER.info("code_graph_build code=busy")
                return self._busy_response()
            if (
                time.monotonic() >= job.build_deadline
                and not job.control.publication_attempted.is_set()
            ):
                # `enter_publication` refuses once the build deadline has
                # passed, so a build that has not even reached the gate can no
                # longer publish anything: there is no job worth polling. The
                # caller keeps today's `busy`, and the doomed worker unwinds on
                # its own instead of being cancelled. Reading `attempted`
                # rather than `entered` keeps the observation monotone: the
                # flag is set before the gate decides, so a build that goes on
                # to publish is never mistaken for a doomed one.
                #
                # The deadline read here is the *running job's*, not this
                # caller's: a caller that joined someone else's build carries
                # its own, later budget, and judging by that would hand back a
                # descriptor for a build already past the publication gate's
                # cutoff -- a job that can only ever end busy or failed.
                LOGGER.info("code_graph_build code=busy")
                return self._busy_response()
            LOGGER.info("code_graph_build code=rebuilding job=%s", job.job_id)
            return _rebuilding_job_answer(job)
        ready = job.result.get("state") == "ready"
        # The worker recorded the terminal state as it ended; this idempotent
        # call only ever repeats that fact -- through the same decision, so a
        # build whose publication failed is `failed` here too, however the
        # index answer below describes the snapshot it did produce.
        _BUILD_WORKERS.finish(job, _terminal_state(job.result))
        if not ready:
            # A build that ended without a report answers with today's error
            # shape, field for field: those dicts are a pinned contract.
            return job.result or _rebuild_failed()
        answer = dict(job.result)
        answer["job"] = job.describe()
        return answer

    def index(
        self,
        *,
        force: bool = False,
        languages: list[str] | None = None,
        wait_seconds: float | None = None,
        publish: Callable[[], dict[str, object]] | None = None,
    ) -> dict[str, object]:
        """Build the graph, publishing the snapshot the build produced.

        `publish` is the caller's publication step, run by the build worker
        after a `ready` build and before the job reports terminality. Its
        answer is returned under `publication` -- the one place a caller reads
        it from, whether the build finished within `wait_seconds` or long
        after the caller detached. A caller with nothing to publish to passes
        no callback, and the key stays absent.
        """
        if languages is not None and (
            not languages
            or any(language not in KNOWN_LANGUAGES for language in languages)
        ):
            return _invalid_config()
        unavailable = self._unavailable()
        if unavailable is not None:
            return unavailable
        assert self._indexer is not None and self.config is not None
        full_rebuild_seconds = (
            self.config.max_full_rebuild_seconds
            or self.config.max_rebuild_seconds
        )
        if wait_seconds is not None and (
            wait_seconds < 0 or wait_seconds > full_rebuild_seconds
        ):
            raise CodeGraphQueryError(
                "wait_seconds must be between 0 and "
                f"{full_rebuild_seconds}",
                parameter="wait_seconds",
            )
        started = time.monotonic()
        build_deadline = started + full_rebuild_seconds
        wait_budget = (
            full_rebuild_seconds
            if wait_seconds is None
            else max(float(wait_seconds), _INDEX_GRACE_SECONDS)
        )
        return self._index_with_deadline(
            force=force,
            languages=languages,
            build_deadline=build_deadline,
            wait_deadline=started + wait_budget,
            restore_prior_on_abort=False,
            cancel_on_wait=False,
            publish=publish,
        )

    def query_guard(
        self,
        *,
        remaining_seconds: float | None = None,
    ) -> dict[str, object]:
        """Prevent non-ready callers from observing rows from an old snapshot."""
        status = self.status()
        if "error" in status:
            return {**status, "fresh": False, "results": []}
        if status.get("state") == "ready":
            assert self.config is not None and self._indexer is not None
            freshness_budget = (
                self.config.max_rebuild_seconds
                if remaining_seconds is None
                else remaining_seconds
            )
            freshness_deadline = time.monotonic() + max(
                0.0,
                min(freshness_budget, self.config.max_rebuild_seconds),
            )
            freshness_started = time.monotonic()
            try:
                became_dirty = self._indexer.mark_dirty_if_stale(
                    deadline=freshness_deadline,
                )
            except Timeout:
                return {
                    **status,
                    "error": "code graph is busy",
                    "code": "busy",
                    "fresh": False,
                    "results": [],
                    "hint": "retry wiki_code_index",
                }
            except CodeGraphError as exc:
                return {
                    **status,
                    **_typed_failure(exc),
                    "fresh": False,
                    "results": [],
                }
            except Exception:
                duration_ms = max(
                    0,
                    int((time.monotonic() - freshness_started) * 1000),
                )
                LOGGER.error(
                    "code_graph_query_guard code=rebuild_failed "
                    "count=1 duration_ms=%d",
                    duration_ms,
                )
                return {
                    **status,
                    **_rebuild_failed(),
                    "fresh": False,
                    "results": [],
                }
            if became_dirty:
                current = self.status()
                if current.get("state") == "ready":
                    return {**current, "results": []}
                if current.get("state") != "dirty":
                    return _not_ready({
                        **current,
                        "fresh": False,
                        "results": [],
                    })
                return {
                    **current,
                    **_typed_failure(CodeGraphStaleError()),
                    "fresh": False,
                    "results": [],
                }
            return {**self.status(), "results": []}
        config = self.config
        state = status.get("state")
        budget = (
            config.max_rebuild_seconds
            if config is not None and remaining_seconds is None
            else remaining_seconds
        )
        if (
            config is not None
            and config.auto_rebuild == "bounded"
            and state in ("missing", "dirty")
            and budget is not None
            and budget >= config.max_rebuild_seconds
        ):
            deadline = time.monotonic() + min(
                budget, config.max_rebuild_seconds
            )
            rebuilt = self._index_with_deadline(
                force=False,
                languages=None,
                build_deadline=deadline,
                wait_deadline=deadline,
                restore_prior_on_abort=True,
                cancel_on_wait=True,
            )
            if rebuilt.get("state") == "ready":
                return {**self.status(), "results": []}
            status = self.status()
        return _not_ready({
            **status,
            "fresh": False,
            "results": [],
        })

    def search(
        self,
        query: str,
        *,
        kinds: list[str] | None = None,
        path: str | None = None,
        languages: list[str] | None = None,
        limit: int = 20,
    ) -> dict[str, object]:
        """Search only one revision proven ready under the shared reader lock."""
        try:
            configured_languages = (
                self.config.languages if self.config is not None else ("python",)
            )
            request = validate_search_request(
                query,
                kinds=kinds,
                path=path,
                languages=languages,
                configured_languages=configured_languages,
                limit=limit,
            )
        except CodeGraphQueryError:
            return _invalid_config()
        guarded = self.query_guard()
        if guarded.get("fresh") is not True:
            return guarded
        assert self.paths is not None and self._store is not None
        guarded_revision = guarded.get("revision")
        query_started = time.monotonic()
        try:
            query_engine = CodeGraphQuery(self.binding.primary or "")
            with code_graph_read_lock(self.paths.lock):
                before = dict(_metadata(self.paths.metadata))
                if (
                    _BUILD_WORKERS.is_indexing(self._worker_domain_key)
                    or not exact_ready_metadata(before)
                    or before.get("domain") != self.binding.primary
                    or before.get("state") != "ready"
                    or before.get("revision") != guarded_revision
                ):
                    return _not_ready({
                        **self._with_rebuilding_state(
                            guarded,
                            shared_writer=_BUILD_WORKERS.is_indexing(
                                self._worker_domain_key
                            ),
                        ),
                        "fresh": False,
                        "results": [],
                    })
                with self._store.read_lease() as connection:
                    data_version = connection.execute(
                        "PRAGMA data_version"
                    ).fetchone()
                    repository = connection.execute(
                        "SELECT state, revision FROM repositories "
                        "WHERE repository_id = ?",
                        (self.binding.primary,),
                    ).fetchone()
                    if repository != ("ready", guarded_revision):
                        return _not_ready({
                            **guarded,
                            "fresh": False,
                            "results": [],
                        })
                    sealed_stamp = before.get("storage_stamp")
                    if (
                        not isinstance(sealed_stamp, Mapping)
                        or self._store.storage_stamp() != sealed_stamp
                    ):
                        return _not_ready({
                            **guarded,
                            "fresh": False,
                            "results": [],
                        })
                    results = query_engine.search(
                        connection,
                        request,
                    )
                    repository_after = connection.execute(
                        "SELECT state, revision FROM repositories "
                        "WHERE repository_id = ?",
                        (self.binding.primary,),
                    ).fetchone()
                    data_version_after = connection.execute(
                        "PRAGMA data_version"
                    ).fetchone()
                    if (
                        self._store.storage_stamp() != sealed_stamp
                        or repository_after != repository
                        or data_version_after != data_version
                    ):
                        return _not_ready({
                            **guarded,
                            "fresh": False,
                            "results": [],
                        })
                    after = dict(_metadata(self.paths.metadata))
                    if (
                        before != after
                        or _BUILD_WORKERS.is_indexing(self._worker_domain_key)
                    ):
                        return _not_ready({
                            **guarded,
                            "fresh": False,
                            "results": [],
                        })
        except CodeGraphQueryError:
            return _invalid_config()
        except Timeout:
            return {
                **self._busy_response(),
                "fresh": False,
                "results": [],
            }
        except CodeGraphStoreError:
            return {
                **_typed_failure(CodeGraphStoreFailure()),
                "fresh": False,
                "results": [],
            }
        except CodeGraphError as exc:
            return {
                **sanitized_error(exc),
                "fresh": False,
                "results": [],
            }
        except Exception:
            duration_ms = max(
                0,
                int((time.monotonic() - query_started) * 1000),
            )
            LOGGER.error(
                "code_graph_query code=rebuild_failed count=1 duration_ms=%d",
                duration_ms,
            )
            return {
                **_rebuild_failed(),
                "fresh": False,
                "results": [],
            }
        return {
            "domain": guarded.get("domain"),
            "state": guarded.get("state"),
            "revision": guarded_revision,
            "fresh": True,
            "warnings": list(guarded.get("warnings", [])),
            "results": [asdict(item) for item in results],
        }

    def _empty_context_response(
        self,
        request: ContextRequest,
        guard: Mapping[str, object],
    ) -> dict[str, object]:
        """Return the exact empty Section 13.5 context response shape."""
        response = {
            "domain": guard.get("domain", self.binding.primary),
            "state": guard.get("state"),
            "revision": guard.get("revision"),
            "seeds": list(request.seeds),
            "nodes": [],
            "relations": [],
            "files": [],
            "wiki_pages": [],
            "limits": {
                "depth": request.depth,
                "max_nodes": request.max_nodes,
                "max_files": request.max_files,
                "max_source_bytes": request.max_source_bytes,
            },
            "truncated": False,
            "warnings": list(guard.get("warnings", [])),
            "fresh": False,
        }
        # A non-ready context answer must always name its own error/code/hint
        # (see `_not_ready`) so an empty `nodes` list is never mistaken for a
        # real, ready answer that simply matched nothing.
        error_default, code_default = _not_ready_defaults(response["state"])
        response["error"] = guard.get("error", error_default)
        response["code"] = guard.get("code", code_default)
        response["hint"] = guard.get("hint", _INDEX_HINT)
        return response

    def context(
        self,
        seeds: list[str],
        *,
        direction: str = "both",
        depth: int = 1,
        relations: list[str] | None = None,
        include_source: bool = False,
        include_wiki: bool = True,
        max_nodes: int = 50,
        max_files: int = 20,
        max_source_bytes: int = 200_000,
    ) -> dict[str, object]:
        """Hold Wiki selector generation stable while hydrating Wiki links."""
        arguments = {
            "direction": direction,
            "depth": depth,
            "relations": relations,
            "include_source": include_source,
            "include_wiki": include_wiki,
            "max_nodes": max_nodes,
            "max_files": max_files,
            "max_source_bytes": max_source_bytes,
        }
        if not include_wiki or self._indexer is None or self.config is None:
            return self._context_unleased(seeds, **arguments)
        try:
            request = validate_context_request(seeds, **arguments)
        except CodeGraphContextError as exc:
            return _invalid_config(exc.parameter)
        resolver = self._indexer.wiki_selector_resolver
        capture = getattr(resolver, "capture", None)
        verify = getattr(resolver, "verify_snapshot", None)
        if not callable(capture) or not callable(verify):
            return self._context_unleased(seeds, **arguments)
        # Prove freshness with the whole rebuild budget before taking the
        # selector lease, exactly as search does: a bounded auto-rebuild needs
        # the Wiki mutation lock that this shared lease would hold against it.
        guarded = self.query_guard()
        if guarded.get("fresh") is not True:
            return self._empty_context_response(
                request,
                {key: value for key, value in guarded.items() if key != "results"},
            )
        timeout = self.config.max_rebuild_seconds
        deadline = time.monotonic() + timeout

        def selector_control() -> None:
            if time.monotonic() >= deadline:
                raise Timeout(str(self.paths.lock if self.paths else "code graph"))

        snapshot = None
        try:
            with _wiki_read_lock(self.binding.base, timeout):
                snapshot = capture(
                    domain=self.binding.primary or "",
                    check_control=selector_control,
                    max_bytes=selector_capture_budget(
                        self.config.max_file_bytes,
                        self.config.max_total_files,
                    ),
                )
                verify(snapshot, check_control=selector_control)
                response = self._context_unleased(
                    seeds,
                    _request=request,
                    _guarded=guarded,
                    **arguments,
                )
                verify(snapshot, check_control=selector_control)
                return response
        except Timeout:
            return self._empty_context_response(
                request, {**self.status(), **self._busy_response()}
            )
        except Exception:
            return self._context_without_wiki(seeds, arguments, guarded)
        finally:
            if snapshot is not None:
                close_snapshot = getattr(resolver, "close_snapshot", None)
                if callable(close_snapshot):
                    close_snapshot(snapshot)

    def _context_without_wiki(
        self,
        seeds: list[str],
        arguments: Mapping[str, object],
        guarded: Mapping[str, object],
    ) -> dict[str, object]:
        """Answer the graph question when the Wiki selectors are unreadable."""
        response = self._context_unleased(
            seeds,
            **{**arguments, "include_wiki": False},
            _guarded=guarded,
        )
        response.pop("wiki_pages", None)
        response["warnings"] = list(dict.fromkeys([
            *response.get("warnings", []),
            "wiki_selector_unavailable",
        ]))
        return response

    def _context_unleased(
        self,
        seeds: list[str],
        *,
        direction: str = "both",
        depth: int = 1,
        relations: list[str] | None = None,
        include_source: bool = False,
        include_wiki: bool = True,
        max_nodes: int = 50,
        max_files: int = 20,
        max_source_bytes: int = 200_000,
        _request: ContextRequest | None = None,
        _guarded: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Compose context under one coherent ready-revision read lease."""
        if _request is None:
            try:
                request = validate_context_request(
                    seeds,
                    direction=direction,
                    depth=depth,
                    relations=relations,
                    include_source=include_source,
                    include_wiki=include_wiki,
                    max_nodes=max_nodes,
                    max_files=max_files,
                    max_source_bytes=max_source_bytes,
                )
            except CodeGraphContextError as exc:
                return _invalid_config(exc.parameter)
        else:
            request = _request
        guarded = self.query_guard() if _guarded is None else _guarded
        context_guarded = {
            key: value for key, value in guarded.items() if key != "results"
        }
        if guarded.get("fresh") is not True:
            return self._empty_context_response(request, context_guarded)
        assert (
            self.paths is not None
            and self._store is not None
            and self.config is not None
        )
        guarded_revision = guarded.get("revision")
        started = time.monotonic()
        try:
            engine = CodeGraphContext(
                self.binding.primary or "",
                self._context_root,
                self.config.max_file_bytes,
            )
            with code_graph_read_lock(self.paths.lock):
                before = dict(_metadata(self.paths.metadata))
                if (
                    _BUILD_WORKERS.is_indexing(self._worker_domain_key)
                    or not exact_ready_metadata(before)
                    or before.get("domain") != self.binding.primary
                    or before.get("state") != "ready"
                    or before.get("revision") != guarded_revision
                ):
                    return self._empty_context_response(
                        request,
                        self._with_rebuilding_state(
                            context_guarded,
                            shared_writer=_BUILD_WORKERS.is_indexing(
                                self._worker_domain_key
                            ),
                        ),
                    )
                with self._store.read_lease() as connection:
                    data_version = connection.execute(
                        "PRAGMA data_version"
                    ).fetchone()
                    repository = self._store.repository_state(
                        connection, self.binding.primary or ""
                    )
                    sealed_stamp = before.get("storage_stamp")
                    if (
                        repository != ("ready", guarded_revision)
                        or not isinstance(sealed_stamp, Mapping)
                        or self._store.storage_stamp() != sealed_stamp
                    ):
                        return self._empty_context_response(
                            request,
                            {**context_guarded, "hint": _INDEX_HINT},
                        )
                    response = engine.context(connection, request)
                    repository_after = self._store.repository_state(
                        connection, self.binding.primary or ""
                    )
                    data_version_after = connection.execute(
                        "PRAGMA data_version"
                    ).fetchone()
                    after = dict(_metadata(self.paths.metadata))
                    if (
                        self._store.storage_stamp() != sealed_stamp
                        or repository_after != repository
                        or data_version_after != data_version
                        or before != after
                        or _BUILD_WORKERS.is_indexing(self._worker_domain_key)
                    ):
                        return self._empty_context_response(
                            request,
                            {**context_guarded, "hint": _INDEX_HINT},
                        )
        except Timeout:
            return self._empty_context_response(
                request, {**context_guarded, **self._busy_response()}
            )
        except CodeGraphStoreError:
            return self._empty_context_response(
                request,
                {**context_guarded, **_typed_failure(CodeGraphStoreFailure())},
            )
        except CodeGraphError as exc:
            return self._empty_context_response(
                request, {**context_guarded, **sanitized_error(exc)}
            )
        except Exception:
            duration_ms = max(0, int((time.monotonic() - started) * 1000))
            LOGGER.error(
                "code_graph_context code=rebuild_failed count=1 duration_ms=%d",
                duration_ms,
            )
            return self._empty_context_response(
                request, {**context_guarded, **_rebuild_failed()}
            )
        source_unavailable = bool(
            {"source_unavailable", "source_changed"} & set(response["warnings"])
        )
        return {
            "domain": guarded.get("domain"),
            "state": guarded.get("state"),
            "revision": guarded_revision,
            "fresh": not source_unavailable,
            **response,
            "warnings": list(dict.fromkeys([
                *guarded.get("warnings", []),
                *response["warnings"],
            ])),
        }


__all__ = [
    "CodeGraphRuntime",
    "sanitized_error",
    "shutdown_code_graph_workers",
]
