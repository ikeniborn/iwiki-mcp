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


def _rank_query(
    domain: str,
    request: ValidatedSearchRequest,
    name: str,
    excluded_ids: tuple[str, ...],
) -> tuple[str, list[object]]:
    placeholders = ", ".join("?" for _kind in request.kinds)
    path_filter = ""
    path_parameters: list[object] = []
    if request.path is not None:
        path_filter = "AND substr(path, 1, length(?)) = ?"
        path_parameters.extend((request.path, request.path))
    excluded_sql = ""
    if excluded_ids:
        excluded_sql = "AND entity_id NOT IN (" + ", ".join(
            "?" for _item in excluded_ids
        ) + ")"
    rank = MATCH_RANK[name]
    if name.startswith("alias_"):
        if name == "alias_exact":
            predicate, rank_parameters = "binding_name = ?", [request.query]
        elif name == "alias_prefix":
            predicate = "substr(binding_name, 1, length(?)) = ? AND binding_name != ?"
            rank_parameters = [request.query, request.query, request.query]
        else:
            lexical, lexical_parameters = _lexical_expression(
                "binding_name_tokens_casefold", request.tokens
            )
            predicate = (
                f"({lexical}) AND binding_name != ? "
                "AND NOT (substr(binding_name, 1, length(?)) = ?)"
            )
            rank_parameters = [
                *lexical_parameters, request.query, request.query, request.query
            ]
        sql = f"""/* iwiki-rank:{name} */
WITH alias_targets AS (
    SELECT DISTINCT r.binding_name, r.binding_name_tokens_casefold,
           f.module_id AS entity_id, 'module' AS entity_type, f.file_id,
           f.module_id, NULL AS symbol_id, 'module' AS kind,
           f.module_qualified_name AS qualified_name, f.module_local_name AS local_name,
           f.module_name_tokens_casefold AS name_tokens_casefold, NULL AS signature,
           NULL AS signature_casefold, f.path, f.path_casefold, f.start_line,
           f.end_line, f.start_byte, f.end_byte
    FROM relations AS r JOIN files AS f ON f.module_id = r.target_module_id
    WHERE r.relation_type = 'IMPORTS' AND r.binding_kind = 'explicit_alias'
      AND r.resolution_state IN ('resolved', 'ambiguous', 'partially_resolved')
      AND f.repository_id = ? AND f.language = ? AND 'module' IN ({placeholders}) {path_filter}
    UNION ALL
    SELECT DISTINCT r.binding_name, r.binding_name_tokens_casefold, s.symbol_id,
           'symbol', f.file_id, f.module_id, s.symbol_id, s.kind, s.qualified_name,
           s.local_name, s.name_tokens_casefold, s.signature, s.signature_casefold,
           f.path, f.path_casefold, s.start_line, s.end_line, s.start_byte, s.end_byte
    FROM relations AS r JOIN symbols AS s ON s.symbol_id = r.target_symbol_id
    JOIN files AS f ON f.file_id = s.file_id
    WHERE r.relation_type = 'IMPORTS' AND r.binding_kind = 'explicit_alias'
      AND r.resolution_state IN ('resolved', 'ambiguous', 'partially_resolved')
      AND f.repository_id = ? AND f.language = ? AND s.kind IN ({placeholders}) {path_filter}
), alias_counts AS (
    SELECT binding_name, COUNT(DISTINCT entity_id) AS target_count
    FROM alias_targets GROUP BY binding_name
), ranked AS (
    SELECT a.*, c.target_count, ROW_NUMBER() OVER (
        PARTITION BY a.entity_id ORDER BY a.binding_name
    ) AS row_number
    FROM alias_targets AS a JOIN alias_counts AS c USING (binding_name)
    WHERE {predicate} {excluded_sql}
)
SELECT {_ENTITY_COLUMNS}, {rank} AS match_rank, binding_name AS matched_alias,
       target_count AS alias_target_count
FROM ranked WHERE row_number = 1 ORDER BY qualified_name, entity_id, matched_alias LIMIT ?"""
        return sql, [
            domain, request.language, *request.kinds, *path_parameters,
            domain, request.language, *request.kinds, *path_parameters,
            *rank_parameters, *excluded_ids,
        ]
    prefix = (
        "substr(qualified_name, 1, length(?)) = ? "
        "OR substr(local_name, 1, length(?)) = ?"
    )
    exact = "qualified_name = ? OR local_name = ?"
    if name == "qualified_exact":
        predicate, rank_parameters = "qualified_name = ?", [request.query]
    elif name == "local_exact":
        predicate = "local_name = ? AND qualified_name != ?"
        rank_parameters = [request.query, request.query]
    elif name == "canonical_prefix":
        predicate = f"({prefix}) AND NOT ({exact})"
        rank_parameters = [request.query] * 6
    elif name == "canonical_lexical":
        lexical, lexical_parameters = _lexical_expression("name_tokens_casefold", request.tokens)
        predicate = f"({lexical}) AND NOT ({prefix}) AND NOT ({exact})"
        rank_parameters = [*lexical_parameters, *([request.query] * 6)]
    elif name == "signature":
        predicate = (
            "signature IS NOT NULL AND instr(COALESCE(signature_casefold, "
            f"lower(signature)), ?) > 0 AND NOT ({prefix}) AND NOT ({exact})"
        )
        rank_parameters = [request.query.casefold(), *([request.query] * 6)]
    else:
        predicate = (
            "instr(COALESCE(path_casefold, lower(path)), ?) > 0 "
            f"AND NOT ({prefix}) AND NOT ({exact})"
        )
        rank_parameters = [request.query.casefold(), *([request.query] * 6)]
    sql = f"""/* iwiki-rank:{name} */
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
)
SELECT {_ENTITY_COLUMNS}, {rank} AS match_rank, NULL AS matched_alias,
       0 AS alias_target_count
FROM entities WHERE kind IN ({placeholders}) {path_filter} AND {predicate} {excluded_sql}
ORDER BY qualified_name, entity_id LIMIT ?"""
    return sql, [
        *(value for _arm in range(3) for value in (domain, request.language)),
        *request.kinds, *path_parameters, *rank_parameters, *excluded_ids,
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
        try:
            results: dict[str, SearchResult] = {}
            winner_keys: dict[str, tuple[int, str, str, str]] = {}
            for name in _MATCH_BY_RANK.values():
                remaining = request.limit - len(results)
                if remaining == 0:
                    break
                sql, parameters = _rank_query(
                    self.domain, request, name, tuple(results)
                )
                rows = connection.execute(sql, [*parameters, remaining])
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
