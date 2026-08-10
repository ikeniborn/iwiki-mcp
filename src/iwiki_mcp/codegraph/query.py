"""Language-neutral, deterministic symbol queries over a ready snapshot."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
import re
import sqlite3

from .models import CodeGraphError, SearchResult
from .schema import CodeGraphStoreError


MATCH_RANK = {
    "exact_qualified": 0,
    "exact_local": 1,
    "prefix": 2,
    "lexical": 3,
    "signature": 4,
    "path": 5,
}

KNOWN_SYMBOL_KINDS = frozenset({
    "async_function",
    "class",
    "function",
    "method",
})

_COLUMNS = (
    "SELECT s.symbol_id, s.kind, s.qualified_name, s.local_name, "
    "s.signature, f.path, s.start_line, s.end_line, s.start_byte, "
    "s.end_byte "
)
_TOKENS = re.compile(r"[^\W_]+", re.UNICODE)


class CodeGraphQueryError(CodeGraphError):
    """Raised when a symbol query violates the public search contract."""

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
    return MATCH_RANK[item.match], item.qualified_name, item.symbol_id


def _like(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(_TOKENS.findall(value.casefold()))


def _token_match(
    query_tokens: tuple[str, ...],
    qualified_name: object | None,
    local_name: object | None,
) -> bool:
    if not query_tokens:
        return False
    names = (
        "" if qualified_name is None else str(qualified_name),
        "" if local_name is None else str(local_name),
    )
    name_tokens = set(_tokens(names[0]) + _tokens(names[1]))
    return all(token in name_tokens for token in query_tokens)


def _fallback_rank(
    query_tokens: tuple[str, ...],
    folded_query: str,
    qualified_name: object | None,
    local_name: object | None,
    signature: object | None,
    path: object | None,
) -> int:
    if _token_match(query_tokens, qualified_name, local_name):
        return MATCH_RANK["lexical"]
    if (
        signature is not None
        and folded_query in str(signature).casefold()
    ):
        return MATCH_RANK["signature"]
    if path is not None and folded_query in str(path).casefold():
        return MATCH_RANK["path"]
    return len(MATCH_RANK)


def validate_search_request(
    query: str,
    *,
    kinds: list[str] | None = None,
    path: str | None = None,
    languages: list[str] | None = None,
    limit: int = 20,
) -> ValidatedSearchRequest:
    """Validate public inputs without touching status, locks, or SQLite."""
    if not isinstance(query, str) or not query.strip():
        raise CodeGraphQueryError("query must be nonblank")
    normalized_query = query.strip()
    if kinds is None:
        normalized_kinds = tuple(sorted(KNOWN_SYMBOL_KINDS))
    elif (
        not isinstance(kinds, list)
        or not kinds
        or any(
            not isinstance(kind, str) or kind not in KNOWN_SYMBOL_KINDS
            for kind in kinds
        )
    ):
        raise CodeGraphQueryError("unknown symbol kind")
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
    if path is not None and (
        not isinstance(path, str)
        or not path
        or "\0" in path
        or "\\" in path
        or PurePosixPath(path).is_absolute()
        or PureWindowsPath(path).is_absolute()
        or PureWindowsPath(path).drive
        or ".." in PurePosixPath(path).parts
    ):
        raise CodeGraphQueryError(
            "path must be a safe project-relative prefix"
        )
    return ValidatedSearchRequest(
        query=normalized_query,
        kinds=normalized_kinds,
        path=path,
        language="python",
        limit=limit,
        tokens=tuple(dict.fromkeys(_tokens(normalized_query))),
    )


class CodeGraphQuery:
    """Retrieve bounded SQL candidates, then apply deterministic ranking."""

    def __init__(self, domain: str) -> None:
        self.domain = domain

    @staticmethod
    def _classify(
        row,
        request: ValidatedSearchRequest,
    ) -> str | None:
        qualified_name = str(row[2])
        local_name = str(row[3])
        signature = row[4]
        path = str(row[5])
        query = request.query
        if qualified_name == query:
            return "exact_qualified"
        if local_name == query:
            return "exact_local"
        folded = query.casefold()
        if (
            qualified_name.startswith(query)
            or local_name.startswith(query)
        ):
            return "prefix"
        fallback_rank = _fallback_rank(
            request.tokens,
            folded,
            qualified_name,
            local_name,
            signature,
            path,
        )
        if fallback_rank < len(MATCH_RANK):
            return tuple(MATCH_RANK)[fallback_rank]
        return None

    def _candidate_rows(
        self,
        connection: sqlite3.Connection,
        request: ValidatedSearchRequest,
    ):
        filters = ["f.repository_id = ?", "f.language = ?"]
        filter_parameters: list[object] = [self.domain, request.language]
        placeholders = ", ".join("?" for _kind in request.kinds)
        filters.append(f"s.kind IN ({placeholders})")
        filter_parameters.extend(request.kinds)
        if request.path is not None:
            filters.append("substr(f.path, 1, length(?)) = ?")
            filter_parameters.extend((request.path, request.path))

        def ordered_candidates(
            predicate: str,
            parameters: list[object],
            remaining: int,
        ):
            sql = (
                _COLUMNS
                + "FROM symbols AS s INDEXED BY idx_symbols_qualified "
                + "JOIN files AS f ON f.file_id = s.file_id "
                + "WHERE "
                + " AND ".join((*filters, f"({predicate})"))
                + " ORDER BY s.qualified_name, s.symbol_id LIMIT ?"
            )
            return connection.execute(
                sql,
                [*filter_parameters, *parameters, remaining],
            )

        def has_local_candidates(
            predicate: str,
            parameters: list[object],
        ) -> bool:
            sql = (
                "SELECT 1 FROM symbols AS s INDEXED BY idx_symbols_local "
                "JOIN files AS f ON f.file_id = s.file_id WHERE "
                + " AND ".join((*filters, f"({predicate})"))
                + " LIMIT 1"
            )
            return connection.execute(
                sql,
                [*filter_parameters, *parameters],
            ).fetchone() is not None

        prefix_end = request.query + "\U0010ffff"
        rows = []
        rows.extend(ordered_candidates(
            "s.qualified_name = ?",
            [request.query],
            request.limit,
        ))
        if len(rows) >= request.limit:
            return rows

        exact_local_sql = "s.qualified_name <> ? AND s.local_name = ?"
        exact_local_parameters = [request.query, request.query]
        if has_local_candidates(exact_local_sql, exact_local_parameters):
            rows.extend(ordered_candidates(
                exact_local_sql,
                exact_local_parameters,
                request.limit - len(rows),
            ))
            if len(rows) >= request.limit:
                return rows

        prefix_qualified_sql = (
            "s.qualified_name <> ? AND s.local_name <> ? "
            "AND s.qualified_name >= ? AND s.qualified_name < ?"
        )
        prefix_qualified_parameters = [
            request.query,
            request.query,
            request.query,
            prefix_end,
        ]
        prefix_local_sql = (
            "s.qualified_name <> ? AND s.local_name <> ? "
            "AND s.local_name >= ? AND s.local_name < ?"
        )
        prefix_local_parameters = [
            request.query,
            request.query,
            request.query,
            prefix_end,
        ]
        if has_local_candidates(prefix_local_sql, prefix_local_parameters):
            prefix_sql = (
                "s.qualified_name <> ? AND s.local_name <> ? AND "
                "((s.qualified_name >= ? AND s.qualified_name < ?) OR "
                "(s.local_name >= ? AND s.local_name < ?))"
            )
            prefix_parameters = [
                request.query,
                request.query,
                request.query,
                prefix_end,
                request.query,
                prefix_end,
            ]
        else:
            prefix_sql = prefix_qualified_sql
            prefix_parameters = prefix_qualified_parameters
        rows.extend(ordered_candidates(
            prefix_sql,
            prefix_parameters,
            request.limit - len(rows),
        ))
        if len(rows) >= request.limit:
            return rows

        folded = request.query.casefold()

        def fallback_rank(
            qualified_name,
            local_name,
            signature,
            path,
        ) -> int:
            return _fallback_rank(
                request.tokens,
                folded,
                qualified_name,
                local_name,
                signature,
                path,
            )

        connection.create_function(
            "iwiki_code_fallback_rank",
            4,
            fallback_rank,
            deterministic=True,
        )
        fallback_rank_sql = (
            "iwiki_code_fallback_rank("
            "s.qualified_name, s.local_name, s.signature, f.path)"
        )
        lexical_prefilters = []
        lexical_parameters: list[object] = []
        for token in request.tokens:
            lexical_prefilters.append(
                "(instr(lower(s.qualified_name), ?) > 0 OR "
                "instr(lower(s.local_name), ?) > 0)"
            )
            lexical_parameters.extend((token, token))
        lexical_prefilter_sql = (
            " AND ".join(lexical_prefilters)
            if lexical_prefilters else "0"
        )
        escaped_folded = _like(folded)
        signature_prefilter_sql = "s.signature LIKE ? ESCAPE '\\'"
        path_prefilter_sql = "f.path LIKE ? ESCAPE '\\'"
        non_ascii_sql = " OR ".join(
            f"length(CAST({column} AS BLOB)) > length({column})"
            for column in (
                "s.qualified_name",
                "s.local_name",
                "s.signature",
                "f.path",
            )
        )
        prefilter_sql = (
            f"({lexical_prefilter_sql}) OR {signature_prefilter_sql} "
            f"OR {path_prefilter_sql} OR ({non_ascii_sql})"
        )
        prefilter_parameters = [
            *lexical_parameters,
            "%" + escaped_folded + "%",
            "%" + escaped_folded + "%",
        ]
        candidate_rank_sql = (
            f"CASE WHEN ({prefilter_sql}) THEN {fallback_rank_sql} "
            f"ELSE {len(MATCH_RANK)} END"
        )
        stronger_parameters = [row[0] for row in rows]
        stronger_sql = (
            "s.symbol_id NOT IN ("
            + ", ".join("?" for _symbol_id in stronger_parameters)
            + ")"
            if stronger_parameters else "1"
        )
        fallback_sql = (
            _COLUMNS
            + f", {fallback_rank_sql} AS match_rank "
            + "FROM files AS f INDEXED BY idx_files_repository_path "
            + "CROSS JOIN symbols AS s INDEXED BY idx_symbols_file "
            + "WHERE "
            + " AND ".join((
                *filters,
                "s.file_id = f.file_id",
                f"({stronger_sql})",
                f"({candidate_rank_sql} < {len(MATCH_RANK)})",
            ))
            + " ORDER BY match_rank, s.qualified_name, s.symbol_id LIMIT ?"
        )
        rows.extend(connection.execute(
            fallback_sql,
            [
                *filter_parameters,
                *stronger_parameters,
                *prefilter_parameters,
                request.limit - len(rows),
            ],
        ))
        return rows

    def search(
        self,
        connection: sqlite3.Connection,
        request: ValidatedSearchRequest,
    ) -> tuple[SearchResult, ...]:
        """Search one ready repository without reading source or all symbols."""
        try:
            rows = self._candidate_rows(connection, request)
        except sqlite3.DatabaseError as exc:
            raise CodeGraphStoreError("code graph search failed") from exc

        results = {}
        for row in rows:
            match = self._classify(row, request)
            if match is None:
                continue
            item = SearchResult(*row[:10], match=match)
            previous = results.get(item.symbol_id)
            if previous is None or result_key(item) < result_key(previous):
                results[item.symbol_id] = item
        return tuple(
            sorted(results.values(), key=result_key)[:request.limit]
        )


__all__ = [
    "CodeGraphQuery",
    "CodeGraphQueryError",
    "KNOWN_SYMBOL_KINDS",
    "MATCH_RANK",
    "ValidatedSearchRequest",
    "result_key",
    "validate_search_request",
]
