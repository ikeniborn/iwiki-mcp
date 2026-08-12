"""Projection-free typed entity search over one ready code-graph snapshot."""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from .models import (
    _TOKENS,
    _validated_relative_posix,
    CodeGraphError,
    SearchResult,
)
from .schema import CodeGraphStoreError


MATCH_RANK = {
    "qualified_exact": 0,
    "local_exact": 1,
    "alias_exact": 2,
    "canonical_prefix": 3,
    "alias_prefix": 4,
    "canonical_lexical": 5,
    "alias_lexical": 6,
    "signature": 7,
    "path": 8,
}
_MATCH_BY_RANK = {rank: name for name, rank in MATCH_RANK.items()}

KNOWN_ENTITY_KINDS = frozenset({
    "async_function",
    "class",
    "file",
    "function",
    "method",
    "module",
})
# Compatibility name for internal callers written before schema v2.
KNOWN_SYMBOL_KINDS = KNOWN_ENTITY_KINDS

_ENTITY_COLUMNS = (
    "entity_id, entity_type, file_id, module_id, symbol_id, kind, "
    "qualified_name, local_name, name_tokens_casefold, signature, "
    "signature_casefold, path, path_casefold, start_line, end_line, "
    "start_byte, end_byte"
)


class CodeGraphQueryError(CodeGraphError):
    """Raised when an entity query violates the public search contract."""

    code = "invalid_config"


@dataclass(frozen=True)
class ValidatedSearchRequest:
    """Pure validated input consumed by the SQLite query boundary."""

    query: str
    kinds: tuple[str, ...]
    path: str | None
    language: str
    limit: int
    tokens: tuple[str, ...]


def result_key(item: SearchResult) -> tuple[int, str, str]:
    """Return the complete stable ordering key for one search result."""
    return MATCH_RANK[item.match], item.qualified_name, item.entity_id


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(_TOKENS.findall(value.casefold()))


def validate_search_request(
    query: str,
    *,
    kinds: list[str] | None = None,
    path: str | None = None,
    languages: list[str] | None = None,
    limit: int = 20,
) -> ValidatedSearchRequest:
    """Validate public inputs without touching binding, status, or SQLite."""
    if not isinstance(query, str) or not any(not char.isspace() for char in query):
        raise CodeGraphQueryError("query must be nonblank")
    if "\0" in query:
        raise CodeGraphQueryError("query must not contain NUL")
    try:
        encoded_query = query.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CodeGraphQueryError("query must be valid UTF-8") from exc
    if len(encoded_query) > 4096:
        raise CodeGraphQueryError("query must be at most 4096 UTF-8 bytes")
    query_tokens = tuple(sorted(set(_tokens(query))))
    if len(query_tokens) > 64:
        raise CodeGraphQueryError("query must contain at most 64 distinct tokens")
    if kinds is None:
        normalized_kinds = tuple(sorted(KNOWN_ENTITY_KINDS))
    elif (
        not isinstance(kinds, list)
        or not kinds
        or any(
            not isinstance(kind, str) or kind not in KNOWN_ENTITY_KINDS
            for kind in kinds
        )
    ):
        raise CodeGraphQueryError("unknown code kind")
    else:
        normalized_kinds = tuple(sorted(set(kinds)))
    if languages is not None and (
        not isinstance(languages, list)
        or not languages
        or any(language != "python" for language in languages)
    ):
        raise CodeGraphQueryError("unsupported language")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise CodeGraphQueryError("limit must be between 1 and 100")
    if path is not None:
        try:
            _validated_relative_posix(path)
        except ValueError as exc:
            raise CodeGraphQueryError(
                "path must be a safe project-relative prefix"
            ) from exc
    return ValidatedSearchRequest(
        query=query,
        kinds=normalized_kinds,
        path=path,
        language="python",
        limit=limit,
        tokens=query_tokens,
    )


def _lexical_expression(column: str, tokens: tuple[str, ...]) -> tuple[str, list[str]]:
    if not tokens:
        return "0", []
    return (
        " AND ".join(f"instr({column}, ?) > 0" for _token in tokens),
        [f"\x1f{token}\x1f" for token in tokens],
    )


