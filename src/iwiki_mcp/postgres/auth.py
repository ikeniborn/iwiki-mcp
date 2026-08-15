"""Personal Bearer-token lifecycle and PostgreSQL authorization context."""
from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets
from typing import Any, ContextManager

import psycopg


_TOKEN_PREFIX = "iwiki"
_LAST_USED_INTERVAL = timedelta(minutes=5)


class AccessError(PermissionError):
    """Safe authentication or authorization failure for an HTTP boundary."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        message = "authentication required" if status_code == 401 else "access denied"
        super().__init__(message)


def validate_domain_identifier(value: str) -> str:
    """Return one strict PostgreSQL domain identifier."""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value.startswith(".")
        or "/" in value
        or "\\" in value
    ):
        raise ValueError("domain identifier is invalid")
    return value


def _unique_domains(values) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise ValueError("domain grants must be arrays")
    result = []
    for value in values:
        validate_domain_identifier(value)
        if value not in result:
            result.append(value)
    return tuple(result)


@dataclass(frozen=True)
class AuthContext:
    """Immutable authenticated tenant and maximum domain grants."""

    iwiki_id: str
    token_id: str
    read_domains: tuple[str, ...]
    write_domains: tuple[str, ...]
    primary: str | None = None
    can_create_domain: bool = False
    managed_domains: tuple[str, ...] = ()

    def can_read(self, domain: str) -> bool:
        return domain in self.read_domains

    def can_write(self, domain: str) -> bool:
        return domain in self.write_domains

    def require_read(self, domain: str) -> None:
        if not self.can_read(domain):
            raise AccessError(403)

    def require_write(self, domain: str) -> None:
        if not self.can_write(domain):
            raise AccessError(403)

    def can_manage_grants(self, domain: str) -> bool:
        return domain in self.managed_domains

    def require_manage_grants(self, domain: str) -> None:
        if not self.can_manage_grants(domain):
            raise AccessError(403)

    def narrow(
        self,
        *,
        read_domains: list[str] | tuple[str, ...] | None = None,
        write_domains: list[str] | tuple[str, ...] | None = None,
        primary: str | None = None,
    ) -> "AuthContext":
        read = self.read_domains if read_domains is None else _unique_domains(read_domains)
        write = self.write_domains if write_domains is None else _unique_domains(write_domains)
        if any(domain not in self.read_domains for domain in read):
            raise PermissionError("read scope cannot be expanded")
        if any(domain not in self.write_domains for domain in write):
            raise PermissionError("write scope cannot be expanded")
        if any(domain not in read for domain in write):
            raise PermissionError("write scope must remain readable")
        selected = primary
        if selected is None:
            selected = (
                self.primary
                if self.primary in write
                else (write[0] if write else None)
            )
        if selected is not None and selected not in write:
            raise PermissionError("primary must belong to write scope")
        return AuthContext(
            iwiki_id=self.iwiki_id,
            token_id=self.token_id,
            read_domains=read,
            write_domains=write,
            primary=selected,
            can_create_domain=self.can_create_domain,
            managed_domains=self.managed_domains,
        )


class AuthStore:
    """Transaction-safe token administration and authentication store."""

    def __init__(
        self,
        dsn: str,
        *,
        connection_factory: Callable[[], ContextManager[Any]] | None = None,
    ) -> None:
        self.dsn = dsn
        self._connection_factory = connection_factory or (
            lambda: psycopg.connect(self.dsn)
        )

    def _connect(self):
        return self._connection_factory()

    def create_wiki(self, iwiki_id: str, slug: str) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO iwiki.iwikis (iwiki_id, slug) VALUES (%s, %s) "
                    "ON CONFLICT (iwiki_id) DO NOTHING",
                    (iwiki_id, slug),
                )

    def create_domain(self, iwiki_id: str, domain: str) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO iwiki.domains (iwiki_id, slug) VALUES (%s, %s) "
                    "ON CONFLICT (iwiki_id, slug) DO NOTHING",
                    (iwiki_id, domain),
                )

    @staticmethod
    def parse_token(token: str) -> tuple[str, bytes]:
        if not isinstance(token, str):
            raise ValueError("malformed token")
        parts = token.split("_", 2)
        if len(parts) != 3 or parts[0] != _TOKEN_PREFIX or not parts[1]:
            raise ValueError("malformed token")
        encoded = parts[2]
        try:
            secret = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except (ValueError, TypeError) as exc:
            raise ValueError("malformed token") from exc
        if len(secret) != 32:
            raise ValueError("malformed token")
        return parts[1], secret

    def create_token(
        self,
        iwiki_id: str,
        owner: str,
        *,
        read_domains: list[str],
        write_domains: list[str],
        can_create_domain: bool = False,
    ) -> dict:
        read = _unique_domains(read_domains)
        write = _unique_domains(write_domains)
        if any(domain not in read for domain in write):
            raise ValueError("write grant must also be readable")
        if not read and not can_create_domain:
            raise ValueError("read grant is required")
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("token owner is required")

        token_id = secrets.token_hex(16)
        secret = secrets.token_urlsafe(32)
        token = f"{_TOKEN_PREFIX}_{token_id}_{secret}"
        digest = hashlib.sha256(token.encode()).digest()
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT slug, domain_id FROM iwiki.domains "
                    "WHERE iwiki_id = %s AND slug = ANY(%s)",
                    (iwiki_id, list(read)),
                )
                domains = dict(cursor.fetchall())
                if set(domains) != set(read):
                    raise ValueError("domain grant does not exist")
                cursor.execute(
                    "INSERT INTO iwiki.tokens "
                    "(iwiki_id, token_id, token_digest, owner, "
                    "can_create_domain) VALUES (%s, %s, %s, %s, %s)",
                    (
                        iwiki_id,
                        token_id,
                        digest,
                        owner.strip(),
                        can_create_domain,
                    ),
                )
                for domain in read:
                    cursor.execute(
                        "INSERT INTO iwiki.token_domain_grants "
                        "(iwiki_id, token_id, domain_id, can_read, can_write) "
                        "VALUES (%s, %s, %s, true, %s)",
                        (iwiki_id, token_id, domains[domain], domain in write),
                    )
        return {
            "token_id": token_id,
            "token": token,
            "read_domains": list(read),
            "write_domains": list(write),
        }

    def authenticate(
        self, token: str, *, now: datetime | None = None
    ) -> AuthContext | None:
        try:
            token_id, _secret = self.parse_token(token)
        except ValueError:
            return None
        supplied_digest = hashlib.sha256(token.encode()).digest()
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("authentication time must be timezone-aware")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT t.iwiki_id, t.token_digest, t.can_create_domain "
                    "FROM iwiki.tokens t "
                    "JOIN iwiki.iwikis w ON w.iwiki_id = t.iwiki_id "
                    "WHERE t.token_id = %s AND t.revoked_at IS NULL "
                    "AND w.active = true",
                    (token_id,),
                )
                row = cursor.fetchone()
                if row is None or not hmac.compare_digest(
                    supplied_digest, bytes(row[1])
                ):
                    return None
                iwiki_id, _stored_digest, can_create_domain = row
                cursor.execute(
                    "SELECT d.slug, g.can_read, g.can_write, "
                    "m.can_manage_grants "
                    "FROM iwiki.domains d "
                    "LEFT JOIN iwiki.token_domain_grants g "
                    "ON g.iwiki_id = d.iwiki_id "
                    "AND g.domain_id = d.domain_id AND g.token_id = %s "
                    "LEFT JOIN iwiki.token_domain_management_grants m "
                    "ON m.iwiki_id = d.iwiki_id "
                    "AND m.domain_id = d.domain_id AND m.token_id = %s "
                    "WHERE d.iwiki_id = %s "
                    "AND (g.token_id IS NOT NULL OR m.token_id IS NOT NULL) "
                    "ORDER BY d.slug",
                    (token_id, token_id, iwiki_id),
                )
                grants = cursor.fetchall()
                read = tuple(row[0] for row in grants if row[1])
                write = tuple(row[0] for row in grants if row[2])
                managed = tuple(row[0] for row in grants if row[3])
                cursor.execute(
                    "UPDATE iwiki.tokens SET last_used_at = %s "
                    "WHERE iwiki_id = %s AND token_id = %s "
                    "AND (last_used_at IS NULL OR last_used_at <= %s)",
                    (
                        current,
                        iwiki_id,
                        token_id,
                        current - _LAST_USED_INTERVAL,
                    ),
                )
        return AuthContext(
            iwiki_id=iwiki_id,
            token_id=token_id,
            read_domains=read,
            write_domains=write,
            primary=write[0] if write else None,
            can_create_domain=bool(can_create_domain),
            managed_domains=managed,
        )

    @staticmethod
    def _active_caller(cursor, context: AuthContext, *, lock: bool) -> bool:
        suffix = " FOR UPDATE" if lock else ""
        cursor.execute(
            "SELECT can_create_domain FROM iwiki.tokens "
            "WHERE iwiki_id = %s AND token_id = %s "
            "AND revoked_at IS NULL" + suffix,
            (context.iwiki_id, context.token_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise AccessError(403)
        return bool(row[0])

    @classmethod
    def _managed_domain(
        cls,
        cursor,
        context: AuthContext,
        domain: str,
        *,
        lock: bool,
    ) -> int:
        cls._active_caller(cursor, context, lock=lock)
        suffix = " FOR UPDATE OF m" if lock else ""
        cursor.execute(
            "SELECT d.domain_id FROM iwiki.domains d "
            "JOIN iwiki.token_domain_management_grants m "
            "ON m.iwiki_id = d.iwiki_id AND m.domain_id = d.domain_id "
            "WHERE d.iwiki_id = %s AND d.slug = %s "
            "AND m.token_id = %s AND m.can_manage_grants = true" + suffix,
            (context.iwiki_id, domain, context.token_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise AccessError(403)
        return row[0]

    @staticmethod
    def _active_target(cursor, context: AuthContext, token_id: str) -> str:
        if token_id == context.token_id:
            raise AccessError(403)
        cursor.execute(
            "SELECT owner FROM iwiki.tokens "
            "WHERE iwiki_id = %s AND token_id = %s "
            "AND revoked_at IS NULL FOR UPDATE",
            (context.iwiki_id, token_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise AccessError(403)
        return row[0]

    def provision_domain(self, context: AuthContext, domain: str) -> dict:
        valid_domain = validate_domain_identifier(domain)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                if not self._active_caller(cursor, context, lock=True):
                    raise AccessError(403)
                cursor.execute(
                    "INSERT INTO iwiki.domains (iwiki_id, slug) "
                    "VALUES (%s, %s) ON CONFLICT (iwiki_id, slug) "
                    "DO NOTHING RETURNING domain_id",
                    (context.iwiki_id, valid_domain),
                )
                inserted = cursor.fetchone()
                if inserted is not None:
                    domain_id = inserted[0]
                    cursor.execute(
                        "INSERT INTO iwiki.token_domain_grants "
                        "(iwiki_id, token_id, domain_id, can_read, can_write) "
                        "VALUES (%s, %s, %s, true, true)",
                        (context.iwiki_id, context.token_id, domain_id),
                    )
                    cursor.execute(
                        "INSERT INTO iwiki.token_domain_management_grants "
                        "(iwiki_id, token_id, domain_id, "
                        "can_manage_grants) VALUES (%s, %s, %s, true)",
                        (context.iwiki_id, context.token_id, domain_id),
                    )
                    return {
                        "domain": valid_domain,
                        "already_existed": False,
                    }

                cursor.execute(
                    "SELECT g.can_read, g.can_write, m.can_manage_grants "
                    "FROM iwiki.domains d "
                    "LEFT JOIN iwiki.token_domain_grants g "
                    "ON g.iwiki_id = d.iwiki_id "
                    "AND g.domain_id = d.domain_id AND g.token_id = %s "
                    "LEFT JOIN iwiki.token_domain_management_grants m "
                    "ON m.iwiki_id = d.iwiki_id "
                    "AND m.domain_id = d.domain_id AND m.token_id = %s "
                    "WHERE d.iwiki_id = %s AND d.slug = %s FOR UPDATE OF d",
                    (
                        context.token_id,
                        context.token_id,
                        context.iwiki_id,
                        valid_domain,
                    ),
                )
                existing = cursor.fetchone()
                if existing != (True, True, True):
                    raise AccessError(403)
                return {"domain": valid_domain, "already_existed": True}

    def list_domain_grants(
        self, context: AuthContext, domain: str
    ) -> list[dict]:
        valid_domain = validate_domain_identifier(domain)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                domain_id = self._managed_domain(
                    cursor, context, valid_domain, lock=False
                )
                cursor.execute(
                    "SELECT t.token_id, t.owner, g.can_read, g.can_write, "
                    "m.can_manage_grants FROM iwiki.tokens t "
                    "LEFT JOIN iwiki.token_domain_grants g "
                    "ON g.iwiki_id = t.iwiki_id AND g.token_id = t.token_id "
                    "AND g.domain_id = %s "
                    "LEFT JOIN iwiki.token_domain_management_grants m "
                    "ON m.iwiki_id = t.iwiki_id AND m.token_id = t.token_id "
                    "AND m.domain_id = %s "
                    "WHERE t.iwiki_id = %s "
                    "AND (g.domain_id IS NOT NULL OR m.domain_id IS NOT NULL) "
                    "ORDER BY t.token_id",
                    (domain_id, domain_id, context.iwiki_id),
                )
                rows = cursor.fetchall()
        return [
            {
                "token_id": token_id,
                "owner": owner,
                "can_read": bool(can_read),
                "can_write": bool(can_write),
                "can_manage_grants": bool(can_manage),
            }
            for token_id, owner, can_read, can_write, can_manage in rows
        ]

    def set_domain_grant(
        self,
        context: AuthContext,
        domain: str,
        token_id: str,
        *,
        can_read: bool,
        can_write: bool,
    ) -> dict:
        valid_domain = validate_domain_identifier(domain)
        if not isinstance(can_read, bool) or not isinstance(can_write, bool):
            raise ValueError("grant flags must be booleans")
        if can_write and not can_read:
            raise ValueError("write grant must also be readable")
        if not can_read and not can_write:
            raise ValueError("empty grant must be revoked")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                domain_id = self._managed_domain(
                    cursor, context, valid_domain, lock=True
                )
                self._active_target(cursor, context, token_id)
                cursor.execute(
                    "INSERT INTO iwiki.token_domain_grants "
                    "(iwiki_id, token_id, domain_id, can_read, can_write) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (iwiki_id, token_id, domain_id) "
                    "DO UPDATE SET can_read = EXCLUDED.can_read, "
                    "can_write = EXCLUDED.can_write",
                    (
                        context.iwiki_id,
                        token_id,
                        domain_id,
                        can_read,
                        can_write,
                    ),
                )
        return {
            "domain": valid_domain,
            "token_id": token_id,
            "can_read": can_read,
            "can_write": can_write,
        }

    def revoke_domain_grant(
        self, context: AuthContext, domain: str, token_id: str
    ) -> dict:
        valid_domain = validate_domain_identifier(domain)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                domain_id = self._managed_domain(
                    cursor, context, valid_domain, lock=True
                )
                self._active_target(cursor, context, token_id)
                cursor.execute(
                    "DELETE FROM iwiki.token_domain_grants "
                    "WHERE iwiki_id = %s AND token_id = %s AND domain_id = %s",
                    (context.iwiki_id, token_id, domain_id),
                )
                revoked = cursor.rowcount == 1
        return {
            "domain": valid_domain,
            "token_id": token_id,
            "revoked": revoked,
        }

    def set_create_domain(
        self, iwiki_id: str, token_id: str, enabled: bool
    ) -> dict:
        if not isinstance(enabled, bool):
            raise ValueError("enabled flag must be boolean")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE iwiki.tokens t SET can_create_domain = %s "
                    "FROM iwiki.iwikis w WHERE w.iwiki_id = t.iwiki_id "
                    "AND w.active = true AND t.iwiki_id = %s "
                    "AND t.token_id = %s AND t.revoked_at IS NULL",
                    (enabled, iwiki_id, token_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("active token not found")
        return {
            "iwiki": iwiki_id,
            "token_id": token_id,
            "can_create_domain": enabled,
        }

    def set_domain_management(
        self,
        iwiki_id: str,
        token_id: str,
        domain: str,
        enabled: bool,
    ) -> dict:
        valid_domain = validate_domain_identifier(domain)
        if not isinstance(enabled, bool):
            raise ValueError("enabled flag must be boolean")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT d.domain_id FROM iwiki.domains d "
                    "JOIN iwiki.iwikis w ON w.iwiki_id = d.iwiki_id "
                    "WHERE d.iwiki_id = %s AND d.slug = %s "
                    "AND w.active = true",
                    (iwiki_id, valid_domain),
                )
                domain_row = cursor.fetchone()
                cursor.execute(
                    "SELECT 1 FROM iwiki.tokens WHERE iwiki_id = %s "
                    "AND token_id = %s AND revoked_at IS NULL FOR UPDATE",
                    (iwiki_id, token_id),
                )
                token_row = cursor.fetchone()
                if domain_row is None or token_row is None:
                    raise ValueError("active token or domain not found")
                domain_id = domain_row[0]
                if enabled:
                    cursor.execute(
                        "INSERT INTO iwiki.token_domain_management_grants "
                        "(iwiki_id, token_id, domain_id, "
                        "can_manage_grants) VALUES (%s, %s, %s, true) "
                        "ON CONFLICT (iwiki_id, token_id, domain_id) "
                        "DO NOTHING",
                        (iwiki_id, token_id, domain_id),
                    )
                else:
                    cursor.execute(
                        "DELETE FROM iwiki.token_domain_management_grants "
                        "WHERE iwiki_id = %s AND token_id = %s "
                        "AND domain_id = %s",
                        (iwiki_id, token_id, domain_id),
                    )
        return {
            "iwiki": iwiki_id,
            "token_id": token_id,
            "domain": valid_domain,
            "can_manage_grants": enabled,
        }

    def list_tokens(self, iwiki_id: str) -> list[dict]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT t.token_id, t.owner, t.created_at, t.last_used_at, "
                    "t.revoked_at, t.can_create_domain, "
                    "COALESCE(array_agg(DISTINCT d.slug ORDER BY d.slug) "
                    "FILTER (WHERE g.can_read), ARRAY[]::text[]), "
                    "COALESCE(array_agg(DISTINCT d.slug ORDER BY d.slug) "
                    "FILTER (WHERE g.can_write), ARRAY[]::text[]), "
                    "COALESCE(array_agg(DISTINCT md.slug ORDER BY md.slug) "
                    "FILTER (WHERE m.can_manage_grants), ARRAY[]::text[]) "
                    "FROM iwiki.tokens t "
                    "LEFT JOIN iwiki.token_domain_grants g "
                    "ON g.iwiki_id = t.iwiki_id AND g.token_id = t.token_id "
                    "LEFT JOIN iwiki.domains d ON d.iwiki_id = g.iwiki_id "
                    "AND d.domain_id = g.domain_id "
                    "LEFT JOIN iwiki.token_domain_management_grants m "
                    "ON m.iwiki_id = t.iwiki_id AND m.token_id = t.token_id "
                    "LEFT JOIN iwiki.domains md ON md.iwiki_id = m.iwiki_id "
                    "AND md.domain_id = m.domain_id "
                    "WHERE t.iwiki_id = %s "
                    "GROUP BY t.iwiki_id, t.token_id "
                    "ORDER BY t.created_at, t.token_id",
                    (iwiki_id,),
                )
                rows = cursor.fetchall()
        return [
            {
                "token_id": token_id,
                "owner": owner,
                "created_at": created_at,
                "last_used_at": last_used_at,
                "revoked_at": revoked_at,
                "can_create_domain": can_create_domain,
                "read_domains": list(read_domains),
                "write_domains": list(write_domains),
                "managed_domains": list(managed_domains),
            }
            for (
                token_id,
                owner,
                created_at,
                last_used_at,
                revoked_at,
                can_create_domain,
                read_domains,
                write_domains,
                managed_domains,
            ) in rows
        ]

    def revoke_token(self, token_id: str) -> bool:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE iwiki.tokens SET revoked_at = CURRENT_TIMESTAMP "
                    "WHERE token_id = %s AND revoked_at IS NULL "
                    "RETURNING iwiki_id",
                    (token_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return False
                iwiki_id = row[0]
                cursor.execute(
                    "DELETE FROM iwiki.token_domain_grants "
                    "WHERE iwiki_id = %s AND token_id = %s",
                    (iwiki_id, token_id),
                )
                cursor.execute(
                    "DELETE FROM iwiki.token_domain_management_grants "
                    "WHERE iwiki_id = %s AND token_id = %s",
                    (iwiki_id, token_id),
                )
                return True

    def set_wiki_active(self, iwiki_id: str, active: bool) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE iwiki.iwikis SET active = %s, "
                    "updated_at = CURRENT_TIMESTAMP WHERE iwiki_id = %s",
                    (active, iwiki_id),
                )

    def last_used_at(self, token_id: str) -> datetime | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT last_used_at FROM iwiki.tokens WHERE token_id = %s",
                    (token_id,),
                )
                row = cursor.fetchone()
        return row[0] if row else None


def authenticate_bearer(store: AuthStore, authorization: str | None) -> AuthContext:
    """Authenticate one strict Bearer header without exposing rejection details."""
    if not isinstance(authorization, str):
        raise AccessError(401)
    parts = authorization.split(" ")
    if len(parts) != 2 or parts[0] != "Bearer" or not parts[1]:
        raise AccessError(401)
    context = store.authenticate(parts[1])
    if context is None:
        raise AccessError(401)
    return context


def authorize_domains(
    context: AuthContext,
    *,
    read_domains: tuple[str, ...] = (),
    write_domains: tuple[str, ...] = (),
) -> None:
    """Reject operations outside authenticated grants with one safe response."""
    if any(not context.can_read(domain) for domain in read_domains):
        raise AccessError(403)
    if any(not context.can_write(domain) for domain in write_domains):
        raise AccessError(403)
