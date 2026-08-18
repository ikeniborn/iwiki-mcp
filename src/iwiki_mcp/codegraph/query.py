"""Projection-free typed entity search over one ready code-graph snapshot."""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Any

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
    "enum",
    "file",
    "function",
    "interface",
    "method",
    "module",
    "type_alias",
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


def search_result_from_row(row: tuple[Any, ...]) -> SearchResult:
    """Map one ranked entity row onto the shared public search result."""
    rank = int(row[17])
    matched_alias = row[18]
    return SearchResult(
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
        matched_alias=None if matched_alias is None else str(matched_alias),
        alias_ambiguous=int(row[19]) > 1,
        alias_target_count=int(row[19]),
    )


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


def _path_filter(
    request: ValidatedSearchRequest,
    column: str,
) -> tuple[str, list[object]]:
    if request.path is None:
        return "", []
    return (
        f"AND substr({column}, 1, length(?)) = ?",
        [request.path, request.path],
    )


def _excluded_filter(
    excluded_ids: tuple[str, ...],
    column: str,
) -> tuple[str, list[object]]:
    if not excluded_ids:
        return "", []
    placeholders = ", ".join("?" for _item in excluded_ids)
    return f"AND {column} NOT IN ({placeholders})", list(excluded_ids)


def _canonical_predicate(
    request: ValidatedSearchRequest,
    name: str,
    *,
    qualified: str,
    local: str,
    tokens: str,
    signature: str,
    raw_signature: str,
    path: str,
    raw_path: str,
) -> tuple[str, list[object]]:
    upper = request.query + "\U0010ffff"
    prefix = (
        f"({qualified} >= ? AND {qualified} < ?) OR "
        f"({local} >= ? AND {local} < ?)"
    )
    exact = f"{qualified} = ? OR {local} = ?"
    if name == "qualified_exact":
        return f"{qualified} = ?", [request.query]
    if name == "local_exact":
        return f"{local} = ? AND {qualified} != ?", [
            request.query,
            request.query,
        ]
    if name == "canonical_lexical":
        lexical, parameters = _lexical_expression(tokens, request.tokens)
        return (
            f"({lexical}) AND NOT ({prefix}) AND NOT ({exact})",
            [
                *parameters,
                request.query,
                upper,
                request.query,
                upper,
                request.query,
                request.query,
            ],
        )
    if name == "signature":
        return (
            f"{raw_signature} IS NOT NULL AND "
            f"instr(COALESCE({signature}, lower({raw_signature})), ?) > 0 "
            f"AND NOT ({prefix}) AND NOT ({exact})",
            [
                request.query.casefold(),
                request.query,
                upper,
                request.query,
                upper,
                request.query,
                request.query,
            ],
        )
    if name == "path":
        return (
            f"instr(COALESCE({path}, lower({raw_path})), ?) > 0 "
            f"AND NOT ({prefix}) AND NOT ({exact})",
            [
                request.query.casefold(),
                request.query,
                upper,
                request.query,
                upper,
                request.query,
                request.query,
            ],
        )
    raise AssertionError(f"unsupported canonical rank: {name}")


def _prefix_predicates(
    request: ValidatedSearchRequest,
    *,
    qualified: str,
    local: str,
) -> tuple[tuple[str, list[object]], tuple[str, list[object]]]:
    upper = request.query + "\U0010ffff"
    qualified_predicate = (
        f"{qualified} >= ? AND {qualified} < ? "
        f"AND {qualified} != ? AND {local} != ?"
    )
    local_predicate = (
        f"{local} >= ? AND {local} < ? "
        f"AND {local} != ? AND {qualified} != ? "
        f"AND NOT ({qualified} >= ? AND {qualified} < ?)"
    )
    return (
        (
            qualified_predicate,
            [request.query, upper, request.query, request.query],
        ),
        (
            local_predicate,
            [
                request.query,
                upper,
                request.query,
                request.query,
                request.query,
                upper,
            ],
        ),
    )


