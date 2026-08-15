"""Measure legacy and combined token-domain authority lookup latency."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import time
import uuid

import psycopg


CALLS = 500
ROUNDS = 3
WARM_CALLS = 50
MAX_RATIO = 1.25
DOMAIN_COUNT = 8
MANAGEMENT_COUNT = 2

LEGACY_SQL = (
    "SELECT d.slug, g.can_read, g.can_write "
    "FROM iwiki.token_domain_grants g "
    "JOIN iwiki.domains d ON d.iwiki_id = g.iwiki_id "
    "AND d.domain_id = g.domain_id "
    "WHERE g.iwiki_id = %s AND g.token_id = %s ORDER BY d.slug"
)

LEGACY_TOKEN_SQL = (
    "SELECT t.iwiki_id, t.token_digest FROM iwiki.tokens t "
    "JOIN iwiki.iwikis w ON w.iwiki_id = t.iwiki_id "
    "WHERE t.token_id = %s AND t.revoked_at IS NULL AND w.active = true"
)

COMBINED_TOKEN_SQL = (
    "SELECT t.iwiki_id, t.token_digest, t.can_create_domain "
    "FROM iwiki.tokens t JOIN iwiki.iwikis w ON w.iwiki_id = t.iwiki_id "
    "WHERE t.token_id = %s AND t.revoked_at IS NULL AND w.active = true"
)

COMBINED_SQL = (
    "SELECT d.slug, g.can_read, g.can_write, m.can_manage_grants "
    "FROM iwiki.domains d "
    "LEFT JOIN iwiki.token_domain_grants g ON g.iwiki_id = d.iwiki_id "
    "AND g.domain_id = d.domain_id AND g.token_id = %s "
    "LEFT JOIN iwiki.token_domain_management_grants m "
    "ON m.iwiki_id = d.iwiki_id AND m.domain_id = d.domain_id "
    "AND m.token_id = %s WHERE d.iwiki_id = %s "
    "AND (g.token_id IS NOT NULL OR m.token_id IS NOT NULL) ORDER BY d.slug"
)

LAST_USED_SQL = (
    "UPDATE iwiki.tokens SET last_used_at = CURRENT_TIMESTAMP "
    "WHERE iwiki_id = %s AND token_id = %s "
    "AND (last_used_at IS NULL OR "
    "last_used_at <= CURRENT_TIMESTAMP - INTERVAL '5 minutes')"
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare three rounds of 500 warm legacy and combined authority "
            "queries on one PostgreSQL database."
        )
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("IWIKI_TEST_POSTGRES_DSN"),
        help=(
            "PostgreSQL DSN; defaults to IWIKI_TEST_POSTGRES_DSN. "
            "The DSN is never printed."
        ),
    )
    return parser


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[math.ceil(len(ordered) * 0.95) - 1]


def _execute_path(
    cursor,
    token_sql: str,
    token_id: str,
    grant_sql: str,
    grant_params: tuple[str, ...],
    iwiki_id: str,
) -> None:
    cursor.execute(token_sql, (token_id,))
    cursor.fetchone()
    cursor.execute(grant_sql, grant_params)
    cursor.fetchall()
    cursor.execute(LAST_USED_SQL, (iwiki_id, token_id))


def _measure(
    cursor,
    token_sql: str,
    token_id: str,
    grant_sql: str,
    grant_params: tuple[str, ...],
    iwiki_id: str,
) -> float:
    samples = []
    for _ in range(CALLS):
        started = time.perf_counter_ns()
        _execute_path(
            cursor,
            token_sql,
            token_id,
            grant_sql,
            grant_params,
            iwiki_id,
        )
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
    return _p95(samples)


def _seed(cursor, iwiki_id: str, token_id: str) -> None:
    cursor.execute(
        "INSERT INTO iwiki.iwikis (iwiki_id, slug) VALUES (%s, %s)",
        (iwiki_id, iwiki_id),
    )
    domain_ids = []
    for number in range(DOMAIN_COUNT):
        cursor.execute(
            "INSERT INTO iwiki.domains (iwiki_id, slug) "
            "VALUES (%s, %s) RETURNING domain_id",
            (iwiki_id, f"domain-{number:03d}"),
        )
        domain_ids.append(cursor.fetchone()[0])
    digest = hashlib.sha256(f"benchmark:{token_id}".encode()).digest()
    cursor.execute(
        "INSERT INTO iwiki.tokens "
        "(iwiki_id, token_id, token_digest, owner) VALUES (%s, %s, %s, %s)",
        (iwiki_id, token_id, digest, "auth-grant-latency"),
    )
    cursor.executemany(
        "INSERT INTO iwiki.token_domain_grants "
        "(iwiki_id, token_id, domain_id, can_read, can_write) "
        "VALUES (%s, %s, %s, true, true)",
        [(iwiki_id, token_id, domain_id) for domain_id in domain_ids],
    )
    cursor.executemany(
        "INSERT INTO iwiki.token_domain_management_grants "
        "(iwiki_id, token_id, domain_id, can_manage_grants) "
        "VALUES (%s, %s, %s, true)",
        [
            (iwiki_id, token_id, domain_id)
            for domain_id in domain_ids[:MANAGEMENT_COUNT]
        ],
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.dsn:
        _parser().error("--dsn or IWIKI_TEST_POSTGRES_DSN is required")

    suffix = uuid.uuid4().hex[:12]
    iwiki_id = f"auth-grant-latency-{suffix}"
    token_id = f"auth-grant-latency-token-{suffix}"
    legacy_p95 = []
    combined_p95 = []
    seeded = False
    with psycopg.connect(args.dsn) as connection:
        try:
            with connection.cursor() as cursor:
                _seed(cursor, iwiki_id, token_id)
            connection.commit()
            seeded = True
            with connection.cursor() as cursor:
                legacy_params = (iwiki_id, token_id)
                combined_params = (token_id, token_id, iwiki_id)
                for _ in range(WARM_CALLS):
                    _execute_path(
                        cursor,
                        LEGACY_TOKEN_SQL,
                        token_id,
                        LEGACY_SQL,
                        legacy_params,
                        iwiki_id,
                    )
                    _execute_path(
                        cursor,
                        COMBINED_TOKEN_SQL,
                        token_id,
                        COMBINED_SQL,
                        combined_params,
                        iwiki_id,
                    )
                for round_number in range(ROUNDS):
                    if round_number % 2:
                        combined_p95.append(
                            _measure(
                                cursor,
                                COMBINED_TOKEN_SQL,
                                token_id,
                                COMBINED_SQL,
                                combined_params,
                                iwiki_id,
                            )
                        )
                        legacy_p95.append(
                            _measure(
                                cursor,
                                LEGACY_TOKEN_SQL,
                                token_id,
                                LEGACY_SQL,
                                legacy_params,
                                iwiki_id,
                            )
                        )
                    else:
                        legacy_p95.append(
                            _measure(
                                cursor,
                                LEGACY_TOKEN_SQL,
                                token_id,
                                LEGACY_SQL,
                                legacy_params,
                                iwiki_id,
                            )
                        )
                        combined_p95.append(
                            _measure(
                                cursor,
                                COMBINED_TOKEN_SQL,
                                token_id,
                                COMBINED_SQL,
                                combined_params,
                                iwiki_id,
                            )
                        )
        finally:
            connection.rollback()
            if seeded:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM iwiki.iwikis WHERE iwiki_id = %s",
                        (iwiki_id,),
                    )
                connection.commit()

    legacy_median = statistics.median(legacy_p95)
    combined_median = statistics.median(combined_p95)
    ratio = combined_median / legacy_median
    print(
        json.dumps(
            {
                "fixture": "8-content-2-overlapping-management-grants",
                "rounds": ROUNDS,
                "calls_per_path_per_round": CALLS,
                "legacy_p95_ms": legacy_p95,
                "combined_p95_ms": combined_p95,
                "median_p95_ratio": ratio,
                "maximum_ratio": MAX_RATIO,
            },
            sort_keys=True,
        )
    )
    return 0 if ratio <= MAX_RATIO else 1


if __name__ == "__main__":
    raise SystemExit(main())