def _canonical_rank_sql(request: ValidatedSearchRequest) -> tuple[str, list[str]]:
    lexical_sql, lexical_parameters = _lexical_expression(
        "name_tokens_casefold", request.tokens
    )
    folded = request.query.casefold()
    sql = f"""
        CASE
            WHEN qualified_name = ? THEN {MATCH_RANK['qualified_exact']}
            WHEN local_name = ? THEN {MATCH_RANK['local_exact']}
            WHEN substr(qualified_name, 1, length(?)) = ?
              OR substr(local_name, 1, length(?)) = ?
                THEN {MATCH_RANK['canonical_prefix']}
            WHEN {lexical_sql} THEN {MATCH_RANK['canonical_lexical']}
            WHEN signature IS NOT NULL
             AND instr(COALESCE(signature_casefold, lower(signature)), ?) > 0
                THEN {MATCH_RANK['signature']}
            WHEN instr(COALESCE(path_casefold, lower(path)), ?) > 0
                THEN {MATCH_RANK['path']}
        END
    """
    return sql, [
        request.query,
        request.query,
        request.query,
        request.query,
        request.query,
        request.query,
        *lexical_parameters,
        folded,
        folded,
    ]


def _alias_rank_sql(request: ValidatedSearchRequest) -> tuple[str, list[str]]:
    lexical_sql, lexical_parameters = _lexical_expression(
        "binding_name_tokens_casefold", request.tokens
    )
    sql = f"""
        CASE
            WHEN binding_name = ? THEN {MATCH_RANK['alias_exact']}
            WHEN substr(binding_name, 1, length(?)) = ?
                THEN {MATCH_RANK['alias_prefix']}
            WHEN {lexical_sql} THEN {MATCH_RANK['alias_lexical']}
        END
    """
    return sql, [
        request.query,
        request.query,
        request.query,
        *lexical_parameters,
    ]


def _query_sql(
    domain: str,
    request: ValidatedSearchRequest,
) -> tuple[str, list[object]]:
    placeholders = ", ".join("?" for _kind in request.kinds)
    path_filter = ""
    path_parameters: list[object] = []
    if request.path is not None:
        path_filter = "AND substr(path, 1, length(?)) = ?"
        path_parameters.extend((request.path, request.path))
    canonical_rank, canonical_parameters = _canonical_rank_sql(request)
    alias_rank, alias_parameters = _alias_rank_sql(request)
    sql = f"""
        WITH entities ({_ENTITY_COLUMNS}) AS (
            SELECT
                f.file_id, 'file', f.file_id, NULL, NULL, 'file',
                f.path, f.file_local_name, f.file_name_tokens_casefold,
                NULL, NULL, f.path, f.path_casefold,
                f.start_line, f.end_line, f.start_byte, f.end_byte
            FROM files AS f
            WHERE f.repository_id = ? AND f.language = ?
            UNION ALL
            SELECT
                f.module_id, 'module', f.file_id, f.module_id, NULL, 'module',
                f.module_qualified_name, f.module_local_name,
                f.module_name_tokens_casefold,
                NULL, NULL, f.path, f.path_casefold,
                f.start_line, f.end_line, f.start_byte, f.end_byte
            FROM files AS f
            WHERE f.repository_id = ? AND f.language = ?
              AND f.module_id IS NOT NULL
            UNION ALL
            SELECT
                s.symbol_id, 'symbol', f.file_id, f.module_id, s.symbol_id,
                s.kind, s.qualified_name, s.local_name,
                s.name_tokens_casefold, s.signature, s.signature_casefold,
                f.path, f.path_casefold,
                s.start_line, s.end_line, s.start_byte, s.end_byte
            FROM symbols AS s
            JOIN files AS f ON f.file_id = s.file_id
            WHERE f.repository_id = ? AND f.language = ?
        ),
        filtered_entities AS (
            SELECT {_ENTITY_COLUMNS}
            FROM entities
            WHERE kind IN ({placeholders}) {path_filter}
        ),
        ranked_canonical AS (
            SELECT {_ENTITY_COLUMNS}, {canonical_rank} AS match_rank
            FROM filtered_entities
        ),
        canonical_matches AS (
            SELECT {_ENTITY_COLUMNS}, match_rank, NULL AS matched_alias,
                   0 AS alias_target_count
            FROM ranked_canonical
            WHERE match_rank IS NOT NULL
        ),
        alias_targets AS (
            SELECT DISTINCT
                r.binding_name, r.binding_name_tokens_casefold,
                e.{_ENTITY_COLUMNS.replace(', ', ', e.')}
            FROM relations AS r
            JOIN filtered_entities AS e
              ON e.entity_type = 'module'
             AND e.module_id = r.target_module_id
            WHERE r.relation_type = 'IMPORTS'
              AND r.binding_kind = 'explicit_alias'
              AND r.resolution_state IN (
                  'resolved', 'ambiguous', 'partially_resolved'
              )
            UNION ALL
            SELECT DISTINCT
                r.binding_name, r.binding_name_tokens_casefold,
                e.{_ENTITY_COLUMNS.replace(', ', ', e.')}
            FROM relations AS r
            JOIN filtered_entities AS e
              ON e.entity_type = 'symbol'
             AND e.symbol_id = r.target_symbol_id
            WHERE r.relation_type = 'IMPORTS'
              AND r.binding_kind = 'explicit_alias'
              AND r.resolution_state IN (
                  'resolved', 'ambiguous', 'partially_resolved'
              )
        ),
        alias_counts AS (
            SELECT binding_name, COUNT(DISTINCT entity_id) AS target_count
            FROM alias_targets
            GROUP BY binding_name
        ),
        ranked_aliases AS (
            SELECT a.*, c.target_count, {alias_rank} AS match_rank
            FROM alias_targets AS a
            JOIN alias_counts AS c USING (binding_name)
        ),
        alias_matches AS (
            SELECT {_ENTITY_COLUMNS}, match_rank,
                   binding_name AS matched_alias,
                   target_count AS alias_target_count
            FROM ranked_aliases
            WHERE match_rank IS NOT NULL
        ),
        matches AS (
            SELECT * FROM canonical_matches
            UNION ALL
            SELECT * FROM alias_matches
        )
        SELECT * FROM matches
        ORDER BY match_rank, qualified_name, entity_id, matched_alias
    """
    entity_parameters: list[object] = [
        value
        for _arm in range(3)
        for value in (domain, request.language)
    ]
    return sql, [
        *entity_parameters,
        *request.kinds,
        *path_parameters,
        *canonical_parameters,
        *alias_parameters,
    ]


