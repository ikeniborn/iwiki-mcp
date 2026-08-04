"""Durable local journal primitives for cross-domain wiki mutations."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import tempfile
from typing import Callable, Iterable

from . import base as wiki_base
from . import graph, indexer, sync
from .engine.config import Config
from .engine.graph_store import GraphStore
from .lock import mutation_lock
from .sync import _head_revision, _run


_STATES = ("prepared", "applied", "committed", "finalized")


class CrossDomainError(RuntimeError):
    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code.replace("_", " "))
        self.code = code


@dataclass(frozen=True)
class FileSnapshot:
    path: str
    existed: bool
    sha256: str | None


@dataclass(frozen=True)
class TransactionManifest:
    transaction_id: str
    state: str
    base_head: str | None
    commit_head: str | None
    affected_domains: tuple[str, ...]
    files: tuple[FileSnapshot, ...]


@dataclass(frozen=True)
class PlannedEdit:
    domain: str
    file: str
    before_hash: str | None
    after: bytes | None


@dataclass(frozen=True)
class MutationPlan:
    operation: str
    transaction_id: str
    base_head: str | None
    edits: tuple[PlannedEdit, ...]
    affected_domains: tuple[str, ...]
    rewritten_pages: tuple[str, ...]
    rewritten_links: int


def _transactions_root(base: str) -> Path:
    iwiki = Path(base) / ".iwiki"
    root = iwiki / "transactions"
    if iwiki.is_symlink() or root.is_symlink():
        raise CrossDomainError("invalid_path")
    return root


def _transaction_dir(base: str, transaction_id: str) -> Path:
    if not transaction_id or any(
        character not in "0123456789abcdef" for character in transaction_id
    ):
        raise CrossDomainError("manual_recovery_required")
    directory = _transactions_root(base) / transaction_id
    if directory.is_symlink():
        raise CrossDomainError("manual_recovery_required")
    return directory


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _relative_file(path: str) -> str:
    relative = PurePosixPath(path)
    if (
        relative.is_absolute()
        or not relative.parts
        or "\\" in path
        or any(part in ("", ".", "..") for part in relative.parts)
    ):
        raise CrossDomainError("invalid_path")
    return relative.as_posix()


def _base_file(base: str, relative: str) -> Path:
    root = Path(base).resolve()
    path = root / _relative_file(relative)
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            raise CrossDomainError("invalid_path")
    try:
        path.resolve(strict=False).relative_to(root)
    except (OSError, ValueError) as exc:
        raise CrossDomainError("invalid_path") from exc
    return path


def _manifest_payload(manifest: TransactionManifest) -> bytes:
    data = asdict(manifest)
    data["affected_domains"] = list(manifest.affected_domains)
    data["files"] = [asdict(item) for item in manifest.files]
    return (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _write_manifest(base: str, manifest: TransactionManifest) -> None:
    path = _transaction_dir(base, manifest.transaction_id) / "manifest.json"
    _atomic_write(path, _manifest_payload(manifest))


def create_transaction(
    base: str,
    *,
    base_head: str | None,
    affected_domains: Iterable[str],
    files: Iterable[str],
    transaction_id: str | None = None,
) -> TransactionManifest:
    """Snapshot every mutable path and persist a prepared journal."""
    transaction_id = transaction_id or secrets.token_hex(16)
    transaction_root = _transactions_root(base)
    transaction_root.mkdir(parents=True, exist_ok=True)
    transaction_dir = _transaction_dir(base, transaction_id)
    transaction_dir.mkdir()
    snapshots = transaction_dir / "snapshots"
    snapshots.mkdir()
    _fsync_directory(transaction_root)
    try:
        records: list[FileSnapshot] = []
        for index, relative in enumerate(sorted(set(map(_relative_file, files)))):
            path = _base_file(base, relative)
            if path.exists():
                if not path.is_file():
                    raise CrossDomainError("invalid_path")
                payload = path.read_bytes()
                _atomic_write(snapshots / f"{index:04d}.bin", payload)
                records.append(
                    FileSnapshot(relative, True, sha256(payload).hexdigest())
                )
            else:
                records.append(FileSnapshot(relative, False, None))
        domains = tuple(sorted(set(affected_domains)))
        if any(
            not domain
            or domain.startswith(".")
            or "/" in domain
            or "\\" in domain
            for domain in domains
        ):
            raise CrossDomainError("invalid_path")
        manifest = TransactionManifest(
            transaction_id,
            "prepared",
            base_head,
            None,
            domains,
            tuple(records),
        )
        _write_manifest(base, manifest)
        return manifest
    except BaseException:
        shutil.rmtree(transaction_dir, ignore_errors=True)
        raise


def transition_transaction(
    base: str,
    manifest: TransactionManifest,
    state: str,
    *,
    commit_head: str | None = None,
) -> TransactionManifest:
    """Persist one forward journal state transition."""
    try:
        current_index = _STATES.index(manifest.state)
        next_index = _STATES.index(state)
    except ValueError as exc:
        raise CrossDomainError("mutation_failed") from exc
    if next_index != current_index + 1:
        raise CrossDomainError("mutation_failed")
    updated = replace(
        manifest,
        state=state,
        commit_head=(commit_head if state == "committed" else manifest.commit_head),
    )
    _write_manifest(base, updated)
    return updated


def _remove_transaction(base: str, transaction_id: str) -> None:
    directory = _transaction_dir(base, transaction_id)
    if directory.exists():
        shutil.rmtree(directory)
    root = _transactions_root(base)
    if root.exists() and not any(root.iterdir()):
        root.rmdir()
        _fsync_directory(root.parent)
    elif root.exists():
        _fsync_directory(root)


def finalize_transaction(base: str, manifest: TransactionManifest) -> None:
    """Persist finalized state, then remove its durable transaction directory."""
    if manifest.state != "finalized":
        if manifest.state != "committed":
            raise CrossDomainError("mutation_failed")
        manifest = transition_transaction(base, manifest, "finalized")
    _remove_transaction(base, manifest.transaction_id)


def _load_manifest(base: str, directory: Path) -> TransactionManifest:
    try:
        data = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        manifest = TransactionManifest(
            transaction_id=str(data["transaction_id"]),
            state=str(data["state"]),
            base_head=data.get("base_head"),
            commit_head=data.get("commit_head"),
            affected_domains=tuple(data["affected_domains"]),
            files=tuple(FileSnapshot(**item) for item in data["files"]),
        )
        if manifest.transaction_id != directory.name or manifest.state not in _STATES:
            raise ValueError
        _transaction_dir(base, manifest.transaction_id)
        for item in manifest.files:
            _base_file(base, item.path)
        return manifest
    except CrossDomainError as exc:
        raise CrossDomainError("manual_recovery_required") from exc
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise CrossDomainError("manual_recovery_required") from exc


def _head_has_transaction(base: str, transaction_id: str) -> bool:
    result = _run(base, "log", "-1", "--format=%B")
    if result.returncode != 0:
        return False
    trailer = f"Iwiki-Transaction: {transaction_id}"
    return trailer in result.stdout.splitlines()


def _restore_transaction(base: str, manifest: TransactionManifest) -> None:
    directory = _transaction_dir(base, manifest.transaction_id)
    for index, item in enumerate(manifest.files):
        path = _base_file(base, item.path)
        if item.existed:
            try:
                payload = (directory / "snapshots" / f"{index:04d}.bin").read_bytes()
            except OSError as exc:
                raise CrossDomainError("manual_recovery_required") from exc
            if sha256(payload).hexdigest() != item.sha256:
                raise CrossDomainError("manual_recovery_required")
            _atomic_write(path, payload)
        elif path.exists():
            if not path.is_file() or path.is_symlink():
                raise CrossDomainError("manual_recovery_required")
            path.unlink()
            _fsync_directory(path.parent)
    for item in manifest.files:
        path = _base_file(base, item.path)
        if item.existed:
            if not path.is_file() or sha256(path.read_bytes()).hexdigest() != item.sha256:
                raise CrossDomainError("manual_recovery_required")
        elif path.exists():
            raise CrossDomainError("manual_recovery_required")
    _remove_transaction(base, manifest.transaction_id)


def recover_pending_transactions(
    base: str,
    *,
    finalize_committed: Callable[[TransactionManifest], bool],
) -> None:
    """Recover every journal or stop without mutation on ambiguous state."""
    try:
        root = _transactions_root(base)
    except CrossDomainError as exc:
        raise CrossDomainError("manual_recovery_required") from exc
    if not root.is_dir():
        return
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        manifest = _load_manifest(base, directory)
        if manifest.state == "finalized":
            _remove_transaction(base, manifest.transaction_id)
            continue
        head = _head_revision(base)
        trailer_matches = _head_has_transaction(base, manifest.transaction_id)
        if (
            manifest.state == "committed"
            and head != manifest.commit_head
            and not trailer_matches
        ):
            raise CrossDomainError("manual_recovery_required")
        committed = manifest.state == "committed" or trailer_matches
        if committed:
            if not finalize_committed(manifest):
                raise CrossDomainError("manual_recovery_required")
            if manifest.state != "committed":
                manifest = replace(manifest, state="committed", commit_head=head)
                _write_manifest(base, manifest)
            finalize_transaction(base, manifest)
            continue
        if head != manifest.base_head:
            raise CrossDomainError("manual_recovery_required")
        _restore_transaction(base, manifest)


def _recovery_graph_safe(base: str, manifest: TransactionManifest) -> bool:
    store = GraphStore(base)
    for domain in manifest.affected_domains:
        try:
            store.mark_domain_dirty(domain)
        except Exception:
            pass
    return graph.incoming_candidates(
        base, manifest.affected_domains, "recovery/probe"
    ) is None


def _validate_plan(base: str, binding, plan: MutationPlan) -> tuple[str, ...]:
    if Path(base).resolve() != Path(binding.base).resolve():
        raise CrossDomainError("mutation_failed")
    domains = tuple(sorted(set(plan.affected_domains)))
    if any(domain not in binding.read for domain in domains):
        raise CrossDomainError("write_scope_blocked")
    if any(domain not in wiki_base.writable_domains(binding) for domain in domains):
        raise CrossDomainError("write_scope_blocked")
    if _head_revision(base) != plan.base_head:
        raise CrossDomainError("source_changed")
    for edit in plan.edits:
        if edit.domain not in domains:
            raise CrossDomainError("mutation_failed")
        path = _base_file(base, f"{edit.domain}/{_relative_file(edit.file)}")
        if edit.before_hash is None:
            if path.exists():
                raise CrossDomainError("target_collision")
        else:
            try:
                current = sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                raise CrossDomainError("source_changed") from exc
            if current != edit.before_hash:
                raise CrossDomainError("source_changed")
    return domains


def _affected_files(plan: MutationPlan, domains: tuple[str, ...]) -> tuple[str, ...]:
    paths = {f"{edit.domain}/{_relative_file(edit.file)}" for edit in plan.edits}
    for domain in domains:
        paths.add(f"{domain}/index.jsonl")
        paths.add(f"{domain}/log.jsonl")
    return tuple(sorted(paths))


def _apply_edit(base: str, edit: PlannedEdit) -> None:
    path = _base_file(base, f"{edit.domain}/{_relative_file(edit.file)}")
    if edit.after is None:
        if path.exists():
            path.unlink()
            _fsync_directory(path.parent)
        return
    _atomic_write(path, edit.after)


def execute_plan(base: str, binding, plan: MutationPlan) -> dict:
    """Execute one immutable multi-domain mutation under a durable journal."""
    domains = _validate_plan(base, binding, plan)
    paths = _affected_files(plan, domains)
    committed = False
    manifest: TransactionManifest | None = None
    graph_warning: str | None = None
    with mutation_lock(base):
        recover_pending_transactions(
            base,
            finalize_committed=lambda item: _recovery_graph_safe(base, item),
        )
        if not wiki_base.ensure_graph_store_excluded(base):
            raise CrossDomainError("mutation_failed")
        ignored = _run(
            base,
            "check-ignore",
            "-q",
            ".iwiki/transactions/probe/manifest.json",
        )
        if ignored.returncode != 0:
            raise CrossDomainError("mutation_failed")
        staged = _run(base, "diff", "--cached", "--name-only", "--", *paths)
        if staged.returncode != 0 or staged.stdout.strip():
            raise CrossDomainError("mutation_failed")
        domains = _validate_plan(base, binding, plan)
        mutations = tuple(
            indexer.prepare_graph_mutation(base, domain, lock_held=True)
            for domain in domains
        )
        try:
            manifest = create_transaction(
                base,
                base_head=plan.base_head,
                affected_domains=domains,
                files=paths,
                transaction_id=plan.transaction_id,
            )
            for edit in sorted(plan.edits, key=lambda item: (item.domain, item.file)):
                _apply_edit(base, edit)
            manifest = transition_transaction(base, manifest, "applied")
            config = Config.load()
            for domain in domains:
                indexer.index_domain(config, base, domain)
            message = (
                f"iwiki: {plan.operation}\n\n"
                f"Iwiki-Transaction: {plan.transaction_id}"
            )
            commit = sync._auto_commit_locked(base, message, paths)
            if not commit.get("committed"):
                raise RuntimeError("local commit failed")
            committed = True
            commit_head = _head_revision(base)
            manifest = transition_transaction(
                base, manifest, "committed", commit_head=commit_head
            )
            refresh_files = {
                domain: tuple(
                    sorted(
                        edit.file
                        for edit in plan.edits
                        if edit.domain == domain
                        and edit.after is not None
                        and edit.file.endswith(".md")
                        and edit.file not in indexer.RESERVED_OKF
                    )
                )
                for domain in domains
            }
            delete_files = {
                domain: tuple(
                    sorted(
                        edit.file
                        for edit in plan.edits
                        if edit.domain == domain
                        and edit.after is None
                        and edit.file.endswith(".md")
                        and edit.file not in indexer.RESERVED_OKF
                    )
                )
                for domain in domains
            }
            graph_warning = indexer.finalize_graph_batch(
                tuple(mutation for mutation in mutations if mutation is not None),
                refresh_files,
                delete_files,
            )
            finalize_transaction(base, manifest)
        except Exception as exc:
            if committed:
                raise CrossDomainError("manual_recovery_required") from exc
            try:
                recover_pending_transactions(
                    base,
                    finalize_committed=lambda item: _recovery_graph_safe(base, item),
                )
                _run(base, "reset", "-q", "HEAD", "--", *paths)
            except Exception as recovery_error:
                raise CrossDomainError("manual_recovery_required") from recovery_error
            raise CrossDomainError("mutation_failed") from exc

    pushed = sync.sync(base)
    result = {
        "transaction_id": plan.transaction_id,
        "rewritten_pages": sorted(set(plan.rewritten_pages)),
        "affected_domains": list(domains),
        "rewritten_links": plan.rewritten_links,
        "committed": True,
        "pushed": bool(pushed.get("pushed")),
        "sync_attempts": pushed.get("sync_attempts", 0),
        "push_attempts": pushed.get("push_attempts", 0),
    }
    warning = pushed.get("warning") or pushed.get("error")
    if warning:
        result["warning"] = sync._sanitize_git_output(str(warning))
    if graph_warning:
        existing = result.get("warning")
        result["warning"] = (
            f"{existing}; {graph_warning}" if existing else graph_warning
        )
    return result
