"""Tenant-scoped PostgreSQL page, vector, and link storage."""
from __future__ import annotations

import json
from typing import Callable

import numpy as np
import psycopg
from pgvector.psycopg import register_vector

from .. import graph, indexer, retrieval
from ..engine import frontmatter
from ..engine.config import Config
from ..engine.embed import embed_texts
from ..engine.links import parse_link_targets
from ..engine.related import related
from ..engine.store import Record, dequantize
from ..engine.validate import validate_page
from ..storage import expected_revision_required, revision_conflict


_BLOCKING_FINDINGS = {"deep_heading", "pre_h2_text"}


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
    ) -> None:
        self._dsn = dsn
        self.iwiki_id = _validate_identifier(iwiki_id, "iwiki id")
        self.cfg = cfg
        self._embedder = embedder

    def with_embedder(self, embedder: Callable) -> "PostgresStore":
        return PostgresStore(
            self._dsn,
            self.iwiki_id,
            self.cfg,
            embedder=embedder,
        )

    def _connect(self):
        connection = psycopg.connect(self._dsn)
        register_vector(connection)
        return connection

    def create_wiki(self, slug: str) -> None:
        slug = _validate_identifier(slug, "wiki slug")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO iwiki.iwikis (iwiki_id, slug) VALUES (%s, %s) "
                    "ON CONFLICT (iwiki_id) DO NOTHING",
                    (self.iwiki_id, slug),
                )

    def create_domain(self, domain: str) -> None:
        domain = _validate_identifier(domain, "domain")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO iwiki.domains (iwiki_id, slug) VALUES (%s, %s) "
                    "ON CONFLICT (iwiki_id, slug) DO NOTHING",
                    (self.iwiki_id, domain),
                )

    def list_domains(self) -> list[str]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT slug FROM iwiki.domains WHERE iwiki_id = %s "
                    "ORDER BY slug",
                    (self.iwiki_id,),
                )
                return [row[0] for row in cursor.fetchall()]

    def list_pages(self, domain: str) -> list[str]:
        domain = _validate_identifier(domain, "domain")
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

    def _prepare_page(self, domain: str, slug: str, markdown: str):
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
        chunks, records = indexer.prepare_page_records(
            self.cfg,
            file,
            markdown,
            embedder=self._embedder,
        )
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

    def _replace_derived(
        self,
        cursor,
        page_id: int,
        chunks,
        records: list[Record],
        targets,
    ) -> None:
        cursor.execute(
            "DELETE FROM iwiki.chunks WHERE iwiki_id = %s AND page_id = %s",
            (self.iwiki_id, page_id),
        )
        cursor.execute(
            "DELETE FROM iwiki.links WHERE iwiki_id = %s AND source_page_id = %s",
            (self.iwiki_id, page_id),
        )
        for storage_ordinal, (chunk, record) in enumerate(zip(chunks, records)):
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
        for target in targets:
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

    def write_page(self, domain: str, slug: str, markdown: str) -> dict:
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
        if expected_revision is None:
            return expected_revision_required()
        domain, slug, markdown, chunks, records, targets = self._prepare_page(
            domain, slug, markdown
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
                self._replace_derived(cursor, row[0], chunks, records, targets)
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
            "code_graph": {
                "available": False,
                "state": "unsupported",
                "revision": None,
                "findings": [],
                "hint": "code graph requires Git storage",
            },
        }
