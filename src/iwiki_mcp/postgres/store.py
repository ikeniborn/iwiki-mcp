"""Tenant-scoped PostgreSQL page, vector, and link storage."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
import hashlib
import json
from typing import Any, ContextManager

import numpy as np
import psycopg
from psycopg import sql
from pgvector.psycopg import register_vector
from psycopg.types.json import Jsonb

from .. import graph, indexer, retrieval
from ..engine import frontmatter
from ..engine.config import Config
from ..engine.embed import embed_texts
from ..engine.links import parse_link_targets
from ..engine.related import related
from ..engine.store import SCHEMA_VERSION, Record, dequantize, make_record
from ..engine.validate import validate_page
from ..storage import expected_revision_required, revision_conflict
from .auth import AccessError, AuthContext


_BLOCKING_FINDINGS = {"deep_heading", "pre_h2_text"}
_PROTECTED_TABLES = (
    "domains",
    "pages",
    "chunks",
    "links",
    "code_graph_domain_state",
    "code_graph_publication_sessions",
    "code_graph_snapshots",
    "code_graph_batches",
    "code_graph_files",
    "code_graph_symbols",
    "code_graph_relations",
    "code_graph_wiki_links",
)


def _principal_shape(cursor, principal: str) -> tuple[bool, bool]:
    cursor.execute(
        "SELECT rolcanlogin, rolsuper OR rolbypassrls "
        "FROM pg_roles WHERE rolname = %s",
        (principal,),
    )
    row = cursor.fetchone()
    if row is None:
        return False, False
    cursor.execute(
        "SELECT EXISTS ("
        "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_roles r ON r.oid = c.relowner "
        "WHERE n.nspname = 'iwiki' AND c.relname = ANY(%s) "
        "AND pg_has_role(%s, r.rolname, 'MEMBER'))",
        (list(_PROTECTED_TABLES), principal),
    )
    return True, bool(not row[0] or row[1] or cursor.fetchone()[0])


def validate_direct_principal(
    dsn: str,
    *,
    iwiki_id: str | None = None,
    read_domains: tuple[str, ...] = (),
    write_domains: tuple[str, ...] = (),
) -> dict[str, str] | None:
    """Reject owner/BYPASSRLS roles and, when supplied, unmapped scope."""
    try:
        with psycopg.connect(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT session_user")
                principal = cursor.fetchone()[0]
                exists, invalid = _principal_shape(cursor, principal)
                if not exists or invalid:
                    return {"error": "invalid_config"}
                if iwiki_id is None:
                    return None
                cursor.execute(
                    "SELECT d.slug, "
                    "iwiki.database_principal_can_access(d.iwiki_id, d.domain_id, false), "
                    "iwiki.database_principal_can_access(d.iwiki_id, d.domain_id, true) "
                    "FROM iwiki.domains d WHERE d.iwiki_id = %s",
                    (iwiki_id,),
                )
                grants = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
    except psycopg.Error:
        return {"error": "invalid_config"}
    if any(not grants.get(domain, (False, False))[0] for domain in read_domains):
        return {"error": "invalid_config"}
    if any(not grants.get(domain, (False, False))[1] for domain in write_domains):
        return {"error": "invalid_config"}
    return None


def provision_runtime_grant(
    admin_dsn: str,
    *,
    principal: str,
    iwiki_id: str,
    read_domains: list[str],
    write_domains: list[str],
    runtime: str,
) -> dict[str, object]:
    """Map an existing restricted role and grant only runtime SQL privileges."""
    if runtime not in {"hosted", "direct"}:
        raise ValueError("runtime must be hosted or direct")
    read = tuple(dict.fromkeys(read_domains))
    write = tuple(dict.fromkeys(write_domains))
    if not read:
        raise ValueError("read grant is required")
    if any(domain not in read for domain in write):
        raise ValueError("write grant must also be readable")
    with psycopg.connect(admin_dsn) as connection:
        with connection.cursor() as cursor:
            exists, invalid = _principal_shape(cursor, principal)
            if not exists or invalid:
                raise ValueError("invalid runtime principal")
            cursor.execute(
                "SELECT slug, domain_id FROM iwiki.domains "
                "WHERE iwiki_id = %s AND slug = ANY(%s)",
                (iwiki_id, list(read)),
            )
            domain_ids = {row[0]: row[1] for row in cursor.fetchall()}
            if set(domain_ids) != set(read):
                raise ValueError("domain grant does not exist")
            cursor.execute(
                "DELETE FROM iwiki.database_principal_domain_grants "
                "WHERE principal = %s AND iwiki_id = %s",
                (principal, iwiki_id),
            )
            for domain in read:
                cursor.execute(
                    "INSERT INTO iwiki.database_principal_domain_grants "
                    "(principal, iwiki_id, domain_id, runtime, can_read, can_write) "
                    "VALUES (%s, %s, %s, %s, true, %s)",
                    (principal, iwiki_id, domain_ids[domain], runtime, domain in write),
                )
            role = sql.Identifier(principal)
            cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA iwiki TO {}").format(role))
            cursor.execute(
                sql.SQL(
                    "GRANT SELECT ON iwiki.schema_migrations, iwiki.iwikis, "
                    "iwiki.tokens, iwiki.token_domain_grants, "
                    "iwiki.token_domain_management_grants, iwiki.domains, "
                    "iwiki.pages, iwiki.chunks, iwiki.links, "
                    "iwiki.code_graph_domain_state, "
                    "iwiki.code_graph_publication_sessions, "
                    "iwiki.code_graph_snapshots, iwiki.code_graph_batches, "
                    "iwiki.code_graph_files, iwiki.code_graph_symbols, "
                    "iwiki.code_graph_relations, iwiki.code_graph_wiki_links TO {}"
                ).format(role)
            )
            cursor.execute(
                sql.SQL("GRANT UPDATE (last_used_at) ON iwiki.tokens TO {}").format(
                    role
                )
            )
            cursor.execute(
                sql.SQL(
                    "GRANT UPDATE (markdown_generation) ON iwiki.domains TO {}"
                ).format(role)
            )
            cursor.execute(
                sql.SQL(
                    "GRANT INSERT, UPDATE, DELETE ON iwiki.pages, iwiki.chunks, "
                    "iwiki.links, iwiki.token_domain_grants, "
                    "iwiki.token_domain_management_grants, "
                    "iwiki.code_graph_domain_state, "
                    "iwiki.code_graph_publication_sessions, "
                    "iwiki.code_graph_snapshots, iwiki.code_graph_batches, "
                    "iwiki.code_graph_files, iwiki.code_graph_symbols, "
                    "iwiki.code_graph_relations, iwiki.code_graph_wiki_links TO {}"
                ).format(role)
            )
            cursor.execute(
                sql.SQL(
                    "GRANT USAGE ON SEQUENCE iwiki.pages_page_id_seq, "
                    "iwiki.chunks_chunk_id_seq, iwiki.links_link_id_seq, "
                    "iwiki.code_graph_domain_lock_id_seq TO {}"
                ).format(role)
            )
            cursor.execute(
                sql.SQL(
                    "GRANT EXECUTE ON FUNCTION "
                    "iwiki.database_principal_can_access(text, bigint, boolean), "
                    "iwiki.database_principal_runtime_domains(text), "
                    "iwiki.create_domain_for_principal(text, text) TO {}"
                ).format(role)
            )
    return {
        "principal": principal,
        "iwiki": iwiki_id,
        "runtime": runtime,
        "read_domains": list(read),
        "write_domains": list(write),
    }


def inspect_runtime_principal(admin_dsn: str, principal: str) -> dict[str, object]:
    """Return safe role shape and shared domain mappings for operators."""
    with psycopg.connect(admin_dsn) as connection:
        with connection.cursor() as cursor:
            exists, invalid = _principal_shape(cursor, principal)
            if not exists:
                raise ValueError("runtime principal does not exist")
            cursor.execute(
                "SELECT g.iwiki_id, d.slug, g.runtime, g.can_read, g.can_write "
                "FROM iwiki.database_principal_domain_grants g "
                "JOIN iwiki.domains d USING (iwiki_id, domain_id) "
                "WHERE g.principal = %s ORDER BY g.iwiki_id, d.slug",
                (principal,),
            )
            grants = [
                {
                    "iwiki": row[0],
                    "domain": row[1],
                    "runtime": row[2],
                    "can_read": row[3],
                    "can_write": row[4],
                }
                for row in cursor.fetchall()
            ]
    return {"principal": principal, "valid_runtime_shape": not invalid, "grants": grants}


def require_hosted_principal(
    admin_dsn: str,
    *,
    principal: str,
    iwiki_id: str,
    read_domains: list[str],
    write_domains: list[str],
) -> None:
    """Reject token issuance unless this exact hosted role covers every domain.

    A create-only bootstrap token requests no domain, so only the restricted
    role shape is provable before the first domain exists.
    """
    with psycopg.connect(admin_dsn) as connection:
        with connection.cursor() as cursor:
            exists, invalid = _principal_shape(cursor, principal)
            if not exists or invalid:
                raise ValueError("invalid hosted principal")
            if not read_domains and not write_domains:
                return
            cursor.execute(
                "SELECT d.slug, g.can_read, g.can_write "
                "FROM iwiki.database_principal_domain_grants g "
                "JOIN iwiki.domains d USING (iwiki_id, domain_id) "
                "WHERE g.principal = %s AND g.iwiki_id = %s "
                "AND g.runtime = 'hosted'",
                (principal, iwiki_id),
            )
            grants = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
    if any(not grants.get(domain, (False, False))[0] for domain in read_domains):
        raise ValueError("hosted principal is not granted every read domain")
    if any(not grants.get(domain, (False, False))[1] for domain in write_domains):
        raise ValueError("hosted principal is not granted every write domain")


def require_hosted_runtime_principal(dsn: str) -> None:
    """Require the connected hosted role to be restricted and provisioned."""
    try:
        with psycopg.connect(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT session_user")
                principal = cursor.fetchone()[0]
                exists, invalid = _principal_shape(cursor, principal)
                if not exists or invalid:
                    raise ValueError("invalid hosted principal")
                cursor.execute(
                    "SELECT iwiki.database_principal_runtime_domains(%s)",
                    ("hosted",),
                )
                granted = cursor.fetchone()[0]
    except psycopg.Error as exc:
        raise ValueError("invalid hosted principal") from exc
    if not granted:
        raise ValueError("hosted principal is not provisioned for any domain")


def _validate_identifier(value: str, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\\" in value
        or value.startswith("/")
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError(f"invalid {name}")
    return value


class PostgresStore:
    """Direct SQL backend isolated by one immutable ``iwiki_id``."""

    def __init__(
        self,
        dsn: str,
        iwiki_id: str,
        cfg: Config,
        *,
        embedder: Callable = embed_texts,
        auth_context: AuthContext | None = None,
        connection_factory: Callable[[], ContextManager[Any]] | None = None,
        require_database_principal: bool = False,
    ) -> None:
        self._dsn = dsn
        self.iwiki_id = _validate_identifier(iwiki_id, "iwiki id")
        if auth_context is not None and auth_context.iwiki_id != self.iwiki_id:
            raise AccessError(403)
        self.cfg = cfg
        self._embedder = embedder
        self._auth_context = auth_context
        self._connection_factory = connection_factory or (
            lambda: psycopg.connect(self._dsn)
        )
        self._require_database_principal = require_database_principal
        if require_database_principal:
            context = auth_context
            if context is None or validate_direct_principal(
                dsn,
                iwiki_id=self.iwiki_id,
                read_domains=context.read_domains,
                write_domains=context.write_domains,
            ) is not None:
                raise ValueError("invalid_config")

    def with_embedder(self, embedder: Callable) -> "PostgresStore":
        return PostgresStore(
            self._dsn,
            self.iwiki_id,
            self.cfg,
            embedder=embedder,
            auth_context=self._auth_context,
            connection_factory=self._connection_factory,
            require_database_principal=self._require_database_principal,
        )

    def _require_read(self, domain: str) -> None:
        if self._auth_context is not None:
            self._auth_context.require_read(domain)

    def _require_write(self, domain: str) -> None:
        if self._auth_context is not None:
            self._auth_context.require_write(domain)

    def _require_admin(self) -> None:
        if self._auth_context is not None:
            raise AccessError(403)

    @contextmanager
    def _connect(self):
        with self._connection_factory() as connection:
            register_vector(connection)
            yield connection

    def create_wiki(self, slug: str) -> None:
        self._require_admin()
        slug = _validate_identifier(slug, "wiki slug")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO iwiki.iwikis (iwiki_id, slug) VALUES (%s, %s) "
                    "ON CONFLICT (iwiki_id) DO NOTHING",
                    (self.iwiki_id, slug),
                )

    def create_domain(self, domain: str) -> None:
        self._require_admin()
        domain = _validate_identifier(domain, "domain")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO iwiki.domains (iwiki_id, slug) VALUES (%s, %s) "
                    "ON CONFLICT (iwiki_id, slug) DO NOTHING",
                    (self.iwiki_id, domain),
                )

    def import_pages(
        self,
        pages: list[tuple[str, str, str]],
        source_fingerprint: str,
        *,
        domains: tuple[str, ...] | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Validate and atomically import one Git snapshot into an empty wiki."""
        self._require_admin()
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM iwiki.iwikis WHERE iwiki_id = %s",
                    (self.iwiki_id,),
                )
                if cursor.fetchone() is None:
                    raise ValueError("wiki not found")
                cursor.execute(
                    "SELECT counts FROM iwiki.git_imports "
                    "WHERE iwiki_id = %s AND source_fingerprint = %s",
                    (self.iwiki_id, source_fingerprint),
                )
                completed = cursor.fetchone()
        if completed is not None:
            return {
                **completed[0],
                "already_imported": True,
                "imported": False,
                "dry_run": dry_run,
                "source_fingerprint": source_fingerprint,
            }
        prepared = [self._prepare_page(*page) for page in pages]
        import_domains = sorted(
            {
                _validate_identifier(domain, "domain")
                for domain in (
                    domains
                    if domains is not None
                    else tuple(page[0] for page in prepared)
                )
            }
        )
        if any(page[0] not in import_domains for page in prepared):
            raise ValueError("page domain is absent from import domains")
        counts = {
            "domains": len(import_domains),
            "pages": len(prepared),
            "chunks": sum(len(page[4]) for page in prepared),
            "links": sum(len(page[5]) for page in prepared),
        }
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT active FROM iwiki.iwikis WHERE iwiki_id = %s FOR UPDATE",
                    (self.iwiki_id,),
                )
                if cursor.fetchone() is None:
                    raise ValueError("wiki not found")
                cursor.execute(
                    "SELECT counts FROM iwiki.git_imports "
                    "WHERE iwiki_id = %s AND source_fingerprint = %s",
                    (self.iwiki_id, source_fingerprint),
                )
                completed = cursor.fetchone()
                if completed is not None:
                    return {
                        **completed[0],
                        "already_imported": True,
                        "imported": False,
                        "dry_run": dry_run,
                        "source_fingerprint": source_fingerprint,
                    }
                cursor.execute(
                    "SELECT "
                    "(SELECT count(*) FROM iwiki.domains WHERE iwiki_id = %s), "
                    "(SELECT count(*) FROM iwiki.pages WHERE iwiki_id = %s), "
                    "(SELECT count(*) FROM iwiki.tokens WHERE iwiki_id = %s)",
                    (self.iwiki_id, self.iwiki_id, self.iwiki_id),
                )
                if any(cursor.fetchone()):
                    raise ValueError("target wiki is not empty")
                if dry_run:
                    connection.rollback()
                    return {
                        **counts,
                        "already_imported": False,
                        "imported": False,
                        "dry_run": True,
                        "source_fingerprint": source_fingerprint,
                    }
                domain_ids = {}
                for domain in import_domains:
                    cursor.execute(
                        "INSERT INTO iwiki.domains (iwiki_id, slug) "
                        "VALUES (%s, %s) RETURNING domain_id",
                        (self.iwiki_id, domain),
                    )
                    domain_ids[domain] = cursor.fetchone()[0]
                for domain, slug, markdown, chunks, records, targets in prepared:
                    cursor.execute(
                        "INSERT INTO iwiki.pages "
                        "(iwiki_id, domain_id, slug, markdown) "
                        "VALUES (%s, %s, %s, %s) RETURNING page_id",
                        (self.iwiki_id, domain_ids[domain], slug, markdown),
                    )
                    page_id = cursor.fetchone()[0]
                    self._replace_derived(
                        cursor, page_id, chunks, records, targets
                    )
                    self._bump_markdown_generation(cursor, domain)
                cursor.execute(
                    "UPDATE iwiki.links l SET target_page_id = p.page_id "
                    "FROM iwiki.pages p "
                    "JOIN iwiki.domains d ON d.iwiki_id = p.iwiki_id "
                    "AND d.domain_id = p.domain_id "
                    "WHERE l.iwiki_id = %s AND p.iwiki_id = l.iwiki_id "
                    "AND d.slug = l.target_domain AND p.slug = l.target_slug",
                    (self.iwiki_id,),
                )
                cursor.execute(
                    "INSERT INTO iwiki.git_imports "
                    "(iwiki_id, source_fingerprint, counts) VALUES (%s, %s, %s)",
                    (self.iwiki_id, source_fingerprint, Jsonb(counts)),
                )
        return {
            **counts,
            "already_imported": False,
            "imported": True,
            "dry_run": False,
            "source_fingerprint": source_fingerprint,
        }

    def export_snapshot(self) -> dict:
        """Read one consistent snapshot containing only authored wiki data."""
        self._require_admin()
        with self._connect() as connection:
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM iwiki.iwikis WHERE iwiki_id = %s",
                    (self.iwiki_id,),
                )
                if cursor.fetchone() is None:
                    raise ValueError("wiki not found")
                cursor.execute(
                    "SELECT slug FROM iwiki.domains WHERE iwiki_id = %s "
                    "ORDER BY slug",
                    (self.iwiki_id,),
                )
                domains = [row[0] for row in cursor.fetchall()]
                cursor.execute(
                    "SELECT d.slug, p.slug, p.markdown FROM iwiki.pages p "
                    "JOIN iwiki.domains d ON d.iwiki_id = p.iwiki_id "
                    "AND d.domain_id = p.domain_id "
                    "WHERE p.iwiki_id = %s ORDER BY d.slug, p.slug",
                    (self.iwiki_id,),
                )
                pages = [
                    {"domain": row[0], "slug": row[1], "markdown": row[2]}
                    for row in cursor.fetchall()
                ]
                cursor.execute(
                    "SELECT "
                    "(SELECT count(*) FROM iwiki.chunks WHERE iwiki_id = %s), "
                    "(SELECT count(*) FROM iwiki.links WHERE iwiki_id = %s)",
                    (self.iwiki_id, self.iwiki_id),
                )
                chunks, links = cursor.fetchone()
        page_hashes = {
            f"{page['domain']}/{page['slug']}.md": hashlib.sha256(
                page["markdown"].encode("utf-8")
            ).hexdigest()
            for page in pages
        }
        return {
            "iwiki": self.iwiki_id,
            "domains": domains,
            "pages": pages,
            "counts": {
                "domains": len(domains),
                "pages": len(pages),
                "chunks": chunks,
                "links": links,
            },
            "page_hashes": page_hashes,
        }

    def list_domains(self) -> list[str]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT slug FROM iwiki.domains WHERE iwiki_id = %s "
                    "ORDER BY slug",
                    (self.iwiki_id,),
                )
                domains = [row[0] for row in cursor.fetchall()]
        if self._auth_context is None:
            return domains
        return [domain for domain in domains if self._auth_context.can_read(domain)]

    def list_pages(self, domain: str) -> list[str]:
        domain = _validate_identifier(domain, "domain")
        self._require_read(domain)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT p.slug FROM iwiki.pages p "
                    "JOIN iwiki.domains d ON d.iwiki_id = p.iwiki_id "
                    "AND d.domain_id = p.domain_id "
                    "WHERE p.iwiki_id = %s AND d.slug = %s ORDER BY p.slug",
                    (self.iwiki_id, domain),
                )
                return [row[0] for row in cursor.fetchall()]

    def read_page(self, domain: str, slug: str) -> dict | None:
        domain = _validate_identifier(domain, "domain")
        self._require_read(domain)
        slug = _validate_identifier(slug, "page slug")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT p.markdown, p.revision FROM iwiki.pages p "
                    "JOIN iwiki.domains d ON d.iwiki_id = p.iwiki_id "
                    "AND d.domain_id = p.domain_id "
                    "WHERE p.iwiki_id = %s AND d.slug = %s AND p.slug = %s",
                    (self.iwiki_id, domain, slug),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return {
            "domain": domain,
            "slug": slug,
            "markdown": row[0],
            "revision": row[1],
        }

    # Incremental chunk reuse
    # -----------------------
    # A chunk row is addressed by its *slot* inside the page:
    #
    #     (kind, heading, chunk index)
    #
    # ``chunk_markdown`` numbers windows per heading across the whole page, so
    # the slot stays unique when a page repeats a heading or a long section is
    # word-split into several windows; ``kind`` only separates the frontmatter
    # summary chunk from a section that happens to carry an empty heading. The
    # slot survives a section moving up or down the page, which is exactly the
    # reuse we want.
    #
    # ``Chunk.hash`` is ``sha256(text)[:16]``, so equal hashes mean equal
    # embedded text: a slot whose stored hash still matches keeps its vector —
    # and its ``chunk_id`` — untouched, mirroring the ``prev.hash == c.hash``
    # check ``indexer.index_domain`` already does on the Git path. Every other
    # slot is replaced in place, inserted, or deleted, so a page always ends up
    # with exactly one row per chunk.
    #
    # The reuse decision is taken twice, from two independent reads: once in
    # ``_prepare_page`` (outside the write transaction, to skip embedder calls)
    # and once in ``_replace_derived`` (inside it, to drive the SQL). They can
    # only disagree under a concurrent write, and disagreeing is harmless: a
    # stored vector is reused only when its hash matches the current text, and
    # every DELETE/INSERT decision comes from the in-transaction read.

    @staticmethod
    def _chunk_slot(chunk) -> tuple:
        return (chunk.kind, chunk.heading, chunk.chunk)

    @staticmethod
    def _row_slot(section_id: str, heading: str) -> tuple:
        metadata = json.loads(section_id)
        return (metadata.get("kind", "section"), heading, metadata["chunk"])

    @staticmethod
    def _reused_record(chunk, scale: float, quantized) -> Record:
        """Rebuild a record around a stored vector, refreshing page facets."""
        return Record(
            id=chunk.id,
            file=chunk.file,
            heading=chunk.heading,
            chunk=chunk.chunk,
            hash=chunk.hash,
            dim=len(quantized),
            scale=scale,
            q=list(quantized),
            type=chunk.type,
            tags=list(chunk.tags),
            kind=chunk.kind,
            ordinal=chunk.ordinal,
            v=SCHEMA_VERSION,
        )

    def _stored_vectors(self, domain: str, slug: str) -> dict:
        """Slot to ``(hash, scale, quantized vector)`` for one page's chunks."""
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT c.section_id, c.heading, c.quantization_scale, "
                    "c.quantized_embedding FROM iwiki.chunks c "
                    "JOIN iwiki.pages p ON p.iwiki_id = c.iwiki_id "
                    "AND p.page_id = c.page_id "
                    "JOIN iwiki.domains d ON d.iwiki_id = p.iwiki_id "
                    "AND d.domain_id = p.domain_id "
                    "WHERE c.iwiki_id = %s AND d.slug = %s AND p.slug = %s",
                    (self.iwiki_id, domain, slug),
                )
                rows = cursor.fetchall()
        return {
            self._row_slot(section_id, heading): (
                json.loads(section_id)["hash"],
                float(scale),
                [int(value) for value in quantized],
            )
            for section_id, heading, scale, quantized in rows
        }

    def _prepare_page(
        self, domain: str, slug: str, markdown: str, *, reuse: bool = False
    ):
        domain = _validate_identifier(domain, "domain")
        slug = _validate_identifier(slug, "page slug")
        if not isinstance(markdown, str):
            raise ValueError("markdown must be a string")
        try:
            frontmatter.split(markdown, strict_code=True)
        except frontmatter.FrontmatterError as exc:
            raise ValueError(str(exc)) from exc
        blocking = [
            finding
            for finding in validate_page(markdown)
            if finding.get("type") in _BLOCKING_FINDINGS
        ]
        if blocking:
            raise ValueError("section structure invalid")
        file = f"{slug}.md"
        chunks = indexer.chunk_markdown(
            file,
            markdown,
            self.cfg.chunk_size,
            self.cfg.chunk_overlap,
            self.cfg.summary_max,
        )
        stored = self._stored_vectors(domain, slug) if reuse else {}
        records: list[Record] = [None] * len(chunks)
        pending = []
        for position, chunk in enumerate(chunks):
            previous = stored.get(self._chunk_slot(chunk))
            if (
                previous is not None
                and previous[0] == chunk.hash
                and len(previous[2]) == self.cfg.dimensions
            ):
                records[position] = self._reused_record(
                    chunk, previous[1], previous[2]
                )
            else:
                pending.append((position, chunk))
        if pending:
            vectors = self._embedder(
                self.cfg, [chunk.text for _position, chunk in pending]
            )
            if len(vectors) != len(pending):
                raise ValueError("embedding response count mismatch")
            if any(len(vector) != self.cfg.dimensions for vector in vectors):
                raise ValueError("embedding dimension mismatch")
            for (position, chunk), vector in zip(pending, vectors):
                records[position] = make_record(chunk, vector)
        targets = parse_link_targets(markdown, domain)
        return domain, slug, markdown, chunks, records, targets

    @staticmethod
    def _record_metadata(record: Record) -> str:
        return json.dumps(
            {
                "chunk": record.chunk,
                "hash": record.hash,
                "kind": record.kind,
                "ordinal": record.ordinal,
                "tags": record.tags,
                "type": record.type,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def _insert_chunk(
        self, cursor, page_id: int, storage_ordinal: int, chunk, record: Record
    ) -> None:
        cursor.execute(
            "INSERT INTO iwiki.chunks ("
            "iwiki_id, page_id, section_id, heading, content, ordinal, "
            "quantization_scale, quantized_embedding, embedding"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                self.iwiki_id,
                page_id,
                self._record_metadata(record),
                chunk.heading,
                chunk.text,
                storage_ordinal,
                record.scale,
                record.q,
                np.asarray(dequantize(record.scale, record.q), dtype=np.float32),
            ),
        )

    def _reindex_chunks(self, cursor, page_id: int, chunks, records) -> None:
        """Diff the page's chunk rows against the new chunks; see the slot note."""
        cursor.execute(
            "SELECT chunk_id, section_id, heading, ordinal FROM iwiki.chunks "
            "WHERE iwiki_id = %s AND page_id = %s ORDER BY chunk_id",
            (self.iwiki_id, page_id),
        )
        existing: dict[tuple, tuple] = {}
        stale: list[int] = []
        highest = -1
        for chunk_id, section_id, heading, ordinal in cursor.fetchall():
            highest = max(highest, ordinal)
            slot = self._row_slot(section_id, heading)
            if slot in existing:
                # A slot is unique per page; a duplicate can only be legacy
                # residue, so drop it rather than leave an unreachable row.
                stale.append(chunk_id)
            else:
                existing[slot] = (
                    chunk_id, json.loads(section_id)["hash"], ordinal, section_id,
                )
        kept, fresh = [], []
        for storage_ordinal, (chunk, record) in enumerate(zip(chunks, records)):
            previous = existing.pop(self._chunk_slot(chunk), None)
            if previous is None:
                fresh.append((storage_ordinal, chunk, record))
            else:
                kept.append((previous, storage_ordinal, chunk, record))
        stale.extend(previous[0] for previous in existing.values())
        if stale:
            cursor.execute(
                "DELETE FROM iwiki.chunks WHERE iwiki_id = %s AND page_id = %s "
                "AND chunk_id = ANY(%s)",
                (self.iwiki_id, page_id, stale),
            )
        # (iwiki_id, page_id, ordinal) is UNIQUE and not deferrable, so park the
        # retained rows above every final ordinal before renumbering them.
        renumber = any(
            previous[2] != storage_ordinal
            for previous, storage_ordinal, _chunk, _record in kept
        )
        if renumber:
            cursor.execute(
                "UPDATE iwiki.chunks SET ordinal = ordinal + %s "
                "WHERE iwiki_id = %s AND page_id = %s",
                (highest + len(chunks) + 1, self.iwiki_id, page_id),
            )
        for previous, storage_ordinal, chunk, record in kept:
            chunk_id, previous_hash, _ordinal, previous_metadata = previous
            metadata = self._record_metadata(record)
            if previous_hash != record.hash:
                cursor.execute(
                    "UPDATE iwiki.chunks SET section_id = %s, heading = %s, "
                    "content = %s, ordinal = %s, quantization_scale = %s, "
                    "quantized_embedding = %s, embedding = %s "
                    "WHERE iwiki_id = %s AND chunk_id = %s",
                    (
                        metadata,
                        chunk.heading,
                        chunk.text,
                        storage_ordinal,
                        record.scale,
                        record.q,
                        np.asarray(
                            dequantize(record.scale, record.q), dtype=np.float32
                        ),
                        self.iwiki_id,
                        chunk_id,
                    ),
                )
            elif renumber or previous_metadata != metadata:
                cursor.execute(
                    "UPDATE iwiki.chunks SET section_id = %s, ordinal = %s "
                    "WHERE iwiki_id = %s AND chunk_id = %s",
                    (metadata, storage_ordinal, self.iwiki_id, chunk_id),
                )
        for storage_ordinal, chunk, record in fresh:
            self._insert_chunk(cursor, page_id, storage_ordinal, chunk, record)

    def _replace_derived(
        self,
        cursor,
        page_id: int,
        chunks,
        records: list[Record],
        targets,
        *,
        reuse: bool = False,
    ) -> None:
        if reuse:
            self._reindex_chunks(cursor, page_id, chunks, records)
        else:
            cursor.execute(
                "DELETE FROM iwiki.chunks WHERE iwiki_id = %s AND page_id = %s",
                (self.iwiki_id, page_id),
            )
            for storage_ordinal, (chunk, record) in enumerate(zip(chunks, records)):
                self._insert_chunk(cursor, page_id, storage_ordinal, chunk, record)
        cursor.execute(
            "DELETE FROM iwiki.links WHERE iwiki_id = %s AND source_page_id = %s",
            (self.iwiki_id, page_id),
        )
        for target in targets:
            target_row = None
            if (
                self._auth_context is None
                or self._auth_context.can_read(target.target_domain)
            ):
                cursor.execute(
                    "SELECT p.page_id FROM iwiki.pages p "
                    "JOIN iwiki.domains d ON d.iwiki_id = p.iwiki_id "
                    "AND d.domain_id = p.domain_id "
                    "WHERE p.iwiki_id = %s AND d.slug = %s AND p.slug = %s",
                    (self.iwiki_id, target.target_domain, target.target_page),
                )
                target_row = cursor.fetchone()
            cursor.execute(
                "INSERT INTO iwiki.links ("
                "iwiki_id, source_page_id, target_page_id, target_domain, target_slug"
                ") VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (iwiki_id, source_page_id, target_domain, target_slug) "
                "DO NOTHING",
                (
                    self.iwiki_id,
                    page_id,
                    target_row[0] if target_row else None,
                    target.target_domain,
                    target.target_page,
                ),
            )

    def _bump_markdown_generation(self, cursor, domain: str) -> None:
        """Advance the domain change token inside the caller's transaction."""
        cursor.execute(
            "UPDATE iwiki.domains SET markdown_generation = "
            "markdown_generation + 1 WHERE iwiki_id = %s AND slug = %s",
            (self.iwiki_id, domain),
        )

    def markdown_snapshot(self, domain: str):
        """Return one coherent Markdown snapshot with its canonical revision."""
        from ..codegraph.linking import (
            MarkdownDomainSnapshot,
            MarkdownPageSnapshot,
            markdown_revision,
        )

        domain = _validate_identifier(domain, "domain")
        self._require_read(domain)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT d.markdown_generation, p.slug, p.markdown "
                    "FROM iwiki.domains d LEFT JOIN iwiki.pages p "
                    "ON p.iwiki_id = d.iwiki_id AND p.domain_id = d.domain_id "
                    "WHERE d.iwiki_id = %s AND d.slug = %s ORDER BY p.slug",
                    (self.iwiki_id, domain),
                )
                rows = cursor.fetchall()
        if not rows:
            raise ValueError("domain not found")
        pages = tuple(
            MarkdownPageSnapshot(slug=slug, markdown=markdown)
            for _generation, slug, markdown in rows
            if slug is not None
        )
        return MarkdownDomainSnapshot(
            change_token=rows[0][0],
            revision=markdown_revision(pages),
            pages=pages,
        )

    def write_page(self, domain: str, slug: str, markdown: str) -> dict:
        self._require_write(_validate_identifier(domain, "domain"))
        domain, slug, markdown, chunks, records, targets = self._prepare_page(
            domain, slug, markdown
        )
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT domain_id FROM iwiki.domains "
                    "WHERE iwiki_id = %s AND slug = %s",
                    (self.iwiki_id, domain),
                )
                domain_row = cursor.fetchone()
                if domain_row is None:
                    raise ValueError("domain not found")
                cursor.execute(
                    "INSERT INTO iwiki.pages (iwiki_id, domain_id, slug, markdown) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (iwiki_id, domain_id, slug) DO NOTHING "
                    "RETURNING page_id, revision",
                    (self.iwiki_id, domain_row[0], slug, markdown),
                )
                row = cursor.fetchone()
                if row is None:
                    return {
                        "error": "page_exists",
                        "hint": "read the page before updating it",
                    }
                self._replace_derived(cursor, row[0], chunks, records, targets)
                self._bump_markdown_generation(cursor, domain)
        return {
            "page": f"{domain}/{slug}.md",
            "revision": row[1],
            "indexed_chunks": len(records),
        }

    def update_page(
        self,
        domain: str,
        slug: str,
        markdown: str,
        expected_revision: int | None,
    ) -> dict:
        self._require_write(_validate_identifier(domain, "domain"))
        if expected_revision is None:
            return expected_revision_required()
        domain, slug, markdown, chunks, records, targets = self._prepare_page(
            domain, slug, markdown, reuse=True
        )
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE iwiki.pages p SET markdown = %s, "
                    "revision = p.revision + 1, updated_at = CURRENT_TIMESTAMP "
                    "FROM iwiki.domains d WHERE p.iwiki_id = %s "
                    "AND d.iwiki_id = p.iwiki_id AND d.domain_id = p.domain_id "
                    "AND d.slug = %s AND p.slug = %s AND p.revision = %s "
                    "RETURNING p.page_id, p.revision",
                    (markdown, self.iwiki_id, domain, slug, expected_revision),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        "SELECT p.revision FROM iwiki.pages p "
                        "JOIN iwiki.domains d ON d.iwiki_id = p.iwiki_id "
                        "AND d.domain_id = p.domain_id WHERE p.iwiki_id = %s "
                        "AND d.slug = %s AND p.slug = %s",
                        (self.iwiki_id, domain, slug),
                    )
                    current = cursor.fetchone()
                    return revision_conflict(current[0] if current else None)
                self._replace_derived(
                    cursor, row[0], chunks, records, targets, reuse=True
                )
                self._bump_markdown_generation(cursor, domain)
        return {
            "page": f"{domain}/{slug}.md",
            "revision": row[1],
            "indexed_chunks": len(records),
        }

    def delete_page(
        self,
        domain: str,
        slug: str,
        expected_revision: int | None,
    ) -> dict:
        self._require_write(_validate_identifier(domain, "domain"))
        if expected_revision is None:
            return expected_revision_required()
        domain = _validate_identifier(domain, "domain")
        slug = _validate_identifier(slug, "page slug")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT p.page_id, p.revision FROM iwiki.pages p "
                    "JOIN iwiki.domains d ON d.iwiki_id = p.iwiki_id "
                    "AND d.domain_id = p.domain_id WHERE p.iwiki_id = %s "
                    "AND d.slug = %s AND p.slug = %s FOR UPDATE OF p",
                    (self.iwiki_id, domain, slug),
                )
                current = cursor.fetchone()
                if current is None or current[1] != expected_revision:
                    return revision_conflict(current[1] if current else None)
                cursor.execute(
                    "UPDATE iwiki.links SET target_page_id = NULL "
                    "WHERE iwiki_id = %s AND target_page_id = %s",
                    (self.iwiki_id, current[0]),
                )
                cursor.execute(
                    "DELETE FROM iwiki.pages WHERE iwiki_id = %s AND page_id = %s",
                    (self.iwiki_id, current[0]),
                )
                self._bump_markdown_generation(cursor, domain)
        return {"page": f"{domain}/{slug}.md", "deleted": True}

    @staticmethod
    def _decode_record(
        file: str,
        section_id: str,
        heading: str,
        scale: float,
        quantized: list[int],
    ) -> Record:
        metadata = json.loads(section_id)
        return Record(
            id=f"{file}#{heading}",
            file=file,
            heading=heading,
            chunk=metadata["chunk"],
            hash=metadata["hash"],
            dim=len(quantized),
            scale=scale,
            q=list(quantized),
            type=metadata["type"],
            tags=metadata["tags"],
            kind=metadata["kind"],
            ordinal=metadata["ordinal"],
            v=2,
        )

    def _search_material(self, domains: list[str], query_vector: list[float]):
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT d.slug, p.slug, p.markdown, c.section_id, c.heading, "
                    "c.quantization_scale, c.quantized_embedding, "
                    "1 - (c.embedding <=> %s) AS score "
                    "FROM iwiki.chunks c "
                    "JOIN iwiki.pages p ON p.iwiki_id = c.iwiki_id "
                    "AND p.page_id = c.page_id "
                    "JOIN iwiki.domains d ON d.iwiki_id = p.iwiki_id "
                    "AND d.domain_id = p.domain_id "
                    "WHERE c.iwiki_id = %s AND d.slug = ANY(%s) "
                    "ORDER BY c.embedding <=> %s, d.slug, p.slug, c.ordinal",
                    (
                        np.asarray(query_vector, dtype=np.float32),
                        self.iwiki_id,
                        domains,
                        np.asarray(query_vector, dtype=np.float32),
                    ),
                )
                rows = cursor.fetchall()

                cursor.execute(
                    "SELECT sd.slug, sp.slug, l.target_domain, l.target_slug "
                    "FROM iwiki.links l "
                    "JOIN iwiki.pages sp ON sp.iwiki_id = l.iwiki_id "
                    "AND sp.page_id = l.source_page_id "
                    "JOIN iwiki.domains sd ON sd.iwiki_id = sp.iwiki_id "
                    "AND sd.domain_id = sp.domain_id "
                    "WHERE l.iwiki_id = %s AND sd.slug = ANY(%s) "
                    "AND l.target_domain = ANY(%s)",
                    (self.iwiki_id, domains, domains),
                )
                edges = cursor.fetchall()

        records: dict[str, list[Record]] = {domain: [] for domain in domains}
        markdown: dict[str, dict[str, str]] = {domain: {} for domain in domains}
        scores = {}
        for domain, slug, content, section_id, heading, scale, quantized, score in rows:
            file = f"{slug}.md"
            decoded = self._decode_record(
                file, section_id, heading, scale, quantized
            )
            records[domain].append(decoded)
            markdown[domain][file] = content
            scores[(domain, file, heading, decoded.chunk)] = float(score)
        adjacency: dict[str, set[str]] = {}
        for source_domain, source_slug, target_domain, target_slug in edges:
            source = f"{source_domain}/{source_slug}.md"
            target = f"{target_domain}/{target_slug}.md"
            adjacency.setdefault(source, set()).add(target)
            adjacency.setdefault(target, set()).add(source)
        return records, markdown, scores, adjacency

    def prepare_read_candidates(
        self,
        domains: list[str],
        query: str,
        *,
        top_k: int,
        threshold: float,
        mode: str = "hybrid",
        type: str | None = None,
        tags: list | None = None,
    ) -> list[dict]:
        domains = [_validate_identifier(domain, "domain") for domain in domains]
        for domain in domains:
            self._require_read(domain)
        query_vector = self._embedder(self.cfg, [query])[0]
        if len(query_vector) != self.cfg.dimensions:
            raise ValueError("embedding dimension mismatch")
        records, markdown, scores, adjacency = self._search_material(
            domains, query_vector
        )
        return retrieval.prepare_storage_candidates(
            self.cfg,
            domains,
            query,
            records,
            markdown,
            scores,
            lambda page: adjacency.get(page, ()),
            top_k,
            threshold,
            mode,
            type,
            tags,
        )

    def search(
        self,
        domains: list[str],
        query: str,
        *,
        top_k: int,
        threshold: float,
        mode: str = "hybrid",
        type: str | None = None,
        tags: list | None = None,
    ) -> list[dict]:
        return self.prepare_read_candidates(
            domains,
            query,
            top_k=top_k,
            threshold=threshold,
            mode=mode,
            type=type,
            tags=tags,
        )[:top_k]

    def hydrate_candidates(self, candidates: list[dict]) -> list[dict]:
        """Attach current chunk text for the shared reranking pipeline."""
        for domain in {item["domain"] for item in candidates}:
            self._require_read(_validate_identifier(domain, "domain"))
        wanted = {
            (
                item["domain"],
                item["file"],
                item["heading"],
                item["chunk"],
            )
            for item in candidates
        }
        if not wanted:
            return []
        domains = sorted({identity[0] for identity in wanted})
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT d.slug, p.slug, p.markdown FROM iwiki.pages p "
                    "JOIN iwiki.domains d ON d.iwiki_id = p.iwiki_id "
                    "AND d.domain_id = p.domain_id "
                    "WHERE p.iwiki_id = %s AND d.slug = ANY(%s)",
                    (self.iwiki_id, domains),
                )
                pages = cursor.fetchall()
        texts = {}
        for domain, slug, markdown in pages:
            file = f"{slug}.md"
            for chunk in indexer.chunk_markdown(
                file,
                markdown,
                self.cfg.chunk_size,
                self.cfg.chunk_overlap,
                self.cfg.summary_max,
            ):
                identity = (domain, file, chunk.heading, chunk.chunk)
                if identity in wanted:
                    texts[identity] = chunk.text
        return [
            {**candidate, "text": texts[identity]}
            for candidate in candidates
            if (
                identity := (
                    candidate["domain"],
                    candidate["file"],
                    candidate["heading"],
                    candidate["chunk"],
                )
            ) in texts
        ]

    def graph_neighbors(
        self, domains: list[str], page: str, *, depth: int
    ) -> list[str]:
        domains = [_validate_identifier(domain, "domain") for domain in domains]
        for domain in domains:
            self._require_read(domain)
        records, markdown, _scores, adjacency = self._search_material(
            domains, [1.0] + [0.0] * (self.cfg.dimensions - 1)
        )
        del records
        allowed = {
            f"{domain}/{file}"
            for domain, pages in markdown.items()
            for file in pages
        }
        ranked = graph.rank_storage_graph(
            [(page, "graph", 0)],
            lambda item: adjacency.get(item, ()),
            depth,
            self.cfg.bfs_top_k,
            allowed,
        )
        return [row["file"] for row in ranked if row["file"] != page]

    def related(self, domain: str, section_id: str) -> dict:
        domain = _validate_identifier(domain, "domain")
        self._require_read(domain)
        records, _markdown, _scores, _adjacency = self._search_material(
            [domain], [1.0] + [0.0] * (self.cfg.dimensions - 1)
        )
        return related(
            section_id,
            records.get(domain, []),
            self.cfg.top_k,
            self.cfg.graph_depth,
        )

    def locate_target(
        self, domain: str, query: str, heading: str | None = None
    ) -> dict:
        candidates = self.search(
            [domain],
            query,
            top_k=self.cfg.top_k,
            threshold=self.cfg.write_seed_threshold,
            mode="semantic",
        )
        if heading is not None:
            wanted = heading.strip().lower()
            candidates = [
                candidate
                for candidate in candidates
                if candidate["heading"].lower() == wanted
            ]
        if not candidates:
            return {"domain": domain, "exists": False}
        best = candidates[0]
        return {
            "domain": domain,
            "file": best["file"],
            "heading": best["heading"],
            "score": best["score"],
            "exists": True,
        }

    def index_domain(self, domain: str) -> dict:
        domain = _validate_identifier(domain, "domain")
        self._require_write(domain)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT p.page_id, p.slug, p.markdown FROM iwiki.pages p "
                    "JOIN iwiki.domains d ON d.iwiki_id = p.iwiki_id "
                    "AND d.domain_id = p.domain_id WHERE p.iwiki_id = %s "
                    "AND d.slug = %s ORDER BY p.slug",
                    (self.iwiki_id, domain),
                )
                pages = cursor.fetchall()
        prepared = []
        for page_id, slug, markdown in pages:
            _domain, _slug, _markdown, chunks, records, targets = (
                self._prepare_page(domain, slug, markdown)
            )
            prepared.append((page_id, chunks, records, targets))
        with self._connect() as connection:
            with connection.cursor() as cursor:
                for page_id, chunks, records, targets in prepared:
                    self._replace_derived(
                        cursor, page_id, chunks, records, targets
                    )
        count = sum(len(records) for _page, _chunks, records, _targets in prepared)
        return {
            "domain": domain,
            "indexed_chunks": count,
            "reused": 0,
            "embedded": count,
            "bytes": 0,
            "over_cap": False,
        }

    def lint_domain(self, domain: str, visible_domains: list[str]) -> dict:
        domain = _validate_identifier(domain, "domain")
        self._require_read(domain)
        for visible_domain in visible_domains:
            self._require_read(_validate_identifier(visible_domain, "domain"))
        visible = {
            visible_domain: set(self.list_pages(visible_domain))
            for visible_domain in visible_domains
        }
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT p.slug, p.markdown FROM iwiki.pages p "
                    "JOIN iwiki.domains d ON d.iwiki_id = p.iwiki_id "
                    "AND d.domain_id = p.domain_id WHERE p.iwiki_id = %s "
                    "AND d.slug = %s ORDER BY p.slug",
                    (self.iwiki_id, domain),
                )
                pages = cursor.fetchall()
        broken = []
        sections = []
        for slug, markdown in pages:
            sections.extend(
                {"page": f"{slug}.md", **finding}
                for finding in validate_page(markdown)
            )
            for target in parse_link_targets(markdown, domain):
                if (
                    target.target_domain in visible
                    and target.target_page not in visible[target.target_domain]
                ):
                    broken.append(
                        {
                            "page": f"{slug}.md",
                            "ref": (
                                f"{target.target_domain}/{target.target_page}"
                            ),
                        }
                    )
        return {
            "wiki_present": True,
            "pages": len(pages),
            "broken": broken,
            "orphans": [],
            "stale": [],
            "missing_source": [],
            "legacy_wikilink": [],
            "sections": sections,
            "missing_frontmatter": [],
            "tag_drift": [],
            "reserved_target": [],
            "unavailable_domain": [],
            "graph": {
                "available": True,
                "schema_version": 2,
                "state": "ready",
                "fingerprint_match": True,
                "missing_pages": [],
                "extra_pages": [],
                "missing_edges": [],
                "extra_edges": [],
                "anchor_mismatches": [],
            },
            "code_graph": self._code_graph_lint(domain, pages),
        }

    def _code_graph_lint(self, domain: str, pages) -> dict:
        """Report stored and current Markdown revisions for the code graph."""
        from ..codegraph.linking import MarkdownPageSnapshot, markdown_revision

        current_pages = tuple(
            MarkdownPageSnapshot(slug=slug, markdown=markdown)
            for slug, markdown in pages
        )
        current_revision = markdown_revision(current_pages)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT s.snapshot_revision, s.markdown_revision, "
                    "s.markdown_generation, d.markdown_generation "
                    "FROM iwiki.domains d "
                    "LEFT JOIN iwiki.code_graph_domain_state g "
                    "ON g.iwiki_id = d.iwiki_id AND g.domain_id = d.domain_id "
                    "LEFT JOIN iwiki.code_graph_snapshots s "
                    "ON s.iwiki_id = g.iwiki_id AND s.domain_id = g.domain_id "
                    "AND s.snapshot_id = g.active_snapshot_id "
                    "WHERE d.iwiki_id = %s AND d.slug = %s",
                    (self.iwiki_id, domain),
                )
                row = cursor.fetchone()
        if row is None or row[0] is None:
            return {
                "available": False,
                "state": "missing_snapshot",
                "revision": None,
                "stored_markdown_revision": None,
                "current_markdown_revision": current_revision,
                "stored_change_token": None,
                "current_change_token": row[3] if row else None,
                "wiki_links_stale": False,
                "findings": [],
                "hint": "publish a code graph snapshot for this domain",
            }
        return {
            "available": True,
            "state": "ready",
            "revision": row[0],
            "stored_markdown_revision": row[1],
            "current_markdown_revision": current_revision,
            "stored_change_token": row[2],
            "current_change_token": row[3],
            "wiki_links_stale": row[1] != current_revision,
            "findings": [],
        }