def _empty_entity_branch() -> str:
    return (
        "SELECT NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, "
        "NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL WHERE 0"
    )


def _canonical_rank_query(
    domain: str,
    request: ValidatedSearchRequest,
    name: str,
    excluded_ids: tuple[str, ...],
) -> tuple[str, list[object]]:
    rank = MATCH_RANK[name]
    symbol_kinds = tuple(
        kind for kind in request.kinds if kind not in {"file", "module"}
    )
    branch_specs = []
    if "file" in request.kinds and name != "signature":
        branch_specs.append({
            "marker": "file",
            "select": (
                "f.file_id, 'file', f.file_id, NULL, NULL, 'file', "
                "f.path, f.file_local_name, f.file_name_tokens_casefold, "
                "NULL, NULL, f.path, f.path_casefold, f.start_line, "
                "f.end_line, f.start_byte, f.end_byte"
            ),
            "from": "files AS f",
            "common": "f.repository_id = ? AND f.language = ?",
            "common_parameters": [domain, request.language],
            "qualified": "f.path",
            "local": "f.file_local_name",
            "tokens": "f.file_name_tokens_casefold",
            "signature": "NULL",
            "raw_signature": "NULL",
            "path": "f.path_casefold",
            "raw_path": "f.path",
            "entity_id": "f.file_id",
            "qualified_index": "idx_files_repository_path",
            "local_index": "idx_files_repository_local",
        })
    if "module" in request.kinds and name != "signature":
        branch_specs.append({
            "marker": "module",
            "select": (
                "f.module_id, 'module', f.file_id, f.module_id, NULL, "
                "'module', f.module_qualified_name, f.module_local_name, "
                "f.module_name_tokens_casefold, NULL, NULL, f.path, "
                "f.path_casefold, f.start_line, f.end_line, f.start_byte, "
                "f.end_byte"
            ),
            "from": "files AS f",
            "common": (
                "f.repository_id = ? AND f.language = ? "
                "AND f.module_id IS NOT NULL"
            ),
            "common_parameters": [domain, request.language],
            "qualified": "f.module_qualified_name",
            "local": "f.module_local_name",
            "tokens": "f.module_name_tokens_casefold",
            "signature": "NULL",
            "raw_signature": "NULL",
            "path": "f.path_casefold",
            "raw_path": "f.path",
            "entity_id": "f.module_id",
            "qualified_index": "idx_files_repository_module_qualified",
            "local_index": "idx_files_repository_module_local",
        })
    if symbol_kinds:
        placeholders = ", ".join("?" for _kind in symbol_kinds)
        branch_specs.append({
            "marker": "symbol",
            "select": (
                "s.symbol_id, 'symbol', f.file_id, f.module_id, s.symbol_id, "
                "s.kind, s.qualified_name, s.local_name, "
                "s.name_tokens_casefold, s.signature, "
                "s.signature_casefold, f.path, f.path_casefold, "
                "s.start_line, s.end_line, s.start_byte, s.end_byte"
            ),
            "from": "symbols AS s JOIN files AS f ON f.file_id = s.file_id",
            "common": (
                f"f.repository_id = ? AND f.language = ? "
                f"AND s.kind IN ({placeholders})"
            ),
            "common_parameters": [domain, request.language, *symbol_kinds],
            "qualified": "s.qualified_name",
            "local": "s.local_name",
            "tokens": "s.name_tokens_casefold",
            "signature": "s.signature_casefold",
            "raw_signature": "s.signature",
            "path": "f.path_casefold",
            "raw_path": "f.path",
            "entity_id": "s.symbol_id",
            "qualified_index": "idx_symbols_qualified",
            "local_index": "idx_symbols_local",
        })

    branches: list[str] = []
    parameters: list[object] = []
    for spec in branch_specs:
        path_sql, path_parameters = _path_filter(request, "f.path")
        excluded_sql, excluded_parameters = _excluded_filter(
            excluded_ids, str(spec["entity_id"])
        )
        predicates: tuple[tuple[str, list[object]], ...]
        if name == "canonical_prefix":
            predicates = _prefix_predicates(
                request,
                qualified=str(spec["qualified"]),
                local=str(spec["local"]),
            )
        else:
            predicates = (_canonical_predicate(
                request,
                name,
                qualified=str(spec["qualified"]),
                local=str(spec["local"]),
                tokens=str(spec["tokens"]),
                signature=str(spec["signature"]),
                raw_signature=str(spec["raw_signature"]),
                path=str(spec["path"]),
                raw_path=str(spec["raw_path"]),
            ),)
        for predicate_offset, (predicate, predicate_parameters) in enumerate(
            predicates
        ):
            source = str(spec["from"])
            endpoint = None
            if name == "qualified_exact":
                endpoint = "qualified"
            elif name == "local_exact":
                endpoint = "local"
            elif name == "canonical_prefix":
                endpoint = "qualified" if predicate_offset == 0 else "local"
            if endpoint is not None:
                index = spec[f"{endpoint}_index"]
                if spec["marker"] == "symbol":
                    source = source.replace(
                        "symbols AS s",
                        f"symbols AS s INDEXED BY {index}",
                    )
                else:
                    source = source.replace(
                        "files AS f",
                        f"files AS f INDEXED BY {index}",
                    )
            branches.append(
                f"/* iwiki-entity:{spec['marker']} */\n"
                f"SELECT {spec['select']}\n"
                f"FROM {source}\n"
                f"WHERE {spec['common']} {path_sql}\n"
                f"  AND {predicate} {excluded_sql}"
            )
            parameters.extend(spec["common_parameters"])
            parameters.extend(path_parameters)
            parameters.extend(predicate_parameters)
            parameters.extend(excluded_parameters)
    if not branches:
        branches.append(_empty_entity_branch())
    branch_sql = "\n    UNION ALL\n    ".join(branches)
    sql = f"""/* iwiki-rank:{name} */
WITH candidates ({_ENTITY_COLUMNS}) AS (
    {branch_sql}
)
SELECT {_ENTITY_COLUMNS}, {rank} AS match_rank, NULL AS matched_alias,
       0 AS alias_target_count
FROM candidates
/* iwiki-after-branches */
ORDER BY qualified_name, entity_id LIMIT ?"""
    return sql, parameters