class CodeGraphQuery:
    """Retrieve, rank, and de-duplicate all matching typed entities."""

    def __init__(self, domain: str) -> None:
        self.domain = domain

    def search(
        self,
        connection: sqlite3.Connection,
        request: ValidatedSearchRequest,
    ) -> tuple[SearchResult, ...]:
        """Search one ready repository without reading project source."""
        sql, parameters = _query_sql(self.domain, request)
        try:
            rows = connection.execute(sql, parameters)
            results: dict[str, SearchResult] = {}
            winner_keys: dict[str, tuple[int, str, str, str]] = {}
            for row in rows:
                rank = int(row[17])
                matched_alias = row[18]
                item = SearchResult(
                    entity_id=str(row[0]),
                    entity_type=str(row[1]),
                    file_id=None if row[2] is None else str(row[2]),
                    module_id=None if row[3] is None else str(row[3]),
                    symbol_id=None if row[4] is None else str(row[4]),
                    kind=str(row[5]),
                    qualified_name=str(row[6]),
                    local_name=str(row[7]),
                    signature=None if row[9] is None else str(row[9]),
                    path=str(row[11]),
                    start_line=int(row[13]),
                    end_line=int(row[14]),
                    start_byte=int(row[15]),
                    end_byte=int(row[16]),
                    match=_MATCH_BY_RANK[rank],
                    matched_alias=(
                        None if matched_alias is None else str(matched_alias)
                    ),
                    alias_ambiguous=int(row[19]) > 1,
                    alias_target_count=int(row[19]),
                )
                winner_key = (
                    rank,
                    item.qualified_name,
                    item.entity_id,
                    item.matched_alias or "",
                )
                if winner_key < winner_keys.get(
                    item.entity_id, (10, "", "", "")
                ):
                    results[item.entity_id] = item
                    winner_keys[item.entity_id] = winner_key
        except sqlite3.DatabaseError as exc:
            raise CodeGraphStoreError("code graph search failed") from exc
        return tuple(sorted(results.values(), key=result_key)[:request.limit])


__all__ = [
    "CodeGraphQuery",
    "CodeGraphQueryError",
    "KNOWN_ENTITY_KINDS",
    "KNOWN_SYMBOL_KINDS",
    "MATCH_RANK",
    "ValidatedSearchRequest",
    "result_key",
    "validate_search_request",
]
