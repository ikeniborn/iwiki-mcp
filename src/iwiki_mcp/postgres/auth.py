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

    def list_tokens(self, iwiki_id: str) -> list[dict]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT t.token_id, t.owner, t.created_at, t.last_used_at, "
                    "t.revoked_at, d.slug, g.can_read, g.can_write "
                    "FROM iwiki.tokens t "
                    "LEFT JOIN iwiki.token_domain_grants g "
                    "ON g.iwiki_id = t.iwiki_id AND g.token_id = t.token_id "
                    "LEFT JOIN iwiki.domains d ON d.iwiki_id = g.iwiki_id "
                    "AND d.domain_id = g.domain_id "
                    "WHERE t.iwiki_id = %s ORDER BY t.created_at, t.token_id, d.slug",
                    (iwiki_id,),
                )
                rows = cursor.fetchall()
        tokens: dict[str, dict] = {}
        for (
            token_id,
            owner,
            created_at,
            last_used_at,
            revoked_at,
            domain,
            can_read,
            can_write,
        ) in rows:
            token = tokens.setdefault(
                token_id,
                {
                    "token_id": token_id,
                    "owner": owner,
                    "created_at": created_at,
                    "last_used_at": last_used_at,
                    "revoked_at": revoked_at,
                    "read_domains": [],
                    "write_domains": [],
                },
            )
            if domain is not None and can_read:
                token["read_domains"].append(domain)
            if domain is not None and can_write:
                token["write_domains"].append(domain)
        return list(tokens.values())

    def revoke_token(self, token_id: str) -> bool:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE iwiki.tokens SET revoked_at = CURRENT_TIMESTAMP "
                    "WHERE token_id = %s AND revoked_at IS NULL",
                    (token_id,),
                )
                return cursor.rowcount == 1

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