def _alias_predicate(
    request: ValidatedSearchRequest,
    name: str,
) -> tuple[str, list[object]]:
    if name == "alias_exact":
        return "r.binding_name = ?", [request.query]
    if name == "alias_prefix":
        return (
            "substr(r.binding_name, 1, length(?)) = ? "
            "AND r.binding_name != ?",
            [request.query, request.query, request.query],
        )
    lexical, parameters = _lexical_expression(
        "r.binding_name_tokens_casefold", request.tokens
    )
    return (
        f"({lexical}) AND r.binding_name != ? "
        "AND NOT (substr(r.binding_name, 1, length(?)) = ?)",
        [*parameters, request.query, request.query, request.query],
    )


def _empty_alias_branch() -> str:
    return (
        "SELECT NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, "
        "NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL WHERE 0"
    )


def _alias_rank_query(
    domain: str,
    request: ValidatedSearchRequest,
    name: str,
    excluded_ids: tuple[str, ...],
) -> tuple[str, list[object]]:
    rank = MATCH_RANK[name]
    symbol_kinds = tuple(
        kind for kind in request.kinds if kind not in {"file", "module"}
    )
    predicate, predicate_parameters = _alias_predicate(request, name)
    branches: list[str] = []
    parameters: list[object] = []
    if "module" in request.kinds:
        path_sql, path_parameters = _path_filter(request, "f.path")
        excluded_sql, excluded_parameters = _excluded_filter(
            excluded_ids, "f.module_id"
        )
        branches.append(f"""/* iwiki-entity:module */
SELECT DISTINCT r.binding_name, r.binding_name_tokens_casefold,
       f.module_id, 'module', f.file_id, f.module_id, NULL, 'module',
       f.module_qualified_name, f.module_local_name,
       f.module_name_tokens_casefold, NULL, NULL, f.path, f.path_casefold,
       f.start_line, f.end_line, f.start_byte, f.end_byte
FROM relations AS r JOIN files AS f ON f.module_id = r.target_module_id
WHERE r.relation_type = 'IMPORTS' AND r.binding_kind = 'explicit_alias'
  AND r.resolution_state IN ('resolved', 'ambiguous', 'partially_resolved')
  AND f.repository_id = ? AND f.language = ? {path_sql}
  AND {predicate} {excluded_sql}""")
        parameters.extend([domain, request.language, *path_parameters])
        parameters.extend(predicate_parameters)
        parameters.extend(excluded_parameters)
    if symbol_kinds:
        placeholders = ", ".join("?" for _kind in symbol_kinds)
        path_sql, path_parameters = _path_filter(request, "f.path")
        excluded_sql, excluded_parameters = _excluded_filter(
            excluded_ids, "s.symbol_id"
        )
        branches.append(f"""/* iwiki-entity:symbol */
SELECT DISTINCT r.binding_name, r.binding_name_tokens_casefold,
       s.symbol_id, 'symbol', f.file_id, f.module_id, s.symbol_id, s.kind,
       s.qualified_name, s.local_name, s.name_tokens_casefold, s.signature,
       s.signature_casefold, f.path, f.path_casefold, s.start_line,
       s.end_line, s.start_byte, s.end_byte
FROM relations AS r JOIN symbols AS s ON s.symbol_id = r.target_symbol_id
JOIN files AS f ON f.file_id = s.file_id
WHERE r.relation_type = 'IMPORTS' AND r.binding_kind = 'explicit_alias'
  AND r.resolution_state IN ('resolved', 'ambiguous', 'partially_resolved')
  AND f.repository_id = ? AND f.language = ?
  AND s.kind IN ({placeholders}) {path_sql}
  AND {predicate} {excluded_sql}""")
        parameters.extend([
            domain,
            request.language,
            *symbol_kinds,
            *path_parameters,
        ])
        parameters.extend(predicate_parameters)
        parameters.extend(excluded_parameters)
    if not branches:
        branches.append(_empty_alias_branch())
    branch_sql = "\n    UNION ALL\n    ".join(branches)
    sql = f"""/* iwiki-rank:{name} */
WITH alias_targets (
    binding_name, binding_name_tokens_casefold, {_ENTITY_COLUMNS}
) AS (
    {branch_sql}
), alias_counts AS (
    SELECT binding_name, COUNT(DISTINCT entity_id) AS target_count
    FROM alias_targets GROUP BY binding_name
), ranked AS (
    SELECT a.*, c.target_count, ROW_NUMBER() OVER (
        PARTITION BY a.entity_id ORDER BY a.binding_name
    ) AS row_number
    FROM alias_targets AS a JOIN alias_counts AS c USING (binding_name)
)
SELECT {_ENTITY_COLUMNS}, {rank} AS match_rank, binding_name AS matched_alias,
       target_count AS alias_target_count
FROM ranked WHERE row_number = 1
/* iwiki-after-branches */
ORDER BY qualified_name, entity_id, matched_alias LIMIT ?"""
    return sql, parameters


def _rank_query(
    domain: str,
    request: ValidatedSearchRequest,
    name: str,
    excluded_ids: tuple[str, ...],
) -> tuple[str, list[object]]:
    if name.startswith("alias_"):
        return _alias_rank_query(domain, request, name, excluded_ids)
    return _canonical_rank_query(domain, request, name, excluded_ids)


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
                    item = search_result_from_row(row)
                    winner_key = (
                        MATCH_RANK[item.match],
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
    "search_result_from_row",
    "validate_search_request",
]
